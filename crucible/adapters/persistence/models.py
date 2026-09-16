"""SQLAlchemy 2 typed ORM models. The migrations are hand-written; these mirror them."""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

ID = String(26)
TZ = DateTime(timezone=True)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSONB, list[Any]: JSONB}


class PrincipalRow(Base):
    __tablename__ = "principals"
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    role: Mapped[str] = mapped_column(String(32))
    token_salt: Mapped[bytes] = mapped_column(LargeBinary)
    token_hash: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(TZ)
    disabled_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class RepositoryRow(Base):
    __tablename__ = "repositories"
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    url: Mapped[str] = mapped_column(Text)
    default_branch: Mapped[str] = mapped_column(String(255))
    installation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    policy_name: Mapped[str] = mapped_column(String(128))
    registered_by: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(TZ)


class PolicyRow(Base):
    __tablename__ = "policies"
    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ)
    retired_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        UniqueConstraint("principal_id", "external_id", name="uq_tasks_principal_external"),
        Index("ix_tasks_state", "state"),
        Index("ix_tasks_principal_updated", "principal_id", "updated_at"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128))
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    repository_id: Mapped[str] = mapped_column(ID, ForeignKey("repositories.id"))
    project: Mapped[str] = mapped_column(String(128))
    title: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(48))
    contract_version: Mapped[int] = mapped_column(Integer)
    policy_name: Mapped[str] = mapped_column(String(128))
    policy_version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZ)
    updated_at: Mapped[datetime] = mapped_column(TZ)
    closed_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class TaskContractRow(Base):
    __tablename__ = "task_contracts"
    __table_args__ = (UniqueConstraint("task_id", "version", name="uq_task_contracts_version"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime] = mapped_column(TZ)


class ExecutionRow(Base):
    __tablename__ = "executions"
    __table_args__ = (Index("ix_executions_task", "task_id"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    role: Mapped[str] = mapped_column(String(16))
    contract_version: Mapped[int] = mapped_column(Integer)
    harness: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(128))
    effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provider: Mapped[str] = mapped_column(String(32))
    image: Mapped[str] = mapped_column(Text)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    state: Mapped[str] = mapped_column(String(16))
    max_attempts: Mapped[int] = mapped_column(Integer)
    retry_on: Mapped[list[Any]] = mapped_column(JSONB)
    timeout_seconds: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(TZ)
    ended_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)


class AttemptRow(Base):
    __tablename__ = "attempts"
    __table_args__ = (
        UniqueConstraint("execution_id", "number", name="uq_attempts_execution_number"),
        Index("ix_attempts_state", "state"),
        Index("ix_attempts_task", "task_id"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    execution_id: Mapped[str] = mapped_column(ID, ForeignKey("executions.id"))
    task_id: Mapped[str] = mapped_column(ID, ForeignKey("tasks.id"))
    number: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16))
    workspace_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    handle: Mapped[str | None] = mapped_column(Text, nullable=True)
    identity_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    image_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZ)
    started_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    timeout_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    drain_deadline: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    termination_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_task_seq", "task_id", "seq"),)
    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(TZ)
    kind: Mapped[str] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(ID, ForeignKey("tasks.id"), nullable=True)
    execution_id: Mapped[str | None] = mapped_column(ID, ForeignKey("executions.id"), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(ID, ForeignKey("attempts.id"), nullable=True)
    principal: Mapped[str] = mapped_column(String(160))
    verified: Mapped[bool] = mapped_column(Boolean)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class LeaseRow(Base):
    __tablename__ = "leases"
    __table_args__ = (
        UniqueConstraint("kind", "key", name="uq_leases_kind_key"),
        Index("ix_leases_expires", "expires_at"),
    )
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    key: Mapped[str] = mapped_column(String(255))
    holder: Mapped[str] = mapped_column(String(128))
    fenced_token: Mapped[int] = mapped_column(BigInteger)
    expires_at: Mapped[datetime] = mapped_column(TZ)


class CompletionClaimRow(Base):
    __tablename__ = "completion_claims"
    attempt_id: Mapped[str] = mapped_column(ID, ForeignKey("attempts.id"), primary_key=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSONB)
    parsed_ok: Mapped[bool] = mapped_column(Boolean)
    parse_errors: Mapped[list[Any]] = mapped_column(JSONB)


class SupervisorStatusRow(Base):
    __tablename__ = "supervisor_status"
    singleton: Mapped[bool] = mapped_column(Boolean, primary_key=True, default=True)
    holder: Mapped[str | None] = mapped_column(String(128), nullable=True)
    last_tick_at: Mapped[datetime | None] = mapped_column(TZ, nullable=True)
    tick_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB)


class IdempotencyKeyRow(Base):
    __tablename__ = "idempotency_keys"
    __table_args__ = (UniqueConstraint("principal_id", "key", name="uq_idempotency_principal_key"),)
    id: Mapped[str] = mapped_column(ID, primary_key=True)
    principal_id: Mapped[str] = mapped_column(ID, ForeignKey("principals.id"))
    key: Mapped[str] = mapped_column(String(255))
    request_sha256: Mapped[str] = mapped_column(String(64))
    response_status: Mapped[int] = mapped_column(Integer)
    response_body: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TZ)
