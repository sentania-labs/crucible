"""Programmatic Alembic: `crucible-admin migrate` applies; serve refuses if not at head (14)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from crucible.adapters.persistence.models import Base

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.set_main_option("prepend_sys_path", ".")
    cfg.set_main_option("path_separator", "os")
    return cfg


def upgrade(url: str, revision: str = "head") -> None:
    command.upgrade(alembic_config(url), revision)


def downgrade(url: str, revision: str) -> None:
    command.downgrade(alembic_config(url), revision)


def head_revision(url: str) -> str | None:
    script = ScriptDirectory.from_config(alembic_config(url))
    return script.get_current_head()


def current_revision(engine: Engine) -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _include_name(name: str | None, type_: str, _parent: object) -> bool:
    return not (type_ == "table" and name == "alembic_version")


def _describe(diff: object) -> str:
    """One line naming the first difference, from alembic's diff tuples."""
    if isinstance(diff, list | tuple) and diff and isinstance(diff[0], str):
        kind = diff[0]
        subject = diff[-1] if len(diff) > 1 else None
        table = getattr(subject, "table", None)
        name = getattr(subject, "name", None)
        if kind.endswith("column") and table is not None:
            return f"{kind} {table.name}.{name}"
        if kind.endswith("table") and name is not None:
            return f"{kind} {name}"
        if kind.startswith("modify_") and len(diff) >= 4:
            return f"{kind} {diff[2]}.{diff[3]}"
        return f"{kind} {name or subject}"
    return str(diff)


def schema_drift(engine: Engine) -> str | None:
    """Compare the ORM metadata to the live schema; return the first difference or None.

    The revision id alone cannot tell a database whose migration file was edited after
    it was applied from a correct one, so readiness checks the shape as well."""
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "include_name": _include_name}
        )
        diffs = compare_metadata(context, Base.metadata)
    if not diffs:
        return None
    first = diffs[0]
    if isinstance(first, list):
        first = first[0]
    return _describe(first)


def is_current(engine: Engine, url: str) -> tuple[bool, str]:
    """True only when the revision is at head and the live schema matches the ORM."""
    head = head_revision(url)
    current = current_revision(engine)
    if head is None:
        return False, "no migrations found"
    if current != head:
        return False, f"database at {current or 'empty'}, head is {head}"
    drift = schema_drift(engine)
    if drift is not None:
        return False, f"schema drift at head {head}: {drift}"
    return True, f"at head {head}, schema matches"
