"""Failures of the model transport or container runtime, outside worker control."""

from __future__ import annotations

from dataclasses import dataclass

from crucible.domain.exit_class import ExitClass

START_FAILURES = frozenset(
    {
        "StartError",
        "RunContainerError",
        "ContainerCannotRun",
        "InvalidImageName",
        "ErrImageNeverPull",
        "ImageInspectError",
        "PostStartHookError",
        "CreateContainerError",
        "CreateContainerConfigError",
        "ImagePullBackOff",
        "ErrImagePull",
    }
)


@dataclass(frozen=True)
class Interruption:
    message: str
    capacity: bool = False
    quota: bool = False

    @property
    def exit_class(self) -> ExitClass:
        return ExitClass.QUOTA_EXHAUSTED if self.quota else ExitClass.INFRASTRUCTURE

