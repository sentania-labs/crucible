"""The harness test (crucible#118): does this harness work, end to end, right now?

It runs the path a real task takes, in order, and stops at the first step that fails:
the harness is enabled, it has a worker image of its own at a supported version (ADR
0016), its credential is stored, and a worker Pod (or container) runs that image with
the credential under the worker's egress and makes one minimal model call. Each step
is reported in plain words, pass or fail, with the failing step's cause. The last result
is kept on the harness's row for the Harnesses page.

A test is a check, not a change, so it asks for no reason (25, crucible#117). The worker
run is the bounded probe (25) with every harness in a worker, Hermes included, so it is
recorded as the probe is: a `credential_probed` event and the last launch outcome."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from crucible.application.admin import credentials
from crucible.application.admin.context import AdminContext, guard_mutation
from crucible.application.errors import ApplicationError, NotFoundError
from crucible.application.harnesses import HarnessUnavailableError, harness_state
from crucible.domain.exit_class import ExitClass
from crucible.ports.repository import UnitOfWork

ENABLED = "Harness enabled"
IMAGE = "Worker image"
CREDENTIAL = "Credential"
ROUTE = "Model"
WORKER = "Worker starts"
MODEL = "Model call"
STEPS = (ENABLED, IMAGE, CREDENTIAL, ROUTE, WORKER, MODEL)

# What each way a run can end means to the operator, for the model call step.
EXIT_WORDS = {
    ExitClass.AUTH_FAILURE.value: "the model provider refused the credential",
    ExitClass.TIMEOUT.value: "no answer before the time limit",
    ExitClass.QUOTA_EXHAUSTED.value: "the model provider says the quota is used up",
    ExitClass.PROVIDER_ERROR.value: "the model provider or the endpoint returned an error",
    ExitClass.ENVIRONMENT.value: "the worker could not run the harness (environment)",
    ExitClass.CRASHED.value: "the harness exited with an error",
    ExitClass.KILLED.value: "the run was killed",
    ExitClass.LOST.value: "the worker was lost",
}


@dataclass(slots=True)
class _Steps:
    items: list[dict[str, Any]]

    def passed(self, name: str, detail: str) -> None:
        self.items.append({"name": name, "ok": True, "result": "pass", "detail": detail})

    def failed(self, name: str, detail: str) -> dict[str, Any]:
        self.items.append({"name": name, "ok": False, "result": "fail", "detail": detail})
        for later in STEPS[STEPS.index(name) + 1 :]:
            self.items.append({"name": later, "ok": None, "result": "not run", "detail": ""})
        return self.items[-1]


async def test_harness(
    ctx: AdminContext,
    uow: UnitOfWork,
    *,
    principal: str,
    harness: str,
    reason: str | None = None,
) -> dict[str, Any]:
    """Run the steps and return {harness, ok, failed_step, steps, tested_at}."""
    reason = guard_mutation(
        ctx, uow, reason, principal=principal, operation=f"harnesses test {harness}"
    )
    adapter = ctx.harnesses.get(harness)
    if adapter is None:
        raise NotFoundError(f"no adapter declares harness {harness!r}")
    steps = _Steps([])
    await _run(ctx, uow, steps, principal=principal, harness=harness, reason=reason)
    failed = next((s["name"] for s in steps.items if s["ok"] is False), None)
    result = {
        "harness": harness,
        "ok": failed is None,
        "failed_step": failed,
        "steps": steps.items,
        "tested_at": ctx.clock.now().isoformat(),
        "tested_by": principal,
    }
    state = harness_state(uow, ctx.clock, harness)
    state.last_test = result
    state.updated_at = ctx.clock.now()
    uow.harnesses.put(state)
    return result


async def _run(
    ctx: AdminContext,
    uow: UnitOfWork,
    steps: _Steps,
    *,
    principal: str,
    harness: str,
    reason: str,
) -> None:
    adapter = ctx.harnesses.require(harness)
    try:
        ctx.harnesses.resolve(harness, gates=ctx.harness_gates, state=uow.harnesses.get(harness))
    except HarnessUnavailableError as exc:
        steps.failed(ENABLED, f"{exc.reason}; enable it on Harnesses")
        return
    steps.passed(ENABLED, "enabled in configuration and on Harnesses")

    default = uow.harness_images.get(harness)
    if default is None:
        steps.failed(IMAGE, f"no worker image is promoted for {harness}; choose one on Images")
        return
    if not adapter.supported_versions.supports(default.version):
        steps.failed(
            IMAGE,
            f"{default.reference} carries {harness} {default.version}, outside the tested "
            f"range {adapter.supported_versions.text}; promote a supported image on Images",
        )
        return
    steps.passed(IMAGE, f"{default.reference} ({harness} {default.version})")

    if adapter.credential_spec() is None:
        steps.passed(CREDENTIAL, "this harness needs none")
    else:
        view = credentials.state_view(ctx, uow, harness)
        state = str(view.get("state"))
        if state == "absent":
            steps.failed(
                CREDENTIAL,
                f"no API key is stored for {harness}; set the key on Credentials"
                if harness == credentials.HERMES
                else f"no credential is stored for {harness}; log in on Credentials",
            )
            return
        steps.passed(CREDENTIAL, f"stored ({state})")

    try:
        model, endpoint, endpoint_url = credentials.probe_route(uow, adapter, harness)
    except ApplicationError as exc:
        steps.failed(ROUTE, exc.detail or exc.title)
        return
    if model == "none":
        steps.passed(ROUTE, "this harness calls no model")
    elif endpoint == "local":
        steps.passed(ROUTE, f"{model} at {endpoint_url}")
    else:
        steps.passed(ROUTE, f"{model} on the {harness} subscription")

    try:
        record = await credentials.worker_probe(
            ctx, uow, harness=harness, principal=principal, reason=reason
        )
    except ApplicationError as exc:
        steps.failed(WORKER, exc.detail or exc.title)
        return
    if record.cause == "provider_unavailable":
        steps.failed(WORKER, f"the worker did not start: {record.detail}")
        return
    seconds = f"{record.duration_seconds:g} s"
    steps.passed(WORKER, f"ran {record.image_digest or record.image} under the worker's egress")
    if record.exit_class == ExitClass.COMPLETED.value:
        steps.passed(
            MODEL,
            f"the harness ran and exited cleanly in {seconds}"
            if model == "none"
            else f"{model} answered one prompt in {seconds}",
        )
        return
    words = EXIT_WORDS.get(record.exit_class, f"the run ended as {record.exit_class}")
    code = f" (exit code {record.exit_code})" if record.exit_code is not None else ""
    steps.failed(MODEL, f"{words}{code}, after {seconds}")
