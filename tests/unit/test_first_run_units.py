"""The small pieces of the first-run setup (crucible#79, #119, #120, #121): the two
stores of the GitHub App credential the service owns (ADR 0016), the configured rule,
and the plain words the gateway test and the Status page use."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sapi import KubernetesApiError
from crucible.adapters.execution.k8sfake import FakeKubernetesApi
from crucible.adapters.github.credentials import DirectoryAppCredentials, SecretAppCredentials
from crucible.application.admin.credentials import model_ids
from crucible.application.admin.gateway import plain_outcome
from crucible.ports.github import GitHubAppStoreError

# Assembled at run time so no fixture on disk carries a key header the scanner flags.
_LABEL = b"RSA " + b"PRIVATE KEY"
PEM = b"-----BEGIN " + _LABEL + b"-----\nnot-a-real-key\n-----END " + _LABEL + b"-----\n"


def test_the_secret_store_is_created_as_the_services_own_and_read_back() -> None:
    api = FakeKubernetesApi()
    store = SecretAppCredentials(api, name="crucible-github-app")
    assert store.read() is None
    assert store.describe()["exists"] is False

    done = store.write(app_id=42, private_key=PEM, webhook_secret=b"hook")

    assert done == {"store": "secret", "name": "crucible-github-app", "created": True}
    body = api.objects[("secrets", "crucible-github-app")].body
    assert body["metadata"]["labels"] == {
        k8sspec.LABEL_MANAGED_BY: "crucible",
        k8sspec.LABEL_CREDENTIAL: "github-app",
    }
    credential = store.read()
    assert credential is not None and credential.app_id == 42 and credential.private_key == PEM
    assert "not-a-real-key" not in repr(credential)
    described = store.describe()
    assert described["service_owned"] is True and described["webhook_secret_present"] is True


def test_a_second_write_replaces_the_key_and_keeps_a_webhook_secret_not_given() -> None:
    api = FakeKubernetesApi()
    store = SecretAppCredentials(api)
    store.write(app_id=42, private_key=PEM, webhook_secret=b"hook")

    again = store.write(app_id=43, private_key=PEM.replace(b"not", b"new"), webhook_secret=None)

    assert again["created"] is False
    credential = store.read()
    assert credential is not None and credential.app_id == 43
    assert b"new-a-real-key" in credential.private_key
    assert store.describe()["webhook_secret_present"] is True


def test_a_gitops_secret_counts_only_with_the_settings_app_id_and_enabled() -> None:
    """ADR 0016: a Secret a deployment placed itself (no `app-id` key) is configured
    only when the settings name the App and turn GitHub on, as before the ADR."""
    api = FakeKubernetesApi()
    api.create(
        "secrets",
        k8sspec.secret(
            name="crucible-github-app", namespace="", object_labels={}, data={"app.pem": PEM}
        ),
    )
    assert SecretAppCredentials(api).read() is None
    assert SecretAppCredentials(api, settings_app_id=7).read() is None
    found = SecretAppCredentials(api, settings_app_id=7, settings_enabled=True).read()
    assert found is not None and found.app_id == 7
    assert SecretAppCredentials(api).describe()["service_owned"] is False


def test_an_unreadable_secret_is_a_refusal_not_an_absence() -> None:
    class Forbidden(FakeKubernetesApi):
        def get(self, kind: str, name: str) -> dict[str, object]:
            raise KubernetesApiError(403, "forbidden")

    store = SecretAppCredentials(Forbidden(), name="crucible-github-app")
    with pytest.raises(GitHubAppStoreError, match="not readable"):
        store.read()
    assert store.describe()["exists"] is None


def test_a_secret_of_another_type_under_the_name_is_never_used_or_adopted() -> None:
    """ADR 0016: `create` cannot be narrowed by name, so a service-account token Secret
    could sit under this name; it is refused, not read as the App credential."""
    api = FakeKubernetesApi()
    body = k8sspec.secret(
        name="crucible-github-app", namespace="", object_labels={}, data={"app.pem": PEM}
    )
    body["type"] = "kubernetes.io/service-account-token"
    api.create("secrets", body)
    store = SecretAppCredentials(api, settings_app_id=7, settings_enabled=True)
    with pytest.raises(GitHubAppStoreError, match="not Opaque"):
        store.read()
    with pytest.raises(GitHubAppStoreError, match="not Opaque"):
        store.write(app_id=7, private_key=PEM, webhook_secret=None)


def test_the_directory_store_writes_private_files_beside_the_key(tmp_path: Path) -> None:
    directory = tmp_path / "github"
    directory.mkdir(mode=0o700)
    store = DirectoryAppCredentials(
        str(directory / "app.pem"), webhook_secret_path=str(directory / "webhook.secret")
    )
    assert store.read() is None

    store.write(app_id=9, private_key=PEM, webhook_secret=b"hook")

    for name in ("app.pem", "app-id", "webhook.secret"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    credential = store.read()
    assert credential is not None and credential.app_id == 9
    assert store.describe()["service_owned"] is True
    assert not list(directory.glob(".*incoming"))


def test_a_read_only_directory_is_refused_by_name(tmp_path: Path) -> None:
    directory = tmp_path / "github"
    directory.mkdir(mode=0o500)
    store = DirectoryAppCredentials(str(directory / "app.pem"))
    try:
        with pytest.raises(GitHubAppStoreError, match="read-only"):
            store.write(app_id=9, private_key=PEM, webhook_secret=None)
    finally:
        directory.chmod(0o700)


@pytest.mark.parametrize(
    ("outcome", "words"),
    [
        (None, "never tested"),
        ("probe:completed", "the last test passed"),
        ("probe:auth_failure", "the key was not accepted"),
        ("probe:inconclusive:endpoint_not_configured", "the gateway URL is not set"),
        ("probe:inconclusive:readiness_http_503", "readiness check answered HTTP 503"),
        ("probe:inconclusive:models_http_500", "listing the gateway's models answered HTTP 500"),
        ("completed", "the last launch ended completed"),
    ],
)
def test_a_recorded_outcome_reads_as_plain_words(outcome: str | None, words: str) -> None:
    assert words in plain_outcome(outcome)


def test_model_ids_read_an_openai_list_and_nothing_else() -> None:
    assert model_ids(b'{"data": [{"id": "a"}, {"id": "b"}, {"id": "a"}, {"x": 1}]}') == ["a", "b"]
    assert model_ids(b"not json") == []
    assert model_ids(b'{"data": "nope"}') == []
