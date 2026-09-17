"""C2: gates, claims, evidence, internal review, acceptance, corrections, decisions,
escalations, wakes, artifacts, routing policies, and attempt metrics.

Adds the collected head and the publish-pending flag to tasks, extends the event-kind
constraint, fences gate_results and attempt_metrics, makes review_dispositions
append-only, and seeds the default routing policy of 05b.

Revision ID: 0004_gates_and_acceptance
Revises: 0003_idempotency_reservation
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

revision = "0004_gates_and_acceptance"
down_revision = "0003_idempotency_reservation"
branch_labels = None
depends_on = None

ID = sa.String(26)
TZ = sa.DateTime(timezone=True)

# Adding a kind is a migration (10). Keep this list in step with crucible.domain.events.
EVENT_KINDS = (
    "task_submitted",
    "task_scheduled",
    "task_running",
    "task_reported",
    "task_blocked",
    "task_cancel_requested",
    "task_cancelling",
    "task_cancelled",
    "task_retry_scheduled",
    "transition_rejected",
    "contract_rejected",
    "execution_created",
    "execution_active",
    "execution_succeeded",
    "execution_failed",
    "execution_cancelled",
    "execution_resumed",
    "attempt_created",
    "attempt_preparing",
    "attempt_launching",
    "attempt_running",
    "attempt_timeout_drain",
    "attempt_timeout_kill",
    "attempt_cancel_kill",
    "attempt_terminating",
    "attempt_exited",
    "attempt_lost",
    "attempt_collected",
    "attempt_succeeded",
    "attempt_blocked",
    "attempt_failed",
    "attempt_adopted",
    "report_parsed",
    "report_parse_failed",
    "evidence_recorded",
    "gates_evaluated",
    "gate_result_recorded",
    "artifact_stored",
    "artifact_rejected",
    "task_awaiting_internal_review",
    "review_execution_requested",
    "review_report_recorded",
    "review_report_rejected",
    "task_gates_passed",
    "task_pre_pr_gates_failed",
    "task_awaiting_acceptance",
    "acceptance_recorded",
    "task_accepted",
    "task_rejected",
    "task_publish_pending",
    "task_amended",
    "task_correction_attached",
    "task_closed",
    "decision_recorded",
    "disposition_recorded",
    "escalation_opened",
    "escalation_answered",
    "escalation_closed",
    "wake_created",
    "wake_delivered",
    "wake_delivery_failed",
    "wake_delivery_abandoned",
    "wake_acked",
    "policy_uploaded",
    "routing_policy_uploaded",
    "quota_reserved",
    "quota_exhausted",
    "attempt_metrics_recorded",
    "supervisor_lease_acquired",
    "supervisor_lease_released",
    "orphan_removed",
    "principal_created",
    "repository_registered",
)

# 05b defaults. Foundry selects from this; Crucible only validates against it.
DEFAULT_ROUTING = {
    "schema_version": "1.0",
    "name": "default-routing",
    "version": 1,
    "tiers": {
        "trivial": {"allowed_capability": ["small", "mid"], "prefer": ["small"]},
        "standard": {"allowed_capability": ["mid", "small"], "prefer": ["mid"]},
        "complex": {"allowed_capability": ["frontier", "mid"], "prefer": ["frontier"]},
    },
    "models": [
        {
            "id": "claude-fable-5-1",
            "harness": "claude_code",
            "endpoint": "subscription",
            "capability": "frontier",
            "cost": "high",
            "speed": "medium",
            "pool": "anthropic-sub",
            "weight": 1,
            "enabled": True,
        },
        {
            "id": "claude-sonnet-5",
            "harness": "claude_code",
            "endpoint": "subscription",
            "capability": "mid",
            "cost": "medium",
            "speed": "fast",
            "pool": "anthropic-sub",
            "weight": 2,
            "enabled": True,
        },
        {
            "id": "gpt-5-codex",
            "harness": "codex",
            "endpoint": "subscription",
            "capability": "frontier",
            "cost": "high",
            "speed": "medium",
            "pool": "openai-sub",
            "weight": 1,
            "enabled": True,
        },
        {
            "id": "gpt-5-codex-mini",
            "harness": "codex",
            "endpoint": "subscription",
            "capability": "mid",
            "cost": "medium",
            "speed": "fast",
            "pool": "openai-sub",
            "weight": 2,
            "enabled": True,
        },
        {
            "id": "gemini-3-pro",
            "harness": "agy",
            "endpoint": "subscription",
            "capability": "mid",
            "cost": "medium",
            "speed": "fast",
            "pool": "google-sub",
            "weight": 2,
            "enabled": True,
        },
        {
            "id": "local-spark-large",
            "harness": "codex",
            "endpoint": "local",
            "endpoint_url": "http://spark.example.internal:8000/v1",
            "capability": "mid",
            "cost": "none",
            "speed": "medium",
            "pool": "local-spark",
            "weight": 3,
            "enabled": False,
        },
        {
            "id": "local-rtx-small",
            "harness": "codex",
            "endpoint": "local",
            "endpoint_url": "http://rtx.example.internal:8000/v1",
            "capability": "small",
            "cost": "none",
            "speed": "fast",
            "pool": "local-rtx",
            "weight": 3,
            "enabled": False,
        },
    ],
    "pools": {
        "anthropic-sub": {"window": "5h", "budget_units": "tokens_out", "soft_limit": 0},
        "openai-sub": {"window": "5h", "budget_units": "tokens_out", "soft_limit": 0},
        "google-sub": {"window": "24h", "budget_units": "tokens_out", "soft_limit": 0},
        "local-spark": {"window": "1h", "budget_units": "attempts", "soft_limit": 0},
        "local-rtx": {"window": "1h", "budget_units": "attempts", "soft_limit": 0},
    },
    "rotation": {
        "strategy": "weighted-least-recent",
        "quality_feedback": True,
        "quality_window": 10,
    },
}

NEW_FENCED_TABLES = ("gate_results", "attempt_metrics", "evidence")
NEW_APPEND_ONLY_TABLES = ("review_dispositions",)
C2_TABLES = (
    "attempt_metrics",
    "wakes",
    "review_dispositions",
    "decisions",
    "escalations",
    "acceptance_results",
    "gate_results",
    "review_reports",
    "evidence",
    "artifacts",
    "routing_policies",
)


def upgrade() -> None:
    op.add_column("tasks", sa.Column("head_sha", sa.String(64), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("publish_pending", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.alter_column("tasks", "publish_pending", server_default=None)

    op.drop_constraint("ck_events_kind", "events", type_="check")
    # The new list is a strict superset of the old one, so every existing row already
    # satisfies it. NOT VALID skips the full scan and the ACCESS EXCLUSIVE lock it would
    # take on the append-only audit log; VALIDATE then confirms it without blocking reads.
    allowed = ", ".join(f"'{k}'" for k in EVENT_KINDS)
    op.execute(
        f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed})) NOT VALID"
    )
    op.execute("ALTER TABLE events VALIDATE CONSTRAINT ck_events_kind")

    op.create_table(
        "routing_policies",
        sa.Column("name", sa.String(128), primary_key=True),
        sa.Column("version", sa.Integer, primary_key=True),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("retired_at", TZ, nullable=True),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", ID, primary_key=True),
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), nullable=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("type", sa.String(64), nullable=False),
        # The logical name the producer gave it; `path` is the content digest.
        sa.Column("filename", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("size", sa.BigInteger, nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("created_by", sa.String(160), nullable=False),
        sa.Column("created_at", TZ, nullable=False),
    )
    op.create_index("ix_artifacts_attempt", "artifacts", ["attempt_id"])
    op.create_index("ix_artifacts_task", "artifacts", ["task_id"])
    op.create_table(
        "evidence",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), nullable=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("pull_request_id", ID, nullable=True),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("observed_at", TZ, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("verified", sa.Boolean, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("artifact_id", ID, sa.ForeignKey("artifacts.id"), nullable=True),
        sa.CheckConstraint("source IN ('crucible', 'github', 'worker')", name="ck_evidence_source"),
        sa.CheckConstraint(
            "NOT (verified AND source = 'worker')", name="ck_evidence_worker_unverified"
        ),
    )
    op.create_index("ix_evidence_attempt_kind", "evidence", ["attempt_id", "kind"])
    op.create_index("ix_evidence_task", "evidence", ["task_id"])
    op.create_table(
        "review_reports",
        sa.Column("id", ID, primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("reviewer_kind", sa.String(32), nullable=False),
        sa.Column("reviewer_attempt_id", ID, sa.ForeignKey("attempts.id"), nullable=True),
        sa.Column("reviewer_principal_id", ID, sa.ForeignKey("principals.id"), nullable=True),
        sa.Column("document", JSONB, nullable=False),
        sa.Column("artifact_id", ID, sa.ForeignKey("artifacts.id"), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("superseded_at", TZ, nullable=True),
    )
    op.create_index("ix_review_reports_task_head", "review_reports", ["task_id", "head_sha"])
    op.create_table(
        "gate_results",
        sa.Column("id", ID, primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("gate", sa.String(48), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("evidence_ids", ARRAY(sa.BigInteger), nullable=False),
        sa.Column("evaluated_at", TZ, nullable=False),
        sa.UniqueConstraint("attempt_id", "gate", "head_sha", name="uq_gate_results_attempt_gate"),
        sa.CheckConstraint(
            "result IN ('pending', 'pass', 'fail', 'skipped', 'error')",
            name="ck_gate_results_result",
        ),
    )
    op.create_index("ix_gate_results_task", "gate_results", ["task_id"])
    op.create_table(
        "acceptance_results",
        sa.Column("id", ID, primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("principal_id", ID, sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("verdict", sa.String(24), nullable=False),
        sa.Column("reasoning", sa.Text, nullable=False),
        sa.Column("superseded_at", TZ, nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.CheckConstraint(
            "verdict IN ('accepted', 'rejected', 'needs_more_work')",
            name="ck_acceptance_results_verdict",
        ),
    )
    op.create_index("ix_acceptance_task", "acceptance_results", ["task_id"])
    op.create_table(
        "escalations",
        sa.Column("id", ID, primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), nullable=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("question", sa.Text, nullable=False),
        sa.Column("opened_at", TZ, nullable=False),
        sa.Column("closed_at", TZ, nullable=True),
        sa.Column("decision_id", ID, nullable=True),
        sa.Column("last_wake_at", TZ, nullable=True),
        sa.CheckConstraint("state IN ('open', 'answered', 'closed')", name="ck_escalations_state"),
    )
    op.create_index("ix_escalations_task_state", "escalations", ["task_id", "state"])
    op.create_table(
        "decisions",
        sa.Column("id", ID, primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("escalation_id", ID, sa.ForeignKey("escalations.id"), nullable=True),
        sa.Column("principal_id", ID, sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("kind", sa.String(48), nullable=False),
        sa.Column("verbatim", sa.Text, nullable=False),
        sa.Column("resolves", sa.Text, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
    )
    op.create_index("ix_decisions_task", "decisions", ["task_id"])
    op.create_table(
        "review_dispositions",
        sa.Column("id", ID, primary_key=True),
        sa.Column("review_comment_id", sa.String(64), nullable=False, unique=True),
        sa.Column("principal_id", ID, sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column("reasoning", sa.Text, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.CheckConstraint(
            "disposition IN ('fix', 'decline', 'out_of_scope', 'already_addressed', 'question')",
            name="ck_review_dispositions_kind",
        ),
    )
    op.create_table(
        "wakes",
        sa.Column("id", ID, primary_key=True),
        sa.Column("principal_id", ID, sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=True),
        sa.Column("reason", sa.String(48), nullable=False),
        sa.Column("payload", JSONB, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("delivered_at", TZ, nullable=True),
        sa.Column("acked_at", TZ, nullable=True),
        sa.Column("ack_note", sa.Text, nullable=True),
        sa.Column("next_attempt_at", TZ, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("gave_up_at", TZ, nullable=True),
    )
    op.alter_column("wakes", "attempts", server_default=None)
    op.create_index(
        "ix_wakes_principal_unacked",
        "wakes",
        ["principal_id", "acked_at"],
        postgresql_where=sa.text("acked_at IS NULL"),
    )
    op.create_table(
        "attempt_metrics",
        sa.Column("attempt_id", ID, sa.ForeignKey("attempts.id"), primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("harness", sa.String(32), nullable=False),
        sa.Column("endpoint_kind", sa.String(16), nullable=False),
        sa.Column("pool", sa.String(64), nullable=False),
        sa.Column("wall_ms", sa.BigInteger, nullable=True),
        sa.Column("tokens_in", sa.BigInteger, nullable=True),
        sa.Column("tokens_out", sa.BigInteger, nullable=True),
        sa.Column("cost_units", sa.Float, nullable=True),
        sa.Column("cost_source", sa.String(24), nullable=False),
        sa.Column("exit_class", sa.String(32), nullable=True),
        sa.Column("gates_passed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("gates_failed", sa.Integer, nullable=False, server_default="0"),
        sa.Column("corrections_after", sa.Integer, nullable=False, server_default="0"),
        sa.Column("acceptance_verdict", sa.String(24), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
    )
    for column in ("gates_passed", "gates_failed", "corrections_after"):
        op.alter_column("attempt_metrics", column, server_default=None)
    op.create_index("ix_attempt_metrics_model", "attempt_metrics", ["model", "created_at"])

    for table in NEW_APPEND_ONLY_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION crucible_reject_mutation();"
        )
    for table in NEW_FENCED_TABLES:
        op.execute(
            f"CREATE TRIGGER trg_{table}_fenced BEFORE INSERT OR UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION crucible_check_fenced_token();"
        )

    op.execute(
        sa.text(
            "INSERT INTO routing_policies (name, version, document, created_at) "
            "VALUES (:name, :version, CAST(:document AS jsonb), now())"
        ).bindparams(name="default-routing", version=1, document=json.dumps(DEFAULT_ROUTING))
    )
    # The C1 seed predates PolicyV1: it carries no `routing` section, and its
    # `round_counting` says `per_pull_request` where 05b says `completed_cycles`. 0001 is
    # applied and is never edited, so the correction is data here. Read, modify, write
    # back, so a missing row or a missing subsection is an error rather than a silent
    # half-application, and an operator's own edit to `routing` is left alone.
    connection = op.get_bind()
    row = connection.execute(
        sa.text("SELECT document FROM policies WHERE name = :name AND version = :version"),
        {"name": "default-software", "version": 1},
    ).scalar_one_or_none()
    if row is None:
        raise RuntimeError(
            "policy default-software/1 is missing; revision 0001 seeds it and 0004 corrects it"
        )
    document = dict(row)
    if "routing" not in document:
        document["routing"] = {"policy": {"name": "default-routing", "version": 1}}
    external_review = document.get("external_review")
    if not isinstance(external_review, dict):
        raise RuntimeError("policy default-software/1 has no external_review section to correct")
    if external_review.get("round_counting") == "per_pull_request":
        external_review["round_counting"] = "completed_cycles"
    connection.execute(
        sa.text(
            "UPDATE policies SET document = CAST(:document AS jsonb) "
            "WHERE name = :name AND version = :version"
        ),
        {"document": json.dumps(document), "name": "default-software", "version": 1},
    )


def downgrade() -> None:
    connection = op.get_bind()
    row = connection.execute(
        sa.text("SELECT document FROM policies WHERE name = :name AND version = :version"),
        {"name": "default-software", "version": 1},
    ).scalar_one_or_none()
    if row is not None:
        document = dict(row)
        document.pop("routing", None)
        external_review = document.get("external_review")
        if isinstance(external_review, dict):
            external_review["round_counting"] = "per_pull_request"
        connection.execute(
            sa.text(
                "UPDATE policies SET document = CAST(:document AS jsonb) "
                "WHERE name = :name AND version = :version"
            ),
            {"document": json.dumps(document), "name": "default-software", "version": 1},
        )
    for table in NEW_FENCED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_fenced ON {table};")
    for table in NEW_APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
    for table in C2_TABLES:
        op.drop_table(table)
    op.drop_constraint("ck_events_kind", "events", type_="check")
    # Going back to C1 means the C1 kinds are the only ones the constraint allows, so the
    # events only C2 can name have to go with it. `events` is append-only, so the trigger
    # is stood down for exactly this statement.
    kinds = ", ".join(f"'{k}'" for k in EVENT_KINDS if k in _C1_KINDS)
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute(f"DELETE FROM events WHERE kind NOT IN ({kinds})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
    op.create_check_constraint("ck_events_kind", "events", f"kind IN ({kinds})")
    op.drop_column("tasks", "publish_pending")
    op.drop_column("tasks", "head_sha")


_C1_KINDS = frozenset(
    {
        "task_submitted",
        "task_scheduled",
        "task_running",
        "task_reported",
        "task_blocked",
        "task_cancel_requested",
        "task_cancelling",
        "task_cancelled",
        "task_retry_scheduled",
        "transition_rejected",
        "contract_rejected",
        "execution_created",
        "execution_active",
        "execution_succeeded",
        "execution_failed",
        "execution_cancelled",
        "execution_resumed",
        "attempt_created",
        "attempt_preparing",
        "attempt_launching",
        "attempt_running",
        "attempt_timeout_drain",
        "attempt_timeout_kill",
        "attempt_cancel_kill",
        "attempt_terminating",
        "attempt_exited",
        "attempt_lost",
        "attempt_collected",
        "attempt_succeeded",
        "attempt_blocked",
        "attempt_failed",
        "attempt_adopted",
        "report_parsed",
        "report_parse_failed",
        "supervisor_lease_acquired",
        "supervisor_lease_released",
        "orphan_removed",
        "principal_created",
        "repository_registered",
    }
)
