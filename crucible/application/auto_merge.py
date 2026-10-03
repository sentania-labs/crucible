"""The live deployment switch for automatic delivery merges."""

from crucible.domain.entities import CICertification
from crucible.ports.repository import UnitOfWork

SETTING_NAME = "delivery.auto_merge"


def auto_merge_enabled(uow: UnitOfWork) -> bool:
    setting = uow.provider_settings.get(SETTING_NAME)
    return setting is None or setting.document.get("enabled", True) is True


def certified_jobs_green(certification: CICertification | None) -> bool:
    """Automatic merges require every eligible job, even if policy narrows CI gates."""
    if certification is None or certification.state != "green":
        return False
    jobs = [
        run
        for run in certification.check_runs
        if run.get("source") != "check_suite"
        and not (run.get("status") == "completed" and run.get("conclusion") == "skipped")
    ]
    return bool(jobs) and all(
        run.get("status") == "completed" and run.get("conclusion") == "success" for run in jobs
    )
