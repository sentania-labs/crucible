"""The Kubernetes login Job, the service-owned harness Secret and the credential probe
(25, 26, ADR 0015, crucible#58 and #92) against the in-memory API.

What a real cluster adds (a kubelet, a CNI that enforces the login's policy, a real
pseudo-terminal) is the kind tier's; the driver script itself is exercised here with
bash and `script` on the host, which is the same program the Pod runs."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.k8sapi import _client_frame
from crucible.adapters.execution.k8sfake import FakeKubernetesApi, FakeLogin
from crucible.adapters.execution.kubernetes import (
    _LOGIN_CODE_SCRIPT,
    _LOGIN_DRIVER,
    KubernetesProvider,
)
from crucible.application.admin.login import FLOWS, LoginSession
from crucible.ports.execution import ProbeRequest, ProviderError
from tests.unit.kubernetes_fixtures import build, created, spec

WORKER = "crucible-worker:20260916-login"
ALL_HARNESSES = {
    "crucible.harnesses": "agy,claude_code,codex,hermes",
    "crucible.harness.agy.version": "1.2.8",
    "crucible.harness.claude_code.version": "2.1.280",
    "crucible.harness.codex.version": "0.156.0",
    "crucible.harness.hermes.version": "0.19.0",
}
CODEX_AUTH = json.dumps(
    {"tokens": {"access_token": "not-a-real-value"}, "last_refresh": "2026-09-24T00:00:00Z"}
).encode()


def login_provider(**api_kwargs: Any) -> tuple[FakeKubernetesApi, KubernetesProvider]:
    api, registry, provider = build(**api_kwargs)
    registry.register(WORKER, labels=ALL_HARNESSES)
    return api, provider


def accept_all(_files: Mapping[str, bytes]) -> Sequence[str]:
    return []


async def run_login(
    provider: KubernetesProvider,
    harness: str,
    session: LoginSession,
    *,
    accept: Any = accept_all,
    timeout: int = 5,
    during: Any = None,
) -> None:
    flow = FLOWS[harness]
    task = asyncio.create_task(
        provider.run_login_job(
            flow=flow,
            image=WORKER,
            session=session,
            argv=(flow.image_binary, *flow.argv[1:]),
            timeout=timeout,
            accept=accept,
        )
    )
    if during is not None:
        await during()
    await asyncio.wait_for(task, 10)


def login_job(api: FakeKubernetesApi) -> dict[str, Any]:
    jobs = created(api, "jobs", "login-")
    assert len(jobs) == 1, jobs
    return jobs[0]


# ----- the login Job's egress (crucible#58) ----------------------------------------


async def test_the_login_role_gets_login_endpoints_and_never_the_model_api() -> None:
    _api, provider = login_provider()
    for harness, model_api in (
        ("codex", "api.openai.com"),
        ("claude_code", "api.anthropic.com"),
        ("agy", "daily-cloudcode-pa.googleapis.com"),
    ):
        plan = provider._egress_plan(spec(harness=harness), k8sspec.ROLE_LOGIN)
        assert plan.hosts, harness
        assert model_api not in plan.hosts, harness


async def test_a_codex_login_policy_names_auth_openai_and_not_the_model_address() -> None:
    api, provider = login_provider()
    api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH})
    session = LoginSession(harness="codex", started_at=0)
    await run_login(provider, "codex", session)
    policies = created(api, "networkpolicies", "np-login-")
    assert len(policies) == 1
    policy = policies[0]
    blocks = [
        peer["ipBlock"]["cidr"]
        for rule in policy["spec"]["egress"]
        for peer in rule["to"]
        if "ipBlock" in peer
    ]
    assert "162.159.140.246/32" in blocks  # auth.openai.com
    assert "162.159.140.245/32" not in blocks  # api.openai.com, the model API
    assert policy["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS] == "auth.openai.com"
    selector = policy["spec"]["podSelector"]["matchLabels"]
    assert selector[k8sspec.LABEL_ROLE] == k8sspec.ROLE_LOGIN
    assert k8sspec.LABEL_ATTEMPT not in selector
    assert ("networkpolicies", policy["metadata"]["name"]) in api.deleted


# ----- the device flow -------------------------------------------------------------


async def test_a_device_login_shows_the_url_and_code_and_stores_the_secret() -> None:
    api, provider = login_provider()
    api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH}, finish_after=3)
    session = LoginSession(harness="codex", started_at=0)
    await run_login(provider, "codex", session)
    assert session.state == "finished", session.error
    assert session.url == "https://example.invalid/device"
    assert session.code == "C7AA-TEST"
    assert session.exit_code == 0
    assert session.credential_written is True
    assert not any("crucible-login.exit" in line for line in session.lines)
    body = api.objects[("secrets", "crucible-harness-codex")].body
    assert base64.b64decode(body["data"]["auth.json"]) == CODEX_AUTH
    assert body["metadata"]["labels"] == {
        k8sspec.LABEL_MANAGED_BY: "crucible",
        k8sspec.LABEL_CREDENTIAL: "codex",
    }
    job = login_job(api)
    name = job["metadata"]["name"]
    assert ("jobs", name) in api.deleted
    assert not [n for n in api.object_names("pods") if n.startswith("login-")]


async def test_the_login_pod_has_no_workspace_no_credential_and_a_memory_home() -> None:
    api, provider = login_provider()
    api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH})
    await run_login(provider, "codex", LoginSession(harness="codex", started_at=0))
    job = login_job(api)
    labels = job["metadata"]["labels"]
    assert labels[k8sspec.LABEL_ROLE] == k8sspec.ROLE_LOGIN
    assert labels[k8sspec.LABEL_HARNESS] == "codex"
    assert labels[k8sspec.LABEL_ADMIN] == k8sspec.ADMIN_LOGIN
    # Nothing that sweeps or adopts attempts can reach a login.
    assert k8sspec.LABEL_ATTEMPT not in labels
    assert job["spec"]["ttlSecondsAfterFinished"] > 0
    assert job["spec"]["activeDeadlineSeconds"] > 5
    pod = job["spec"]["template"]["spec"]
    volumes = pod["volumes"]
    assert not [v for v in volumes if "persistentVolumeClaim" in v or "secret" in v]
    assert {v["name"]: v["emptyDir"]["medium"] for v in volumes} == {
        "tmp": "Memory",
        "home": "Memory",
    }
    container = pod["containers"][0]
    assert container["command"][:2] == ["bash", "-c"]
    assert container["command"][3:] == [
        "crucible-login",
        "/usr/local/bin/codex",
        "login",
        "--device-auth",
    ]
    env = {item["name"]: item["value"] for item in container["env"]}
    assert env["CODEX_HOME"] == "/home/worker/.codex"
    assert env["CRUCIBLE_LOGIN_DIR"] == "/home/worker/.codex"
    assert env["CRUCIBLE_EGRESS_ALLOWLIST"] == "auth.openai.com"
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert pod["automountServiceAccountToken"] is False


async def test_agy_logs_in_from_the_home_directory_its_token_sits_under() -> None:
    api, provider = login_provider()
    token = json.dumps({"token": {"expiry": "2026-09-24T01:00:00Z"}}).encode()
    api.login = FakeLogin(
        prompt="Enter the authorization code: ",
        files={"/home/worker/.gemini/antigravity-cli/antigravity-oauth-token": token},
    )
    session = LoginSession(harness="agy", started_at=0)

    async def paste() -> None:
        await wait_for_state(session, "waiting_for_code")
        session.submit_code("4/0Aabc")

    await run_login(provider, "agy", session, during=paste)
    assert session.credential_written is True, session.error
    env = {
        item["name"]: item["value"]
        for item in login_job(api)["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert env["HOME"] == "/home/worker" and env["CRUCIBLE_LOGIN_DIR"] == "/home/worker"
    stored = api.harness_secret("crucible-harness-agy")
    assert stored == {"antigravity-cli_antigravity-oauth-token": token}


# ----- the paste flow and the token --------------------------------------------------


async def wait_for_state(session: LoginSession, state: str) -> None:
    for _ in range(400):
        if session.state == state:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"the session never reached {state}: {session.as_dict()}")


async def test_a_pasted_code_goes_in_over_exec_stdin_and_never_in_an_argv() -> None:
    api, provider = login_provider()
    api.login = FakeLogin(
        lines=[
            "Browser didn't open? Use the url below to sign in:",
            "https://platform.claude.com/oauth/authorize?x=1",
        ],
        prompt="Paste code here if prompted > ",
        after_code=["[pasted code]", "[captured to oauth-token]"],
        files={"/home/worker/.claude/oauth-token": b"not-a-real-value\n"},
    )
    session = LoginSession(harness="claude_code", started_at=0)

    async def paste() -> None:
        await wait_for_state(session, "waiting_for_code")
        assert session.prompt == "Paste code here if prompted >"
        session.submit_code("the-pasted-code#state")

    await run_login(provider, "claude_code", session, during=paste)
    assert session.state == "finished", session.error
    assert session.url == "https://platform.claude.com/oauth/authorize?x=1"
    assert session.token_written is True
    assert session.credential_written is True
    stdin = [b for pod, items in api.exec_stdin.items() if pod.startswith("login-") for b in items]
    assert stdin == [b"the-pasted-code#state\n"]
    everything = json.dumps(api.created)
    assert "the-pasted-code" not in everything
    assert api.harness_secret("crucible-harness-claude_code") == {
        "oauth-token": b"not-a-real-value\n"
    }


# ----- what is and is not stored ------------------------------------------------------


async def test_files_that_fail_acceptance_leave_the_secret_exactly_as_it_was() -> None:
    api, provider = login_provider()
    api.put_harness_secret("crucible-harness-codex", {"auth.json": b"the old session"})
    api.login = FakeLogin(files={"/home/worker/.codex/auth.json": b"{}"})
    session = LoginSession(harness="codex", started_at=0)
    await run_login(
        provider, "codex", session, accept=lambda files: ["auth.json: missing keys ['tokens']"]
    )
    assert session.state == "failed"
    assert session.credential_written is False
    assert "were not stored" in str(session.error) and "missing keys" in str(session.error)
    assert api.harness_secret("crucible-harness-codex") == {"auth.json": b"the old session"}


async def test_a_login_that_wrote_nothing_stores_nothing() -> None:
    api, provider = login_provider()
    api.login = FakeLogin(files={}, exit_code=1)
    session = LoginSession(harness="codex", started_at=0)
    seen: list[dict[str, bytes]] = []

    def accept(files: Mapping[str, bytes]) -> Sequence[str]:
        seen.append(dict(files))
        return ["auth.json: missing"]

    await run_login(provider, "codex", session, accept=accept)
    assert seen == [{}]
    assert session.state == "failed" and session.exit_code == 1
    assert not api.secret_exists("crucible-harness-codex")


async def test_a_nonzero_exit_with_valid_files_stores_them_and_says_so() -> None:
    """AGY's login command ends with a prompt the login Job cannot send to its model
    API. The token it wrote is still a token, as it is on Docker."""
    api, provider = login_provider()
    api.login = FakeLogin(files={"/home/worker/.codex/auth.json": CODEX_AUTH}, exit_code=2)
    session = LoginSession(harness="codex", started_at=0)
    await run_login(provider, "codex", session)
    assert session.state == "failed"
    assert session.credential_written is True
    assert "exited 2" in str(session.error) and "stored in the Secret" in str(session.error)


async def test_cancel_deletes_the_job_and_leaves_the_secret_alone() -> None:
    api, provider = login_provider()
    api.put_harness_secret("crucible-harness-codex", {"auth.json": CODEX_AUTH})
    api.login = FakeLogin(never_exits=True)
    session = LoginSession(harness="codex", started_at=0)

    async def cancel() -> None:
        await wait_for_state(session, "waiting_for_operator")
        await asyncio.sleep(0.05)
        session.cancel_requested = True

    await run_login(provider, "codex", session, during=cancel)
    assert session.state == "failed" and session.error == "login cancelled"
    assert api.harness_secret("crucible-harness-codex") == {"auth.json": CODEX_AUTH}
    assert ("jobs", login_job(api)["metadata"]["name"]) in api.deleted


async def test_a_login_that_never_finishes_times_out_and_is_removed() -> None:
    api, provider = login_provider()
    api.login = FakeLogin(never_exits=True)
    session = LoginSession(harness="codex", started_at=0)
    await run_login(provider, "codex", session, timeout=1)
    assert session.error == "login timed out"
    assert not [n for n in api.object_names("jobs") if n.startswith("login-")]
    assert not api.object_names("networkpolicies")


async def test_no_login_job_on_a_namespace_that_failed_its_readiness_probe() -> None:
    api, provider = login_provider(egress_enforced=False)
    session = LoginSession(harness="codex", started_at=0)
    await run_login(provider, "codex", session)
    assert session.state == "failed"
    assert "not ready" in str(session.error)
    assert not created(api, "jobs", "login-")


# ----- the login is a lock another process can see (12) ---------------------------


async def test_a_running_login_is_listed_and_refuses_the_seeding_of_that_harness() -> None:
    api, provider = login_provider()
    api.put_harness_secret("crucible-harness-codex", {"auth.json": CODEX_AUTH})
    api.login = FakeLogin(never_exits=True)
    session = LoginSession(harness="codex", started_at=0)
    seen: list[frozenset[str]] = []

    async def while_running() -> None:
        await wait_for_state(session, "waiting_for_operator")
        seen.append(await provider.logins_in_progress())
        launch = spec(harness="codex", image=WORKER)
        with pytest.raises(ProviderError, match="a login for codex is running"):
            await provider.prepare(launch)
        session.cancel_requested = True

    await run_login(provider, "codex", session, during=while_running)
    assert seen == [frozenset({"codex"})]
    assert await provider.logins_in_progress() == frozenset()


# ----- the service-owned Secret (ADR 0015) -------------------------------------------


def test_the_service_creates_the_secret_and_then_replaces_it_whole() -> None:
    api, provider = login_provider()
    first = provider.write_credential_files(
        "claude_code", {"oauth-token": b"a\n", ".claude.json": b"{}"}
    )
    assert first == {
        "secret": "crucible-harness-claude_code",
        "created": True,
        "files": [".claude.json", "oauth-token"],
    }
    second = provider.write_credential_files("claude_code", {"oauth-token": b"b\n"})
    assert second["created"] is False
    assert api.harness_secret("crucible-harness-claude_code") == {"oauth-token": b"b\n"}
    labels = api.objects[("secrets", "crucible-harness-claude_code")].body["metadata"]["labels"]
    assert labels[k8sspec.LABEL_MANAGED_BY] == "crucible"


def test_a_gitops_secret_is_taken_over_by_the_first_service_write() -> None:
    api, provider = login_provider()
    api.put_harness_secret("crucible-harness-codex", {"auth.json": b"old", "stray": b"x"})
    provider.write_credential_files("codex", {"auth.json": CODEX_AUTH})
    body = api.objects[("secrets", "crucible-harness-codex")].body
    assert set(body["data"]) == {"auth.json"}
    assert body["metadata"]["labels"][k8sspec.LABEL_CREDENTIAL] == "codex"


def test_only_declared_auth_files_are_ever_written() -> None:
    _api, provider = login_provider()
    with pytest.raises(ProviderError, match="not auth files"):
        provider.write_credential_files("codex", {"config.toml": b"x"})


def test_read_credential_files_maps_keys_back_to_auth_file_names() -> None:
    api, provider = login_provider()
    assert provider.read_credential_files("agy") is None
    api.put_harness_secret(
        "crucible-harness-agy", {"antigravity-cli_antigravity-oauth-token": b"{}"}
    )
    assert provider.read_credential_files("agy") == {
        "antigravity-cli/antigravity-oauth-token": b"{}"
    }


async def test_an_optional_credential_is_available_by_its_secret_without_a_mapping() -> None:
    _api, provider = login_provider()
    assert "hermes" not in provider.config.credential_secrets
    assert await provider.credential_available("hermes") is False
    provider.write_credential_files("hermes", {"api-key": b"not-a-real-key\n"})
    assert await provider.credential_available("hermes") is True
    assert await provider.credential_available("claude_code") is True
    assert await provider.credential_available("script-harness") is False


async def test_sync_back_marks_the_secret_as_the_services_own() -> None:
    api, provider = login_provider()
    api.put_harness_secret("crucible-harness-codex", {"auth.json": CODEX_AUTH})
    launch = spec(harness="codex", image=WORKER)
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    claim = api.claims[k8sspec.object_name("ws", launch.attempt_id)]
    newer = json.loads(CODEX_AUTH)
    newer["last_refresh"] = "2026-09-25T00:00:00Z"
    claim["credential/auth.json"] = json.dumps(newer).encode()
    for _ in range(5):
        await provider.observe(handle)
    outputs = await provider.collect(handle, workspace, launch)
    assert outputs.credential_sync is not None
    assert [f.synced for f in outputs.credential_sync.files] == [True]
    labels = api.objects[("secrets", "crucible-harness-codex")].body["metadata"]["labels"]
    assert labels[k8sspec.LABEL_MANAGED_BY] == "crucible"


# ----- the probe (25) ---------------------------------------------------------------


def probe_request(harness: str = "codex") -> ProbeRequest:
    return ProbeRequest(
        harness=harness,
        image=WORKER,
        argv=("/usr/local/bin/codex", "exec", "Reply OK"),
        identity_text="# Probe\n\nReply OK\n",
        timeout_seconds=5,
        policy={
            "resources": {"cpus": 2, "memory": "3GiB"},
            "network": {"mode": "egress-proxy", "egress_allowlist": []},
            "images": {"allowlist": [WORKER]},
        },
    )


async def test_the_probe_runs_a_worker_on_the_secret_and_removes_everything() -> None:
    api, provider = login_provider()
    api.put_harness_secret("crucible-harness-codex", {"auth.json": CODEX_AUTH})
    result = await provider.probe_credential(probe_request())
    assert result.exit_code == 0, result
    assert result.harness_version == "0.156.0"
    assert result.image_digest.startswith("crucible-worker@sha256:")
    assert result.credential_sync is not None
    assert [(f.name, f.present, f.changed) for f in result.credential_sync.files] == [
        ("auth.json", True, False)
    ]
    assert result.credential_sync.removed
    worker = created(api, "jobs", "worker-probe")[0]
    assert worker["metadata"]["labels"][k8sspec.LABEL_ADMIN] == k8sspec.ADMIN_PROBE
    policy = created(api, "networkpolicies", "np-worker-probe")[0]
    assert "api.openai.com" in policy["metadata"]["annotations"][k8sspec.ANNOTATION_EGRESS]
    identity = created(api, "configmaps", "identity-probe")[0]
    assert set(identity["data"]) == {"IDENTITY.md", "harness__config.toml"}
    for kind in ("jobs", "pods", "networkpolicies", "configmaps", "persistentvolumeclaims"):
        assert not [n for n in api.object_names(kind) if "probe" in n], kind
    assert api.object_names("secrets") == ["crucible-harness-codex"]


async def test_a_probe_with_no_secret_is_refused_and_leaves_nothing() -> None:
    api, provider = login_provider()
    with pytest.raises(ProviderError, match="not readable"):
        await provider.probe_credential(probe_request())
    for kind in ("jobs", "pods", "configmaps", "persistentvolumeclaims", "secrets"):
        assert not api.object_names(kind), kind


async def test_the_supervisor_neither_sweeps_nor_adopts_a_probe_in_flight() -> None:
    api, provider = login_provider()
    api.put_harness_secret("crucible-harness-codex", {"auth.json": CODEX_AUTH})
    api.script_all("hang")
    other = login_provider()[1]
    other.client = provider.client
    result: dict[str, Any] = {}

    async def probe() -> None:
        try:
            result["probe"] = await provider.probe_credential(probe_request())
        except Exception as exc:  # pragma: no cover - reported below
            result["error"] = exc

    task = asyncio.create_task(probe())
    for _ in range(400):
        if [n for n in api.object_names("jobs") if n.startswith("worker-probe")]:
            break
        await asyncio.sleep(0.01)
    assert await other.retention([]) == 0
    assert await other.reconcile() == []
    assert [n for n in api.object_names("persistentvolumeclaims") if "probe" in n]
    await asyncio.wait_for(task, 20)
    assert result["probe"].timed_out


async def test_retention_removes_a_login_policy_whose_job_is_gone() -> None:
    api, provider = login_provider()
    labels = {k8sspec.LABEL_ROLE: k8sspec.ROLE_LOGIN}
    for login_id, created_at in (
        ("login01", "2026-09-01T00:00:00Z"),  # its Job is gone: swept
        ("login02", "2026-09-01T00:00:00Z"),  # its Job still runs: kept
        ("login03", ""),  # just created, its Job not yet: kept
    ):
        api.create(
            "networkpolicies",
            {
                "metadata": {
                    "name": f"np-login-{login_id}",
                    "labels": {**labels, k8sspec.LABEL_LOGIN: login_id},
                    "creationTimestamp": created_at,
                }
            },
        )
    api.create(
        "jobs",
        {
            "metadata": {
                "name": "login-codex-login02",
                "labels": {**labels, k8sspec.LABEL_LOGIN: "login02"},
            }
        },
    )
    assert await provider.retention([]) == 1
    assert api.object_names("networkpolicies") == ["np-login-login02", "np-login-login03"]


# ----- the transport ---------------------------------------------------------------------


def test_a_client_frame_is_masked_binary_and_decodes_to_the_channel_and_bytes() -> None:
    payload = bytes([0]) + b"ABCD-EFGH\n"
    frame = _client_frame(payload)
    assert frame[0] == 0x82
    assert frame[1] & 0x80 and frame[1] & 0x7F == len(payload)
    mask = frame[2:6]
    decoded = bytes(b ^ mask[i % 4] for i, b in enumerate(frame[6:]))
    assert decoded == payload
    assert b"ABCD-EFGH" not in frame
    big = _client_frame(b"x" * 300)
    assert big[1] & 0x7F == 126 and int.from_bytes(big[2:4], "big") == 300


# ----- the driver the login Pod runs -----------------------------------------------------


@pytest.mark.skipif(
    shutil.which("script") is None or shutil.which("bash") is None,
    reason="needs bash and util-linux script, which every Debian worker image carries",
)
def test_the_driver_masks_the_token_and_the_pasted_code_and_reports_the_exit(
    tmp_path: Path,
) -> None:
    cli = tmp_path / "fake-cli"
    cli.write_text(
        "#!/bin/bash\n"
        "echo 'Use the url below to sign in:'\n"
        "printf '\\033[1mhttps://example.invalid/oauth?x=1\\033[0m\\n'\n"
        "printf 'Paste code here if prompted > '\n"
        "IFS= read -r code\n"
        "echo\n"
        'echo "got ${#code} characters"\n'
        "echo 'sk-ant-oat01-ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'\n"
        "printf 'spinner 1\\rspinner 2\\rdone\\n'\n"
        "exit 3\n"
    )
    cli.chmod(0o755)
    login_dir = tmp_path / "login"
    control = Path("/tmp/crucible-login")
    if control.exists():
        pytest.skip("another login driver owns /tmp/crucible-login on this host")
    env = {
        **os.environ,
        "CRUCIBLE_LOGIN_DIR": str(login_dir),
        "CRUCIBLE_LOGIN_TOKEN_PATTERN": FLOWS["claude_code"].token_pattern,
        "CRUCIBLE_LOGIN_TOKEN_FILE": "oauth-token",
        "TERM": "xterm",
    }
    process = subprocess.Popen(
        ["bash", "-c", _LOGIN_DRIVER, "crucible-login", str(cli)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    output = bytearray()
    lock = threading.Lock()

    def pump() -> None:
        stream = process.stdout
        assert stream is not None
        descriptor = stream.fileno()
        while chunk := os.read(descriptor, 4096):
            with lock:
                output.extend(chunk)

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        deadline = time.monotonic() + 20
        pasted = False
        while time.monotonic() < deadline:
            with lock:
                seen = bytes(output)
            if not pasted and b"Paste code here" in seen:
                subprocess.run(
                    ["sh", "-c", _LOGIN_CODE_SCRIPT], input=b"MY-PASTED-CODE\n", check=True
                )
                pasted = True
            if b"crucible-login.exit=" in seen:
                break
            time.sleep(0.1)
    finally:
        process.kill()
        process.wait()
        shutil.rmtree(control, ignore_errors=True)
    text = bytes(output).decode()
    assert "https://example.invalid/oauth?x=1\n" in text
    assert "Paste code here if prompted > \n" in text
    assert "[captured to oauth-token]" in text
    assert "[pasted code]" in text
    assert "got 14 characters" in text
    assert "\ndone\n" in text
    assert "crucible-login.exit=3" in text
    assert "sk-ant-" not in text and "MY-PASTED-CODE" not in text
    assert "\x1b" not in text
    token = login_dir / "oauth-token"
    assert token.read_text().startswith("sk-ant-oat01-")
    assert token.stat().st_mode & 0o777 == 0o600
