"""Programmatic Alembic: `crucible-admin migrate` applies; serve refuses if not at head (14)."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

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


def is_current(engine: Engine, url: str) -> tuple[bool, str]:
    head = head_revision(url)
    current = current_revision(engine)
    if head is None:
        return False, "no migrations found"
    if current != head:
        return False, f"database at {current or 'empty'}, head is {head}"
    return True, f"at head {head}"
