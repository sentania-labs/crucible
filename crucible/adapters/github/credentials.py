"""The GitHub App credential the service owns (ADR 0017, crucible#120, #79).

Two stores behind one port. On Kubernetes the credential is the `crucible-github-app`
Secret in the service's own namespace: the service creates it the first time the
Connect GitHub flow writes it, labels it as its own, and replaces its data whole on each
later write. It is read through the API server on each signature rather than from the
mounted file, so a save is in force at once instead of after the kubelet's next sync;
the mount stays for the webhook secret, which the webhook route reads as a file. With
the Docker provider the credential is the files beside `github.app.private_key_path`,
written mode 0600 by the same flow.

Keys and file names are the same in both: `app-id`, `app.pem`, `webhook.secret`. The App
id is a public identifier; the other two are never returned, logged or audited.
"""

from __future__ import annotations

import base64
import contextlib
import os
from pathlib import Path
from typing import Any

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.ports.github import AppCredential, GitHubAppStoreError

APP_ID = "app-id"
PRIVATE_KEY = "app.pem"
WEBHOOK_SECRET = "webhook.secret"
CREDENTIAL_LABEL = "github-app"


def _app_id(raw: bytes | None) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        return None
    return value if value > 0 else None


def _resolve(
    files: dict[str, bytes] | None, *, settings_app_id: int, settings_enabled: bool
) -> AppCredential | None:
    """The configured rule (ADR 0017): a key, and an App id that the service wrote beside
    it, or that the settings name with `github.enabled` on. A key alone is not enough."""
    if not files or not files.get(PRIVATE_KEY):
        return None
    stored = _app_id(files.get(APP_ID))
    if stored is not None:
        return AppCredential(stored, files[PRIVATE_KEY])
    if settings_enabled and settings_app_id > 0:
        return AppCredential(settings_app_id, files[PRIVATE_KEY])
    return None


class SecretAppCredentials:
    """The App credential as a Secret the service owns, in its own namespace."""

    def __init__(
        self,
        client: Any,
        *,
        name: str = "crucible-github-app",
        settings_app_id: int = 0,
        settings_enabled: bool = False,
    ) -> None:
        self._client = client
        self.name = name
        self._settings_app_id = settings_app_id
        self._settings_enabled = settings_enabled

    @property
    def namespace(self) -> str:
        return str(getattr(self._client, "namespace", ""))

    def _body(self) -> dict[str, Any] | None:
        try:
            body: dict[str, Any] = self._client.get("secrets", self.name)
        except KubernetesApiError as exc:
            if exc.status == 404:
                return None
            raise GitHubAppStoreError(
                f"the Secret {self.name!r} in {self.namespace} is not readable ({exc.status})"
            ) from None
        self._require_opaque(body)
        return body

    def _require_opaque(self, body: dict[str, Any]) -> None:
        """Only an Opaque Secret is the App credential. A Secret of another type under
        this name (a service-account token, say) is refused, never read or adopted."""
        kind = body.get("type") or "Opaque"
        if kind != "Opaque":
            raise GitHubAppStoreError(
                f"the Secret {self.name!r} in {self.namespace} is of type {kind}, not Opaque; "
                "it is not the App credential and the service will not use or adopt it"
            )

    def _files(self, body: dict[str, Any] | None) -> dict[str, bytes] | None:
        if body is None:
            return None
        out: dict[str, bytes] = {}
        for key, raw in (body.get("data") or {}).items():
            with contextlib.suppress(ValueError):
                out[str(key)] = base64.b64decode(str(raw))
        return out

    def describe(self) -> dict[str, Any]:
        try:
            body = self._body()
        except GitHubAppStoreError as exc:
            return {
                "kind": "secret",
                "name": self.name,
                "namespace": self.namespace,
                "exists": None,
                "detail": str(exc),
            }
        files = self._files(body) or {}
        labels = ((body or {}).get("metadata") or {}).get("labels") or {}
        return {
            "kind": "secret",
            "name": self.name,
            "namespace": self.namespace,
            "exists": body is not None,
            "service_owned": labels.get(k8sspec.LABEL_MANAGED_BY) == k8sspec.MANAGED_BY_CRUCIBLE,
            "app_id_stored": _app_id(files.get(APP_ID)),
            "key_present": bool(files.get(PRIVATE_KEY)),
            "webhook_secret_present": bool(files.get(WEBHOOK_SECRET)),
        }

    def read(self) -> AppCredential | None:
        return _resolve(
            self._files(self._body()),
            settings_app_id=self._settings_app_id,
            settings_enabled=self._settings_enabled,
        )

    def write(
        self, *, app_id: int, private_key: bytes, webhook_secret: bytes | None
    ) -> dict[str, Any]:
        """Created and labelled as the service's own when absent; otherwise one merge
        patch that sets the labels and replaces the id and the key, and the webhook
        secret when one is given. The values travel in the request body only."""
        data = {APP_ID: str(app_id).encode("ascii"), PRIVATE_KEY: private_key}
        if webhook_secret:
            data[WEBHOOK_SECRET] = webhook_secret
        labels = {
            k8sspec.LABEL_MANAGED_BY: k8sspec.MANAGED_BY_CRUCIBLE,
            k8sspec.LABEL_CREDENTIAL: CREDENTIAL_LABEL,
        }
        body = k8sspec.secret(
            name=self.name, namespace=self.namespace, object_labels=labels, data=data
        )
        try:
            self._client.create("secrets", body)
            return {"store": "secret", "name": self.name, "created": True}
        except KubernetesApiError as exc:
            if exc.status != 409:
                raise GitHubAppStoreError(
                    f"the Secret {self.name!r} could not be created in {self.namespace} "
                    f"({exc.status})"
                ) from None
        try:
            current = self._client.get("secrets", self.name)
            self._require_opaque(current)
            keep = {WEBHOOK_SECRET} if not webhook_secret else set()
            stale = {
                k: None for k in (current.get("data") or {}) if k not in data and k not in keep
            }
            self._client.patch(
                "secrets",
                self.name,
                {"metadata": {"labels": labels}, "data": {**body["data"], **stale}},
            )
        except KubernetesApiError as exc:
            raise GitHubAppStoreError(
                f"the Secret {self.name!r} could not be updated in {self.namespace} ({exc.status})"
            ) from None
        return {"store": "secret", "name": self.name, "created": False}


class DirectoryAppCredentials:
    """The App credential as files beside `github.app.private_key_path` (Docker)."""

    def __init__(
        self,
        private_key_path: str,
        *,
        webhook_secret_path: str | None = None,
        settings_app_id: int = 0,
        settings_enabled: bool = False,
    ) -> None:
        self.key_path = Path(private_key_path)
        self.directory = self.key_path.parent
        self.webhook_path = Path(webhook_secret_path) if webhook_secret_path else None
        self.app_id_path = self.directory / APP_ID
        self._settings_app_id = settings_app_id
        self._settings_enabled = settings_enabled

    def _files(self) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for name, path in (
            (APP_ID, self.app_id_path),
            (PRIVATE_KEY, self.key_path),
            (WEBHOOK_SECRET, self.webhook_path),
        ):
            if path is None:
                continue
            with contextlib.suppress(OSError):
                out[name] = path.read_bytes()
        return out

    def describe(self) -> dict[str, Any]:
        files = self._files()
        return {
            "kind": "directory",
            "path": str(self.directory),
            "exists": self.key_path.is_file(),
            "service_owned": self.app_id_path.is_file(),
            "writable": self.directory.is_dir() and os.access(self.directory, os.W_OK),
            "app_id_stored": _app_id(files.get(APP_ID)),
            "key_present": bool(files.get(PRIVATE_KEY)),
            "webhook_secret_present": bool(files.get(WEBHOOK_SECRET)),
        }

    def read(self) -> AppCredential | None:
        return _resolve(
            self._files(),
            settings_app_id=self._settings_app_id,
            settings_enabled=self._settings_enabled,
        )

    def write(
        self, *, app_id: int, private_key: bytes, webhook_secret: bytes | None
    ) -> dict[str, Any]:
        if not self.directory.is_dir() or not os.access(self.directory, os.W_OK):
            raise GitHubAppStoreError(
                f"the GitHub App directory {self.directory} is missing or read-only here; "
                "the service needs it writable to own the credential (ADR 0017)"
            )
        writes = [(self.app_id_path, str(app_id).encode("ascii")), (self.key_path, private_key)]
        if webhook_secret and self.webhook_path is not None:
            writes.append((self.webhook_path, webhook_secret))
        for path, value in writes:
            _write_private(path, value)
        return {"store": "directory", "path": str(self.directory), "created": False}


def _write_private(target: Path, value: bytes) -> None:
    """Mode 0600, written beside the target and renamed over it, so a reader sees the
    old file or the new one and never half of either."""
    temporary = target.with_name(f".{target.name}.incoming")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
    except OSError as exc:
        raise GitHubAppStoreError(f"{target} could not be written ({type(exc).__name__})") from None
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
