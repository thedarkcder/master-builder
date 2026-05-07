"""add workflow type engine and retry config

Revision ID: 20260417_0073
Revises: 20260417_0072
Create Date: 2026-04-17 14:20:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "20260417_0073"
down_revision = "20260417_0072"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _retry_config(*, manual_retry_enabled: bool, max_attempts: int, initial_interval_seconds: int, max_interval_seconds: int, backoff_coefficient: float) -> dict[str, object]:
    return {
        "manual_retry_enabled": manual_retry_enabled,
        "max_attempts": max_attempts,
        "initial_interval_seconds": initial_interval_seconds,
        "max_interval_seconds": max_interval_seconds,
        "backoff_coefficient": backoff_coefficient,
    }


def upgrade() -> None:
    bind = op.get_bind()
    added_orchestration_backend = not _has_column("workflow_types", "orchestration_backend")
    added_retry_policy_config = not _has_column("workflow_type_operations", "retry_policy_config_json")
    if added_orchestration_backend:
        op.add_column(
            "workflow_types",
            sa.Column("orchestration_backend", sa.String(length=32), nullable=True),
        )
    if added_retry_policy_config:
        op.add_column(
            "workflow_type_operations",
            sa.Column("retry_policy_config_json", sa.JSON(), nullable=True),
        )

    workflow_types = sa.table(
        "workflow_types",
        sa.column("workflow_type_key", sa.String()),
        sa.column("orchestration_backend", sa.String()),
    )
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key.in_(("issue_execution", "pr_remediation")))
        .values(
            orchestration_backend="temporal",
        )
    )
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key == "parent_planning")
        .values(orchestration_backend="legacy")
    )

    workflow_type_operations = sa.table(
        "workflow_type_operations",
        sa.column("workflow_type_key", sa.String()),
        sa.column("operation_type", sa.String()),
        sa.column("retry_policy_config_json", sa.JSON()),
    )

    retry_defaults = {
        "run_attempt_execution": _retry_config(
            manual_retry_enabled=True,
            max_attempts=5,
            initial_interval_seconds=30,
            max_interval_seconds=900,
            backoff_coefficient=2.0,
        ),
        "human_input_resume": _retry_config(
            manual_retry_enabled=False,
            max_attempts=1,
            initial_interval_seconds=0,
            max_interval_seconds=0,
            backoff_coefficient=1.0,
        ),
        "jira_parent_update": _retry_config(
            manual_retry_enabled=True,
            max_attempts=4,
            initial_interval_seconds=60,
            max_interval_seconds=1800,
            backoff_coefficient=2.0,
        ),
        "jira_comment_projection": _retry_config(
            manual_retry_enabled=False,
            max_attempts=3,
            initial_interval_seconds=60,
            max_interval_seconds=1800,
            backoff_coefficient=2.0,
        ),
        "discord_followup_projection": _retry_config(
            manual_retry_enabled=False,
            max_attempts=3,
            initial_interval_seconds=60,
            max_interval_seconds=1800,
            backoff_coefficient=2.0,
        ),
        "notification_emit": _retry_config(
            manual_retry_enabled=False,
            max_attempts=3,
            initial_interval_seconds=60,
            max_interval_seconds=1800,
            backoff_coefficient=2.0,
        ),
        "brief_normalization": _retry_config(
            manual_retry_enabled=True,
            max_attempts=2,
            initial_interval_seconds=0,
            max_interval_seconds=300,
            backoff_coefficient=1.5,
        ),
        "backlog_planning": _retry_config(
            manual_retry_enabled=True,
            max_attempts=2,
            initial_interval_seconds=60,
            max_interval_seconds=600,
            backoff_coefficient=2.0,
        ),
        "jira_child_fanout": _retry_config(
            manual_retry_enabled=True,
            max_attempts=4,
            initial_interval_seconds=60,
            max_interval_seconds=1800,
            backoff_coefficient=2.0,
        ),
        "jira_child_promotion": _retry_config(
            manual_retry_enabled=True,
            max_attempts=4,
            initial_interval_seconds=60,
            max_interval_seconds=1800,
            backoff_coefficient=2.0,
        ),
    }
    for operation_type, retry_config in retry_defaults.items():
        bind.execute(
            workflow_type_operations.update()
            .where(workflow_type_operations.c.operation_type == operation_type)
            .values(retry_policy_config_json=retry_config)
        )
    if bind.dialect.name != "sqlite":
        if added_orchestration_backend:
            op.alter_column("workflow_types", "orchestration_backend", nullable=False)
        if added_retry_policy_config:
            op.alter_column("workflow_type_operations", "retry_policy_config_json", nullable=False)
    else:
        if added_orchestration_backend:
            with op.batch_alter_table("workflow_types") as batch_op:
                batch_op.alter_column("orchestration_backend", nullable=False)
        if added_retry_policy_config:
            with op.batch_alter_table("workflow_type_operations") as batch_op:
                batch_op.alter_column("retry_policy_config_json", nullable=False)

def downgrade() -> None:
    op.drop_column("workflow_type_operations", "retry_policy_config_json")
    op.drop_column("workflow_types", "orchestration_backend")
