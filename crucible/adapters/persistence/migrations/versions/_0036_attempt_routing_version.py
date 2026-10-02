"""Record the routing version and recover missing attempt pools.

Revision ID: 0036_attempt_routing_version
Revises: 0035_credential_renewer
"""

import sqlalchemy as sa
from alembic import op

revision = "0036_attempt_routing_version"
down_revision = "0035_credential_renewer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("attempts", sa.Column("routing_version", sa.Integer(), nullable=True))
    # Use the execution's immutable snapshot, never today's routing policy. Pending
    # attempts have not selected a route yet; imported attempts were not supervised.
    op.execute(
        """
        UPDATE attempts AS a
        SET selected_pool = COALESCE(a.selected_pool, model->>'pool'),
            routing_version = r.version
        FROM executions AS e
        JOIN routing_policies AS r
          ON r.name = e.policy_snapshot #>> '{routing,policy,name}'
         AND r.version = (e.policy_snapshot #>> '{routing,policy,version}')::integer
        CROSS JOIN LATERAL jsonb_array_elements(r.document->'models') AS model
        WHERE a.execution_id = e.id
          AND a.state <> 'pending' AND NOT a.unsupervised
          AND model->>'id' = COALESCE(a.selected_model, e.model)
          AND model->>'harness' = COALESCE(a.selected_harness, e.harness)
        """
    )


def downgrade() -> None:
    op.drop_column("attempts", "routing_version")
