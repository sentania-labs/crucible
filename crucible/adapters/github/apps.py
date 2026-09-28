"""What the GitHub App can see (crucible#120): its identity, its installations, and the
repositories each installation covers, for the Connect GitHub flow and the repository
picker. Reads only. The one token minted per installation is scoped to metadata read and
discarded before the call returns; nothing here is cached or stored."""

from __future__ import annotations

from typing import Any

from crucible.adapters.github.appauth import AppAuthenticator
from crucible.adapters.github.transport import MAX_PAGES, PER_PAGE, RestTransport
from crucible.ports.github import AppCredential, GitHubError, ManifestConversion


class RestGitHubApps:
    """The `GitHubAppDirectory` port over the REST API."""

    def __init__(self, authenticator: AppAuthenticator, transport: RestTransport) -> None:
        self._auth = authenticator
        self._http = transport

    def app(self, credential: AppCredential | None = None) -> dict[str, Any]:
        payload = self._http.get("/app", bearer=self._auth.app_jwt(credential))
        if not isinstance(payload, dict):
            raise GitHubError(200, "GET /app answered something that is not an App", path="/app")
        owner = payload.get("owner") or {}
        return {
            "id": payload.get("id"),
            "slug": payload.get("slug"),
            "name": payload.get("name"),
            "owner": owner.get("login") if isinstance(owner, dict) else None,
            "html_url": payload.get("html_url"),
        }

    def installations(self) -> list[dict[str, Any]]:
        items = self._http.paginate("/app/installations", bearer=self._auth.app_jwt())
        out: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict) or item.get("id") is None:
                continue
            account = item.get("account") or {}
            out.append(
                {
                    "id": int(item["id"]),
                    "account": account.get("login") if isinstance(account, dict) else None,
                    "account_type": account.get("type") if isinstance(account, dict) else None,
                    "repository_selection": item.get("repository_selection"),
                    "html_url": item.get("html_url"),
                }
            )
        return out

    def installation_repositories(self, installation_id: int) -> list[dict[str, Any]]:
        token = self._auth.unscoped_installation_token(installation_id)
        try:
            repositories: list[dict[str, Any]] = []
            page = 1
            while page <= MAX_PAGES:
                payload = self._http.get(
                    "/installation/repositories",
                    bearer=token.reveal(),
                    params={"per_page": PER_PAGE, "page": page},
                )
                batch = payload.get("repositories") if isinstance(payload, dict) else None
                if not isinstance(batch, list):
                    break
                repositories.extend(r for r in batch if isinstance(r, dict))
                total = payload.get("total_count") if isinstance(payload, dict) else None
                if len(batch) < PER_PAGE or (isinstance(total, int) and len(repositories) >= total):
                    break
                page += 1
        finally:
            token.discard()
        return [
            {
                "full_name": repo.get("full_name"),
                "owner": (repo.get("owner") or {}).get("login"),
                "html_url": repo.get("html_url"),
                "clone_url": repo.get("clone_url"),
                "default_branch": repo.get("default_branch") or "main",
                "private": repo.get("private") is True,
                "archived": repo.get("archived") is True,
            }
            for repo in repositories
            if repo.get("full_name")
        ]

    def convert_manifest(self, code: str) -> ManifestConversion:
        status, payload, _ = self._http.request(
            "POST", f"/app-manifests/{code}/conversions", bearer=""
        )
        path = "/app-manifests/{code}/conversions"
        if status >= 400:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise GitHubError(status, str(message or "request failed"), path=path)
        if not isinstance(payload, dict):
            raise GitHubError(status, "the conversion answered no App", path=path)
        pem = payload.get("pem")
        app_id = payload.get("id")
        if not isinstance(pem, str) or not pem.strip() or not isinstance(app_id, int):
            raise GitHubError(status, "the conversion answered no App id and key", path=path)
        owner = payload.get("owner") or {}
        webhook_secret = payload.get("webhook_secret")
        return ManifestConversion(
            app_id=app_id,
            slug=str(payload.get("slug") or ""),
            name=str(payload.get("name") or payload.get("slug") or ""),
            owner=owner.get("login") if isinstance(owner, dict) else None,
            html_url=str(payload.get("html_url") or ""),
            private_key=pem.encode("utf-8"),
            webhook_secret=webhook_secret.encode("utf-8")
            if isinstance(webhook_secret, str) and webhook_secret
            else None,
        )
