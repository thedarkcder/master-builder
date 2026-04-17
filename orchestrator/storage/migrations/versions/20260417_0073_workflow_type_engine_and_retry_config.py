"""add workflow type engine and retry config

Revision ID: 20260417_0073
Revises: 20260417_0072
Create Date: 2026-04-17 14:20:00.000000
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "20260417_0073"
down_revision = "20260417_0072"
branch_labels = None
depends_on = None


def _temporal_config(*, workflow_name: str) -> str:
    return json.dumps(
        {
            "temporal": {
                "workflow_name": workflow_name,
                "task_queue": "master-builder",
                "activity_start_to_close_timeout_seconds": 7200,
                "human_input_resume_timeout_seconds": 7200,
            }
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _retry_config(*, manual_retry_enabled: bool, max_attempts: int, initial_interval_seconds: int, max_interval_seconds: int, backoff_coefficient: float, non_retryable_error_categories: list[str] | None = None) -> str:
    return json.dumps(
        {
            "manual_retry_enabled": manual_retry_enabled,
            "max_attempts": max_attempts,
            "initial_interval_seconds": initial_interval_seconds,
            "max_interval_seconds": max_interval_seconds,
            "backoff_coefficient": backoff_coefficient,
            "non_retryable_error_categories": non_retryable_error_categories or [],
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "workflow_types",
        sa.Column("orchestration_backend", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "workflow_types",
        sa.Column("engine_config_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "workflow_type_operations",
        sa.Column("retry_policy_config_json", sa.JSON(), nullable=True),
    )

    workflow_types = sa.table(
        "workflow_types",
        sa.column("workflow_type_key", sa.String()),
        sa.column("orchestration_backend", sa.String()),
        sa.column("engine_config_json", sa.JSON()),
    )
    temporal_engine_config = json.loads(_temporal_config(workflow_name="DevelopmentTeamRunWorkflow"))
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key.in_(("issue_execution", "pr_remediation")))
        .values(
            orchestration_backend="temporal",
            engine_config_json=temporal_engine_config,
        )
    )
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key == "parent_planning")
        .values(orchestration_backend="legacy", engine_config_json={})
    )

    workflow_type_operations = sa.table(
        "workflow_type_operations",
        sa.column("workflow_type_key", sa.String()),
        sa.column("operation_type", sa.String()),
        sa.column("retry_policy_config_json", sa.JSON()),
    )

    retry_defaults = {
        "run_attempt_execution": json.loads(
            _retry_config(
                manual_retry_enabled=True,
                max_attempts=5,
                initial_interval_seconds=30,
                max_interval_seconds=900,
                backoff_coefficient=2.0,
                non_retryable_error_categories=["contract_invalid", "authorization_failed"],
            )
        ),
        "human_input_resume": json.loads(
            _retry_config(
                manual_retry_enabled=False,
                max_attempts=1,
                initial_interval_seconds=0,
                max_interval_seconds=0,
                backoff_coefficient=1.0,
                non_retryable_error_categories=["missing_input", "contract_invalid"],
            )
        ),
        "jira_parent_update": json.loads(
            _retry_config(
                manual_retry_enabled=True,
                max_attempts=4,
                initial_interval_seconds=60,
                max_interval_seconds=1800,
                backoff_coefficient=2.0,
                non_retryable_error_categories=["content_limit", "contract_invalid", "authorization_failed"],
            )
        ),
        "jira_comment_projection": json.loads(
            _retry_config(
                manual_retry_enabled=False,
                max_attempts=3,
                initial_interval_seconds=60,
                max_interval_seconds=1800,
                backoff_coefficient=2.0,
                non_retryable_error_categories=["contract_invalid", "authorization_failed"],
            )
        ),
        "discord_followup_projection": json.loads(
            _retry_config(
                manual_retry_enabled=False,
                max_attempts=3,
                initial_interval_seconds=60,
                max_interval_seconds=1800,
                backoff_coefficient=2.0,
                non_retryable_error_categories=["contract_invalid", "authorization_failed"],
            )
        ),
        "notification_emit": json.loads(
            _retry_config(
                manual_retry_enabled=False,
                max_attempts=3,
                initial_interval_seconds=60,
                max_interval_seconds=1800,
                backoff_coefficient=2.0,
                non_retryable_error_categories=[],
            )
        ),
        "brief_normalization": json.loads(
            _retry_config(
                manual_retry_enabled=True,
                max_attempts=2,
                initial_interval_seconds=0,
                max_interval_seconds=300,
                backoff_coefficient=1.5,
                non_retryable_error_categories=["missing_input", "contract_invalid"],
            )
        ),
        "backlog_planning": json.loads(
            _retry_config(
                manual_retry_enabled=True,
                max_attempts=2,
                initial_interval_seconds=60,
                max_interval_seconds=600,
                backoff_coefficient=2.0,
                non_retryable_error_categories=["missing_input", "contract_invalid"],
            )
        ),
        "jira_child_fanout": json.loads(
            _retry_config(
                manual_retry_enabled=True,
                max_attempts=4,
                initial_interval_seconds=60,
                max_interval_seconds=1800,
                backoff_coefficient=2.0,
                non_retryable_error_categories=["content_limit", "contract_invalid", "authorization_failed"],
            )
        ),
        "jira_child_promotion": json.loads(
            _retry_config(
                manual_retry_enabled=True,
                max_attempts=4,
                initial_interval_seconds=60,
                max_interval_seconds=1800,
                backoff_coefficient=2.0,
                non_retryable_error_categories=["contract_invalid", "authorization_failed"],
            )
        ),
    }
    for operation_type, retry_config in retry_defaults.items():
        bind.execute(
            workflow_type_operations.update()
            .where(workflow_type_operations.c.operation_type == operation_type)
            .values(retry_policy_config_json=retry_config)
        )
    if bind.dialect.name != "sqlite":
        op.alter_column("workflow_types", "orchestration_backend", nullable=False)
        op.alter_column("workflow_types", "engine_config_json", nullable=False)
        op.alter_column("workflow_type_operations", "retry_policy_config_json", nullable=False)
    else:
        with op.batch_alter_table("workflow_types") as batch_op:
            batch_op.alter_column("orchestration_backend", nullable=False)
            batch_op.alter_column("engine_config_json", nullable=False)
        with op.batch_alter_table("workflow_type_operations") as batch_op:
            batch_op.alter_column("retry_policy_config_json", nullable=False)

def downgrade() -> None:
    op.drop_column("workflow_type_operations", "retry_policy_config_json")
    op.drop_column("workflow_types", "engine_config_json")
    op.drop_column("workflow_types", "orchestration_backend")
