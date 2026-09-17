"""The e2e script harness (18) as the fourth adapter.

A script implementing the launch contract: it reads the identity bundle, does what the
repository's `e2e-behavior` file asks, writes a CompletionClaimV1, and exits with the
code the case wants. No model, no credential, no subscription, so the whole provider
path can be proven in CI. It lived inside the execution adapter in C3; it is behind the
port now so the registry has one shape for every harness (07).
"""

from __future__ import annotations

from pathlib import Path

from crucible.adapters.harness import base
from crucible.domain.exit_class import ExitClass
from crucible.ports.harness import (
    AdapterLaunch,
    CredentialSpec,
    ExitInfo,
    HarnessCapabilities,
    LaunchContext,
    ParsedReport,
    ReportMetrics,
    TranscriptFormat,
    VersionRange,
)

NAME = "script-harness"


class ScriptHarnessAdapter:
    name = NAME
    supported_versions = VersionRange("1.0.0", "2.0.0")

    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            prompt_on_stdin=False,
            model_flag=False,
            effort_flag=False,
            transcript_format=TranscriptFormat.NONE,
            endpoints=(),
            shim=None,
        )

    def credential_spec(self) -> CredentialSpec | None:
        return None

    def build_launch(self, ctx: LaunchContext) -> AdapterLaunch:
        return AdapterLaunch(argv=("crucible-script-harness",), workdir=ctx.repo_mount)

    def parse_report(self, report_dir: Path, exit: ExitInfo) -> ParsedReport:
        return base.parse_report_dir(report_dir, exit, metrics=ReportMetrics(), transcript_lines=0)

    def classify_exit(self, exit: ExitInfo, stdout_tail: str, stderr_tail: str) -> ExitClass:
        return base.classify_with_patterns(exit, stdout_tail, stderr_tail, auth=(), quota=())
