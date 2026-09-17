"""C4: GitHub delivery. Pull requests, head history, external review cycles and their
signals, reactions, CI certifications and decisions, and webhook deliveries.

Also: the operator's external-review attestation on `repositories` (23), and the removal
of `tasks.publish_pending`, which was C2's stand-in for the `publishing` edge and is
replaced by the state itself (09).

Revision ID: 0007_github_delivery
Revises: 0006_log_occurrence
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

from crucible.adapters.persistence.migrations.versions._0006_log_occurrence import (
    _event_kinds as c3_event_kinds,
)

revision = "0007_github_delivery"
down_revision = "0006_log_occurrence"
branch_labels = None
depends_on = None

ID = sa.String(26)
TZ = sa.DateTime(timezone=True)

# Adding a kind is a migration (10). Keep this list in step with crucible.domain.events.
C4_EVENT_KINDS = (
    "task_publishing",
    "publish_started",
    "installation_token_minted",
    "publisher_started",
    "publisher_finished",
    "branch_pushed",
    "pull_request_opened",
    "pull_request_head_updated",
    "publish_completed",
    "task_publish_failed",
    "task_awaiting_external_review",
    "task_awaiting_ci_certification",
    "pull_request_polled",
    "pull_request_state_changed",
    "pull_request_head_observed",
    "reactions_unobservable",
    "reaction_received",
    "reaction_removed",
    "external_review_received",
    "review_comment_received",
    "issue_comment_received",
    "external_review_ignored",
    "external_review_cycle_opened",
    "external_review_cycle_completed",
    "external_review_trigger_needed",
    "task_external_feedback_received",
    "check_run_observed",
    "workflow_run_observed",
    "ci_certification_recorded",
    "task_ci_certification_failed",
    "ci_decision_recorded",
    "task_ready_for_merge",
    "task_merged",
    "task_head_diverged",
    "head_decision_recorded",
    "superseded_for_head",
    "github_delivery_received",
    "github_delivery_rejected",
    "github_rate_limited",
    "repository_attestation_recorded",
)

C4_TABLES = (
    "github_deliveries",
    "ci_decisions",
    "ci_certifications",
    "reactions",
    "review_comments",
    "external_reviews",
    "external_review_cycles",
    "pull_request_heads",
    "pull_requests",
)

# 14: everything the supervisor alone writes carries the fenced token. Observation is
# the supervisor's, so every observed row is fenced. `ci_decisions` is Foundry's and is
# append-only instead. `github_deliveries` is written by the webhook endpoint, which has
# no lease and no principal, and is processed by the supervisor afterwards, so it is
# neither fenced nor append-only (04, 14).
NEW_FENCED_TABLES = (
    "pull_requests",
    "pull_request_heads",
    "external_review_cycles",
    "external_reviews",
    "review_comments",
    "reactions",
    "ci_certifications",
)
NEW_APPEND_ONLY_TABLES = ("ci_decisions",)

# Where a downgrade puts the C4 event rows. The revisions below this one recreate the
# event-kind CHECK in its validating form, which C4 rows cannot satisfy, and they are
# applied revisions this one may not edit. Deleting the rows to make a constraint fit
# would destroy the audit log, so they are moved aside instead and moved back on the way
# up. Nothing is lost either way, and a database that was downgraded says so by having
# this table.
EVENT_ARCHIVE = "events_c4_archive"


def _event_kinds() -> list[str]:
    return [*c3_event_kinds(), *C4_EVENT_KINDS]


def _archive_exists(connection: sa.engine.Connection) -> bool:
    return bool(
        connection.execute(
            sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": f"public.{EVENT_ARCHIVE}"}
        ).scalar()
    )


def upgrade() -> None:
    op.add_column(
        "repositories",
        sa.Column(
            "external_review_attested", sa.Boolean, nullable=False, server_default=sa.false()
        ),
    )
    op.alter_column("repositories", "external_review_attested", server_default=None)
    op.add_column("repositories", sa.Column("attested_by", sa.String(128), nullable=True))
    op.add_column("repositories", sa.Column("attested_at", TZ, nullable=True))

    op.drop_constraint("ck_events_kind", "events", type_="check")
    allowed = ", ".join(f"'{k}'" for k in _event_kinds())
    op.execute(
        f"ALTER TABLE events ADD CONSTRAINT ck_events_kind CHECK (kind IN ({allowed})) NOT VALID"
    )
    op.execute("ALTER TABLE events VALIDATE CONSTRAINT ck_events_kind")

    # Put back anything a previous downgrade moved aside. The constraint above already
    # allows these kinds again, which is why this runs here and not earlier.
    connection = op.get_bind()
    if _archive_exists(connection):
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
        op.execute(
            f"INSERT INTO events (seq, ts, kind, task_id, execution_id, attempt_id, "
            f"principal, verified, payload) SELECT seq, ts, kind, task_id, execution_id, "
            f"attempt_id, principal, verified, payload FROM {EVENT_ARCHIVE}"
        )
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")
        op.execute(
            "SELECT setval('events_seq_seq', GREATEST("
            "(SELECT COALESCE(MAX(seq), 1) FROM events), 1))"
        )
        op.execute(f"DROP TABLE {EVENT_ARCHIVE}")

    # C2's flag becomes the `publishing` state (09). A task accepted under C2 is waiting
    # in `awaiting_acceptance` with the flag raised and would be stranded by a bare drop,
    # so it is moved first, with the event the transition owes it. The event kind is
    # already allowed by the constraint above, which is why this runs after it.
    connection = op.get_bind()
    stranded = connection.execute(
        sa.text(
            "SELECT id, head_sha FROM tasks "
            "WHERE publish_pending AND state = 'awaiting_acceptance' FOR UPDATE"
        )
    ).all()
    if stranded:
        # A migration holds no supervisor lease, and `events` is fenced to the
        # supervisor. The fence exists to stop a second *supervisor* writing, not a
        # migration, so it stands down for exactly this insert and nothing else.
        op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_fenced")
    for task_id, head_sha in stranded:
        connection.execute(
            sa.text("UPDATE tasks SET state = 'publishing', updated_at = now() WHERE id = :id"),
            {"id": task_id},
        )
        connection.execute(
            sa.text(
                "INSERT INTO events (ts, kind, task_id, principal, verified, payload) "
                "VALUES (now(), 'task_publishing', :task_id, 'crucible', true, "
                "CAST(:payload AS jsonb))"
            ),
            {
                "task_id": task_id,
                "payload": json.dumps(
                    {
                        "from": "awaiting_acceptance",
                        "to": "publishing",
                        "head_sha": head_sha,
                        "note": (
                            "migrated from C2's publish_pending flag by revision "
                            "0007_github_delivery"
                        ),
                    }
                ),
            },
        )
    if stranded:
        op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_fenced")
    op.drop_column("tasks", "publish_pending")

    # 23: the provider's summary comment is edited in place and never counts as a round,
    # but it arrives before any verdict exists and the C1 seed lists `comment` as an
    # accepted signal, so it completed the cycle before the review did. 0001 is applied
    # and is never edited, so the correction is data here: read, modify, write back, so
    # a missing row is an error rather than a silent half-application, and an operator's
    # own edit to the list is left alone.
    policy_row = connection.execute(
        sa.text("SELECT document FROM policies WHERE name = :name AND version = :version"),
        {"name": "default-software", "version": 1},
    ).scalar_one_or_none()
    if policy_row is None:
        raise RuntimeError(
            "policy default-software/1 is missing; revision 0001 seeds it and 0007 corrects it"
        )
    document = dict(policy_row)
    external_review = document.get("external_review")
    if not isinstance(external_review, dict):
        raise RuntimeError("policy default-software/1 has no external_review section to correct")
    signals = external_review.get("accepted_signals")
    if isinstance(signals, list) and set(signals) == {"review", "comment", "reaction:+1"}:
        external_review["accepted_signals"] = ["review", "reaction:+1"]
        connection.execute(
            sa.text(
                "UPDATE policies SET document = CAST(:document AS jsonb) "
                "WHERE name = :name AND version = :version"
            ),
            {"document": json.dumps(document), "name": "default-software", "version": 1},
        )

    op.create_table(
        "pull_requests",
        sa.Column("id", ID, primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False, unique=True),
        sa.Column("repository_id", ID, sa.ForeignKey("repositories.id"), nullable=False),
        sa.Column("number", sa.Integer, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("base_ref", sa.String(255), nullable=False),
        sa.Column("work_branch", sa.String(255), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        # The hash of the rendered body; the body itself is on GitHub (23 step 6).
        sa.Column("body_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("opened_at", TZ, nullable=False),
        sa.Column("merged_at", TZ, nullable=True),
        sa.Column("merge_sha", sa.String(64), nullable=True),
        sa.Column("merged_by", sa.String(128), nullable=True),
        sa.Column("closed_at", TZ, nullable=True),
        sa.Column("closed_by", sa.String(128), nullable=True),
        sa.Column("last_polled_at", TZ, nullable=True),
        sa.Column("last_reactions_polled_at", TZ, nullable=True),
        sa.Column("reactions_observable", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("cancelled_at", TZ, nullable=True),
        sa.CheckConstraint(
            "state IN ('opening', 'open', 'merged', 'closed')", name="ck_pull_requests_state"
        ),
        sa.UniqueConstraint("repository_id", "number", name="uq_pull_requests_repo_number"),
    )
    for column in ("title", "body_sha256"):
        op.alter_column("pull_requests", column, server_default=None)
    op.alter_column("pull_requests", "reactions_observable", server_default=None)
    op.create_index("ix_pull_requests_state", "pull_requests", ["state"])

    op.create_table(
        "pull_request_heads",
        sa.Column("id", ID, primary_key=True),
        sa.Column("pull_request_id", ID, sa.ForeignKey("pull_requests.id"), nullable=False),
        sa.Column("sha", sa.String(64), nullable=False),
        sa.Column("pushed_by", sa.String(16), nullable=False),
        sa.Column("observed_at", TZ, nullable=False),
        sa.CheckConstraint(
            "pushed_by IN ('crucible', 'other')", name="ck_pull_request_heads_pushed_by"
        ),
        sa.UniqueConstraint("pull_request_id", "sha", name="uq_pull_request_heads_sha"),
    )
    op.create_index("ix_pull_request_heads_pr", "pull_request_heads", ["pull_request_id"])

    op.create_table(
        "external_review_cycles",
        sa.Column("id", ID, primary_key=True),
        sa.Column("pull_request_id", ID, sa.ForeignKey("pull_requests.id"), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("components", JSONB, nullable=False),
        sa.Column("completed_components", JSONB, nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("opened_at", TZ, nullable=False),
        sa.Column("completed_at", TZ, nullable=True),
        sa.CheckConstraint(
            "state IN ('open', 'completed', 'superseded')",
            name="ck_external_review_cycles_state",
        ),
    )
    op.create_index(
        "ix_external_review_cycles_pr", "external_review_cycles", ["pull_request_id", "head_sha"]
    )

    op.create_table(
        "external_reviews",
        sa.Column("id", ID, primary_key=True),
        sa.Column("pull_request_id", ID, sa.ForeignKey("pull_requests.id"), nullable=False),
        sa.Column("cycle_id", ID, sa.ForeignKey("external_review_cycles.id"), nullable=True),
        sa.Column("reviewer_login", sa.String(128), nullable=False),
        sa.Column("signal", sa.String(16), nullable=False),
        sa.Column("github_id", sa.String(64), nullable=False),
        sa.Column("reviewed_sha", sa.String(64), nullable=True),
        # A reaction carries no commit id, so its head binding is inferred from the head
        # history at its created_at and is recorded as inferred, never as a field (S12).
        sa.Column("sha_inferred", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("state", sa.String(32), nullable=False, server_default=""),
        # Stored only after secret scanning and redaction (23).
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("accepted", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("received_at", TZ, nullable=False),
        sa.CheckConstraint(
            "signal IN ('review', 'comment', 'reaction')", name="ck_external_reviews_signal"
        ),
        sa.UniqueConstraint(
            "pull_request_id", "signal", "github_id", name="uq_external_reviews_github_id"
        ),
    )
    for column in ("sha_inferred", "state", "accepted"):
        op.alter_column("external_reviews", column, server_default=None)
    op.create_index("ix_external_reviews_pr", "external_reviews", ["pull_request_id"])

    op.create_table(
        "review_comments",
        sa.Column("id", ID, primary_key=True),
        sa.Column("pull_request_id", ID, sa.ForeignKey("pull_requests.id"), nullable=False),
        sa.Column("external_review_id", ID, sa.ForeignKey("external_reviews.id"), nullable=True),
        sa.Column("github_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("login", sa.String(128), nullable=False),
        sa.Column("path", sa.Text, nullable=True),
        sa.Column("line", sa.Integer, nullable=True),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("reviewed_sha", sa.String(64), nullable=True),
        sa.Column("created_at", TZ, nullable=False),
        sa.Column("updated_at", TZ, nullable=False),
        sa.UniqueConstraint(
            "pull_request_id", "kind", "github_id", name="uq_review_comments_github_id"
        ),
    )
    op.create_index("ix_review_comments_pr", "review_comments", ["pull_request_id"])

    op.create_table(
        "reactions",
        sa.Column("id", ID, primary_key=True),
        sa.Column("pull_request_id", ID, sa.ForeignKey("pull_requests.id"), nullable=False),
        sa.Column("subject_kind", sa.String(24), nullable=False),
        sa.Column("subject_github_id", sa.String(64), nullable=False),
        sa.Column("github_id", sa.String(64), nullable=False),
        sa.Column("login", sa.String(128), nullable=False),
        sa.Column("content", sa.String(32), nullable=False),
        sa.Column("created_at", TZ, nullable=True),
        sa.Column("observed_at", TZ, nullable=False),
        # A reaction can be deleted between polls and polling cannot tell a deleted one
        # from one that never existed, so the disappearance is recorded, not the delete.
        sa.Column("removed_at", TZ, nullable=True),
        sa.CheckConstraint(
            "subject_kind IN ('pull_request', 'review', 'review_comment', 'issue_comment')",
            name="ck_reactions_subject_kind",
        ),
        sa.UniqueConstraint(
            "pull_request_id",
            "subject_kind",
            "subject_github_id",
            "github_id",
            name="uq_reactions_github_id",
        ),
    )
    op.create_index("ix_reactions_pr", "reactions", ["pull_request_id"])

    op.create_table(
        "ci_certifications",
        sa.Column("id", ID, primary_key=True),
        sa.Column("pull_request_id", ID, sa.ForeignKey("pull_requests.id"), nullable=False),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("head_sha", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("required_checks", JSONB, nullable=False),
        sa.Column("check_runs", JSONB, nullable=False),
        sa.Column("failure", JSONB, nullable=False),
        sa.Column("detail", sa.Text, nullable=False),
        sa.Column("evaluated_at", TZ, nullable=False),
        sa.CheckConstraint(
            "state IN ('pending', 'green', 'failed', 'skipped')",
            name="ck_ci_certifications_state",
        ),
        sa.UniqueConstraint("pull_request_id", "head_sha", name="uq_ci_certifications_head"),
    )
    op.create_index("ix_ci_certifications_task", "ci_certifications", ["task_id"])

    op.create_table(
        "ci_decisions",
        sa.Column("id", ID, primary_key=True),
        sa.Column("task_id", ID, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("ci_certification_id", ID, sa.ForeignKey("ci_certifications.id"), nullable=True),
        sa.Column("principal_id", ID, sa.ForeignKey("principals.id"), nullable=False),
        sa.Column("cause", sa.String(48), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reasoning", sa.Text, nullable=False),
        sa.Column("created_at", TZ, nullable=False),
        sa.CheckConstraint(
            "cause IN ('false_pre_pr_evidence', 'wrong_sha_checked', "
            "'correction_without_checks', 'environment_drift', 'flaky_test', "
            "'crucible_verification_defect', 'ci_infrastructure', 'other')",
            name="ck_ci_decisions_cause",
        ),
        sa.CheckConstraint(
            "action IN ('rerun', 'correct', 'reject', 'cancel')", name="ck_ci_decisions_action"
        ),
    )
    op.create_index("ix_ci_decisions_task", "ci_decisions", ["task_id"])

    op.create_table(
        "github_deliveries",
        sa.Column("delivery_id", sa.String(64), primary_key=True),
        sa.Column("event", sa.String(48), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("repository", sa.String(255), nullable=False),
        sa.Column("received_at", TZ, nullable=False),
        # The SHA-256 of the original body. The raw body is never persisted (04, 23).
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column("normalized", JSONB, nullable=False),
        sa.Column("processed_at", TZ, nullable=True),
    )
    op.create_index(
        "ix_github_deliveries_unprocessed",
        "github_deliveries",
        ["received_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )

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


def downgrade() -> None:
    for table in NEW_FENCED_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_fenced ON {table};")
    for table in NEW_APPEND_ONLY_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table};")
    for table in C4_TABLES:
        op.drop_table(table)

    op.add_column(
        "tasks",
        sa.Column("publish_pending", sa.Boolean, nullable=False, server_default=sa.false()),
    )
    op.alter_column("tasks", "publish_pending", server_default=None)

    connection = op.get_bind()
    policy_row = connection.execute(
        sa.text("SELECT document FROM policies WHERE name = :name AND version = :version"),
        {"name": "default-software", "version": 1},
    ).scalar_one_or_none()
    if policy_row is not None:
        document = dict(policy_row)
        external_review = document.get("external_review")
        if isinstance(external_review, dict) and set(
            external_review.get("accepted_signals") or []
        ) == {"review", "reaction:+1"}:
            external_review["accepted_signals"] = ["review", "comment", "reaction:+1"]
            connection.execute(
                sa.text(
                    "UPDATE policies SET document = CAST(:document AS jsonb) "
                    "WHERE name = :name AND version = :version"
                ),
                {"document": json.dumps(document), "name": "default-software", "version": 1},
            )
    op.drop_column("repositories", "attested_at")
    op.drop_column("repositories", "attested_by")
    op.drop_column("repositories", "external_review_attested")

    # `events` is the audit log, and no downgrade deletes audit rows to make a constraint
    # fit. The revisions below this one recreate the CHECK in its validating form, so the
    # C4 rows are moved into an archive table this migration leaves behind, and 0007's
    # upgrade moves them back. The append-only trigger stands down for exactly the move.
    gone = ", ".join(f"'{k}'" for k in C4_EVENT_KINDS)
    # `LIKE events` without INCLUDING DEFAULTS: copying the default would make the
    # archive depend on `events_seq_seq`, and revision 0001's downgrade could then not
    # drop `events` at all.
    op.execute(f"CREATE TABLE IF NOT EXISTS {EVENT_ARCHIVE} (LIKE events)")
    op.execute("ALTER TABLE events DISABLE TRIGGER trg_events_append_only")
    op.execute(f"INSERT INTO {EVENT_ARCHIVE} SELECT * FROM events WHERE kind IN ({gone})")
    op.execute(f"DELETE FROM events WHERE kind IN ({gone})")
    op.execute("ALTER TABLE events ENABLE TRIGGER trg_events_append_only")

    op.drop_constraint("ck_events_kind", "events", type_="check")
    kinds = ", ".join(f"'{k}'" for k in _event_kinds() if k not in C4_EVENT_KINDS)
    op.create_check_constraint("ck_events_kind", "events", f"kind IN ({kinds})")
