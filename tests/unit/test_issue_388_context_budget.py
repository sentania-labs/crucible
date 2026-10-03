"""Issue 388: Hermes budgets its context with the response allowance the gateway enforces.

The routing entry's thinking setting and the Local gateway page's response allowance
reach the Hermes wrapper beside the context length; the wrapper writes the allowance
into Hermes's per-run config as `model.max_tokens`, so its compressor reserves it and its
requests carry it. The attempt records the three effective settings once and keeps them,
and a lower allowance, a retry's or Hermes's own one-call cap, is what is sent."""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import signal
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from crucible.adapters.harness.hermes import HermesAdapter
from crucible.adapters.harness.registry import default_registry
from crucible.application.supervisor import Supervisor
from crucible.contracts.evidence import EvidenceKind
from crucible.domain.entities import EvidenceRecord, ExecutionRole
from crucible.domain.harness_settings import hermes_limit_problems
from crucible.ports.harness import CredentialSource, LaunchContext
from tests.unit.test_class_routing import NOW
from tests.unit.test_codex_local import routing
from tests.unit.test_hermes_wrapper import stand_in_release

WRAPPER = Path(__file__).resolve().parents[2] / "images" / "worker" / "crucible-hermes.py"
HERMES_PYTHON = Path("/opt/hermes/bin/python")
URL = "http://gateway.lab.test:4000/v1"
WINDOW = 131_072
ALLOWANCE = 32_000

# Hermes 0.19.0, agent/context_compressor.py: MINIMUM_CONTEXT_LENGTH, the default
# trigger percent, and the raise-only floor for windows under 512K.
MINIMUM_CONTEXT_LENGTH = 64_000
DEFAULT_THRESHOLD_PERCENT = 0.50
SMALL_WINDOW_LIMIT = 512_000
SMALL_WINDOW_PERCENT = 0.75
MIN_TRIGGER_RATIO = 0.85


def compression_trigger_copy(context_length: int, max_tokens: int | None) -> int:
    """ContextCompressor._compute_threshold_tokens with _effective_threshold_percent,
    as Hermes 0.19.0 has them: the trigger is a share of the window less the output
    reservation, and no max_tokens means no reservation."""
    percent = DEFAULT_THRESHOLD_PERCENT
    if context_length and context_length < SMALL_WINDOW_LIMIT:
        percent = max(percent, SMALL_WINDOW_PERCENT)
    effective = context_length - (max_tokens or 0)
    if effective <= 0:
        effective = context_length
    floored = max(int(effective * percent), MINIMUM_CONTEXT_LENGTH)
    if effective > 0 and floored >= effective:
        return max(1, min(int(effective * MIN_TRIGGER_RATIO), effective - 1))
    return floored


def _wrapper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("crucible_hermes_388", WRAPPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _context(**kw: Any) -> LaunchContext:
    return LaunchContext(
        attempt_id="attempt",
        model="coder",
        effort=None,
        timeout_seconds=600,
        identity_mount="/crucible/identity",
        report_mount="/crucible/report",
        repo_mount="/crucible/repo",
        endpoint="local",
        endpoint_url=URL,
        **kw,
    )


def _config(home: Path) -> dict[str, int]:
    """The `model:` block the wrapper writes, one `key: whole number` per line."""
    lines = (home / "config.yaml").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "model:"
    return {key.strip(): int(value) for key, value in (line.split(":", 1) for line in lines[1:])}


# A stand-in for the two Hermes 0.19.0 modules the bootstrap touches. The agent keeps
# what it was built with, and `request` puts a request's max_tokens together the way
# Hermes 0.19.0's chat-completions transport does: its one-call cap first, then the
# configured max_tokens, then the request overrides over the top.
_STAND_IN_AGENT = """
class AIAgent:
    def __init__(self, base_url=None, api_key=None, provider=None, api_mode=None,
                 acp_command=None, acp_args=None, command=None, args=None, model="",
                 max_iterations=90, tool_delay=1.0, max_tokens=None,
                 request_overrides=None):
        self.max_iterations = max_iterations
        self.max_tokens = max_tokens
        self.request_overrides = dict(request_overrides or {})
        self._ephemeral_max_output_tokens = None

    def request(self):
        kwargs = {}
        ephemeral = self._ephemeral_max_output_tokens
        self._ephemeral_max_output_tokens = None
        if ephemeral is not None:
            kwargs["max_tokens"] = ephemeral
        elif self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        for key, value in self.request_overrides.items():
            kwargs[key] = value
        return kwargs
"""
_STAND_IN_MAIN = """
import json
import os
import sys


def main():
    import run_agent

    home = os.environ["HERMES_HOME"]
    with open(os.path.join(home, "config.yaml"), encoding="utf-8") as handle:
        config = handle.read()
    # Hermes reads model.max_tokens when the caller passed none (agent_init, 0.19.0).
    configured = None
    if "max_tokens:" in config:
        configured = int(config.split("max_tokens:")[1].split()[0])
    agent = run_agent.AIAgent(model="m", max_tokens=configured)
    first = agent.request()
    # The gateway refused the allowance for this prompt: Hermes retries the one call
    # with the lower cap the error left room for, then goes back to its setting.
    agent._ephemeral_max_output_tokens = 20000
    retry = agent.request()
    after = agent.request()
    # The length-retry boost: Hermes retries a cut-off reply with a boosted cap.
    agent._ephemeral_max_output_tokens = 32768
    boost = agent.request()
    template = {"chat_template_kwargs": {"enable_thinking": False}}
    named = run_agent.AIAgent(model="m", request_overrides={"extra_body": template})
    with open(os.environ["STAND_IN_OUT"], "w", encoding="utf-8") as handle:
        json.dump(
            {
                "argv": sys.argv[1:],
                "max_iterations": agent.max_iterations,
                "first": first,
                "retry": retry,
                "after": after,
                "boost": boost,
                "named": named.request(),
            },
            handle,
        )
    return 0
"""


def _stand_in_hermes(root: Path, version: str = "0.19.0") -> Path:
    hermes = root / "hermes"
    (hermes / "hermes_cli").mkdir(parents=True)
    (hermes / "tools").mkdir(parents=True)
    (hermes / "run_agent.py").write_text(_STAND_IN_AGENT, encoding="utf-8")
    (hermes / "hermes_cli" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "hermes_cli" / "main.py").write_text(_STAND_IN_MAIN, encoding="utf-8")
    (hermes / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (hermes / "tools" / "file_operations.py").write_text(
        "class ShellFileOperations:\n"
        "    def _search_with_grep(\n"
        "        self, pattern, path, file_glob, limit, offset, output_mode, context\n"
        "    ):\n"
        '        cmd_parts = ["grep", "-rnH"]\n'
        '        cmd_parts.append("--exclude-dir=\x27.*\x27")\n'
        "        cmd_parts.append(self._escape_shell_arg(path))\n"
        '        cmd_parts.extend(["|", "head", "-n", str(fetch_limit)])\n'
        '        cmd = "set -o pipefail; " + " ".join(cmd_parts)\n'
        "        pass\n"
        "\n"
        "    def _exec(self, command, *args, **kwargs):\n"
        "        pass\n"
        "\n"
        "    def _escape_shell_arg(self, arg):\n"
        "        pass\n",
        encoding="utf-8",
    )
    stand_in_release(hermes, version)
    return hermes


def _run_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    *,
    version: str = "0.19.0",
) -> tuple[int, Path]:
    """The wrapper's own main(), with the adapter's environment, against the stand-in."""
    hermes = _stand_in_hermes(tmp_path, version)
    home = tmp_path / "home"
    wrapper = _wrapper()
    monkeypatch.setattr(wrapper, "HERMES_PYTHON", sys.executable)
    monkeypatch.setattr(wrapper, "PROGRESS_SECONDS", 3600.0)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("CRUCIBLE_HERMES_USAGE", str(tmp_path / "usage.json"))
    monkeypatch.delenv("CRUCIBLE_HERMES_IDENTITY", raising=False)
    monkeypatch.setenv("PYTHONPATH", str(hermes))
    monkeypatch.setenv("STAND_IN_OUT", str(tmp_path / "out.json"))
    monkeypatch.setattr(sys, "argv", ["crucible-hermes", "-z", "prompt"])
    # main() forwards these to Hermes; the test process gets its own handlers back.
    handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        return int(wrapper.main()), home
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)


def test_the_wrapper_config_carries_max_tokens_from_the_adapter_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = HermesAdapter().build_launch(_context())
    assert launch.env["CRUCIBLE_HERMES_CONTEXT_LENGTH"] == str(WINDOW)
    assert launch.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == str(ALLOWANCE)
    assert "CRUCIBLE_HERMES_THINKING" not in launch.env
    code, home = _run_wrapper(tmp_path, monkeypatch, dict(launch.env))
    assert code == 0
    assert _config(home) == {"context_length": WINDOW, "max_tokens": ALLOWANCE}
    out = json.loads((tmp_path / "out.json").read_text())
    assert out["argv"] == ["-z", "prompt"]
    assert out["max_iterations"] == 300
    # Every request carries the allowance, and the routing entry's thinking setting.
    assert out["first"] == {
        "max_tokens": ALLOWANCE,
    }


def test_the_compressor_trigger_reserves_the_allowance() -> None:
    """The numbers in the issue: without max_tokens Hermes compresses at 98304 input
    tokens, 768 under the 99072 the gateway accepts beside a 32000 reservation; with it,
    at 74304."""
    ceiling = WINDOW - ALLOWANCE
    assert ceiling == 99_072
    assert compression_trigger_copy(WINDOW, None) == 98_304
    assert ceiling - compression_trigger_copy(WINDOW, None) == 768
    assert compression_trigger_copy(WINDOW, ALLOWANCE) == 74_304
    assert ceiling - compression_trigger_copy(WINDOW, ALLOWANCE) == 24_768


def test_the_trigger_from_the_written_config_is_the_lower_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = _wrapper()
    monkeypatch.setenv("CRUCIBLE_HERMES_CONTEXT_LENGTH", str(WINDOW))
    monkeypatch.setenv("CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS", str(ALLOWANCE))
    wrapper.write_settings(
        tmp_path,
        wrapper._limit("CRUCIBLE_HERMES_CONTEXT_LENGTH"),
        wrapper._limit("CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"),
    )
    config = _config(tmp_path)
    assert compression_trigger_copy(config["context_length"], config.get("max_tokens")) == 74_304


@pytest.mark.skipif(not HERMES_PYTHON.exists(), reason="needs the worker image's Hermes")
def test_hermes_itself_agrees_where_it_is_installed(tmp_path: Path) -> None:
    """In the worker image (or anywhere the Hermes venv is), Hermes's own compressor
    built from the written config gives the same trigger, and its requests carry the
    allowance, then the lower one-call cap on a retry."""
    wrapper = _wrapper()
    wrapper.write_settings(tmp_path, WINDOW, ALLOWANCE)
    probe = wrapper.BOOTSTRAP.split('sys.argv = ["hermes"')[0] + (
        "from hermes_cli.runtime_provider import resolve_runtime_provider\n"
        "from run_agent import AIAgent\n"
        "from agent.chat_completion_helpers import build_api_kwargs\n"
        "r = resolve_runtime_provider(requested='openai-api', target_model='coder')\n"
        "a = AIAgent(api_key=r['api_key'], base_url=r['base_url'], provider=r['provider'],\n"
        "    api_mode=r.get('api_mode'), model='coder', quiet_mode=True, platform='cli',\n"
        "    enabled_toolsets=['terminal', 'file'])\n"
        "first = build_api_kwargs(a, [{'role': 'user', 'content': 'hi'}])\n"
        "a._ephemeral_max_output_tokens = 20000\n"
        "retry = build_api_kwargs(a, [{'role': 'user', 'content': 'hi'}])\n"
        "print(a.context_compressor.threshold_tokens, first.get('max_tokens'),\n"
        "    retry.get('max_tokens'), first.get('extra_body'))\n"
    )
    result = subprocess.run(
        [str(HERMES_PYTHON), "-P", "-c", probe],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        timeout=120,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HERMES_HOME": str(tmp_path),
            "OPENAI_BASE_URL": "http://127.0.0.1:9/v1",
            "OPENAI_API_KEY": "local-no-auth",
            "CRUCIBLE_HERMES_THINKING": "on",
        },
    )
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == (
        "74304 32000 20000 {'chat_template_kwargs': {'enable_thinking': True}}"
    )


def test_a_lower_allowance_on_retry_is_what_hermes_sends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hermes's one-call lower cap outranks model.max_tokens; the wrapper never puts
    max_tokens in the request overrides, where it would outrank that cap, and a thinking
    setting the caller named stays."""
    launch = HermesAdapter().build_launch(
        _context(harness_settings={"enable_thinking": True, "max_output_tokens": 24000})
    )
    code, home = _run_wrapper(tmp_path, monkeypatch, dict(launch.env))
    assert code == 0
    assert _config(home)["max_tokens"] == 24000
    out = json.loads((tmp_path / "out.json").read_text())
    thinking = {"chat_template_kwargs": {"enable_thinking": True}}
    assert out["first"] == {"max_tokens": 24000, "extra_body": thinking}
    assert out["retry"] == {"max_tokens": 20000, "extra_body": thinking}
    assert out["after"] == {"max_tokens": 24000, "extra_body": thinking}
    assert out["boost"] == {"max_tokens": 32768, "extra_body": thinking}
    assert out["named"] == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def test_another_hermes_release_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    launch = HermesAdapter().build_launch(_context())
    code, _ = _run_wrapper(tmp_path, monkeypatch, dict(launch.env), version="0.20.0")
    assert code != 0
    assert "refusing to start Hermes unpatched" in capfd.readouterr().err
    assert not (tmp_path / "out.json").exists()


def test_the_allowance_must_leave_the_window_room() -> None:
    assert hermes_limit_problems(300, WINDOW, ALLOWANCE) == []
    assert hermes_limit_problems(300, 0, ALLOWANCE) == []
    assert hermes_limit_problems(300, WINDOW, 100) == [
        "max output tokens must be between 1024 and 1000000"
    ]
    assert hermes_limit_problems(300, 64_000, 40_000) == [
        "max output tokens must be at most half the context length"
    ]


class _Evidence:
    def __init__(self) -> None:
        self.rows: list[EvidenceRecord] = []

    def add(self, evidence: EvidenceRecord) -> EvidenceRecord:
        self.rows.append(evidence)
        return evidence

    def list_for_attempt(self, attempt_id: str) -> list[EvidenceRecord]:
        return [row for row in self.rows if row.attempt_id == attempt_id]


def _supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, saved: dict[str, Any]
) -> tuple[Supervisor, _Evidence]:
    route = routing()
    route.models[0].chat_template_kwargs.enable_thinking = True
    monkeypatch.setattr("crucible.application.supervisor.load_routing", lambda *_: route)
    evidence = _Evidence()
    uow: Any = SimpleNamespace(
        provider_settings=SimpleNamespace(get=lambda _name: SimpleNamespace(document=saved)),
        evidence=evidence,
        commit=lambda: None,
    )
    supervisor = object.__new__(Supervisor)
    supervisor._uow_factory = lambda: nullcontext(uow)  # type: ignore[assignment]
    supervisor._fenced = lambda: nullcontext(uow)  # type: ignore[method-assign, assignment, return-value]
    supervisor.fenced_token = 1
    supervisor._harnesses = default_registry()
    supervisor._clock = SimpleNamespace(now=lambda: NOW)
    (tmp_path / "api-key").write_text("test-key")
    supervisor._credential_sources = {"hermes": CredentialSource(str(tmp_path))}
    return supervisor, evidence


def _attempt(attempt_id: str) -> Any:
    return SimpleNamespace(
        id=attempt_id,
        selected_harness="hermes",
        selected_model="a-hermes",
        selected_image="image",
        resume_from_remote=False,
    )


EXECUTION: Any = SimpleNamespace(
    role=ExecutionRole.IMPLEMENT,
    harness="hermes",
    model="a-hermes",
    image="image",
    policy_snapshot={},
    timeout_seconds=600,
    effort=None,
    provider="docker",
)
TASK: Any = SimpleNamespace(id="task", external_id="FDY-0288", principal_id="tests")


def _recorded(evidence: _Evidence, attempt_id: str) -> list[dict[str, Any]]:
    return [
        row.payload["settings"]
        for row in evidence.list_for_attempt(attempt_id)
        if row.kind == EvidenceKind.LAUNCH_SETTINGS.value and row.verified
    ]


@pytest.mark.asyncio
async def test_the_attempt_records_the_effective_settings_and_keeps_them(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saved: dict[str, Any] = {"max_turns": 400}
    supervisor, evidence = _supervisor(monkeypatch, tmp_path, saved)
    launch = await supervisor._build_spec(_attempt("one"), EXECUTION, TASK, {})
    effective = {
        "max_turns": 400,
        "context_length": WINDOW,
        "max_output_tokens": ALLOWANCE,
        "enable_thinking": True,
    }
    assert _recorded(evidence, "one") == [effective]
    assert launch.harness_settings == effective
    assert launch.env["CRUCIBLE_HERMES_CONTEXT_LENGTH"] == str(WINDOW)
    assert launch.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == str(ALLOWANCE)
    assert launch.env["CRUCIBLE_HERMES_THINKING"] == "on"
    # Saved settings change while the attempt runs; a rebuild of its spec (a collect
    # after a restart) keeps what it launched with and records nothing new.
    saved.update(context_length=98_304, max_output_tokens=16_000)
    again = await supervisor._build_spec(_attempt("one"), EXECUTION, TASK, {})
    assert again.harness_settings == effective
    assert again.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == str(ALLOWANCE)
    assert _recorded(evidence, "one") == [effective]


@pytest.mark.asyncio
async def test_a_retry_with_a_lower_allowance_is_honoured(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    saved: dict[str, Any] = {"max_turns": 300, "context_length": WINDOW}
    supervisor, evidence = _supervisor(monkeypatch, tmp_path, saved)
    first = await supervisor._build_spec(_attempt("first"), EXECUTION, TASK, {})
    assert first.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == str(ALLOWANCE)
    # The caller lowers the allowance and retries: the new attempt gets the lower one,
    # not the default and not the first attempt's.
    saved["max_output_tokens"] = 16_000
    retry = await supervisor._build_spec(_attempt("retry"), EXECUTION, TASK, {})
    assert retry.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == "16000"
    assert _recorded(evidence, "retry")[0]["max_output_tokens"] == 16_000
    assert _recorded(evidence, "first")[0]["max_output_tokens"] == ALLOWANCE
    # The adapter passes what it is given, never raising it back to the default.
    launch = HermesAdapter().build_launch(
        dataclasses.replace(_context(), harness_settings=retry.harness_settings)
    )
    assert launch.env["CRUCIBLE_HERMES_MAX_OUTPUT_TOKENS"] == "16000"
