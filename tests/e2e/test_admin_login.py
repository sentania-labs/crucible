"""The C7a interactive login path against a real daemon and stub CLI."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from crucible.adapters.execution.docker import DockerConfig, DockerProvider
from crucible.adapters.harness.script import ScriptHarnessAdapter
from crucible.application.admin.login import LoginFlow, LoginSession
from crucible.application.harnesses import HarnessRegistry
from crucible.ports.harness import AuthFile, CredentialSpec, MountMode
from tests.e2e import daemon

pytestmark = pytest.mark.e2e


class LoginStubAdapter(ScriptHarnessAdapter):
    def credential_spec(self) -> CredentialSpec:
        return CredentialSpec(
            harness=self.name,
            mount_target="/home/worker/.crucible-login",
            auth_files=(AuthFile("session.json", json=True),),
            minimum_mode=MountMode.RW_NARROW,
            config_dir_env="CRUCIBLE_LOGIN_DIR",
        )


def test_stub_device_login_uses_one_credential_mount_and_reaps_container(
    docker_config: DockerConfig,
    artifact_root: Path,
    worker_image: str,
) -> None:
    credential_root = artifact_root / "credentials"
    selected = credential_root / "script-harness"
    selected.mkdir(parents=True, exist_ok=True)
    credential_root.chmod(0o777)
    selected.chmod(0o777)
    provider = DockerProvider(
        replace(docker_config, credential_volume=""),
        harnesses=HarnessRegistry((LoginStubAdapter(),)),
    )
    flow = LoginFlow(
        harness="script-harness",
        argv=("crucible-script-harness", "login-stub"),
        image_binary="/usr/local/bin/crucible-script-harness",
        directory_env="CRUCIBLE_LOGIN_DIR",
        directory_subdir="",
        pastes_code=False,
        captures_token=False,
        token_pattern="",
        token_file="",
        window="stub device flow",
    )
    session = LoginSession(harness=flow.harness, started_at=0)
    before_containers = set(daemon.container_ids("crucible.role=login"))
    workspace_root = artifact_root / "workspaces"
    before_workspaces = set(workspace_root.iterdir()) if workspace_root.is_dir() else set()

    asyncio.run(
        provider.run_login_container(
            flow=flow,
            image=worker_image,
            directory=str(selected),
            session=session,
            argv=flow.argv,
            timeout=30,
        )
    )

    assert session.state == "finished", f"{session.error}: {session.lines}"
    assert session.url == "https://example.invalid/device"
    assert session.code == "C7AA-TEST"
    assert (selected / "session.json").read_text(encoding="utf-8") == ('{"authenticated":true}\n')
    after_workspaces = set(workspace_root.iterdir()) if workspace_root.is_dir() else set()
    assert after_workspaces == before_workspaces, "a login must not receive a workspace"
    assert set(daemon.container_ids("crucible.role=login")) == before_containers
