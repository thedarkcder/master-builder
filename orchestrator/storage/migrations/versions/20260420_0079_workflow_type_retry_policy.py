"""move retry policy to workflow types

Revision ID: 20260420_0079
Revises: 20260420_0078
Create Date: 2026-04-20 15:30:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260420_0079"
down_revision = "20260420_0078"
branch_labels = None
depends_on = None


def _default_retry_policy_config() -> dict[str, object]:
    return {
        "manual_retry_enabled": True,
        "max_attempts": 3,
        "initial_interval_seconds": 60,
        "max_interval_seconds": 900,
        "backoff_coefficient": 2.0,
    }


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    workflow_type_columns = {
        column["name"] for column in inspector.get_columns("workflow_types")
    }
    workflow_type_operation_columns = {
        column["name"] for column in inspector.get_columns("workflow_type_operations")
    }

    if "retry_policy_config_json" not in workflow_type_columns:
        op.add_column(
            "workflow_types",
            sa.Column("retry_policy_config_json", sa.JSON(), nullable=True),
        )

    workflow_rows = bind.execute(
        sa.text(
            """
            SELECT
                workflow_type_key,
                retry_policy_config_json
            FROM workflow_type_operations
            ORDER BY workflow_type_key ASC, required DESC, sort_order ASC
            """
        )
    ).mappings()

    backfill_by_type: dict[str, dict[str, object]] = {}
    for row in workflow_rows:
        workflow_type_key = str(row["workflow_type_key"] or "").strip()
        if not workflow_type_key or workflow_type_key in backfill_by_type:
            continue
        raw_config = row["retry_policy_config_json"]
        if isinstance(raw_config, str):
            try:
                parsed_config = json.loads(raw_config)
            except json.JSONDecodeError:
                parsed_config = _default_retry_policy_config()
        elif isinstance(raw_config, dict):
            parsed_config = raw_config
        else:
            parsed_config = _default_retry_policy_config()
        backfill_by_type[workflow_type_key] = parsed_config

    workflow_types = sa.table(
        "workflow_types",
        sa.column("workflow_type_key", sa.String()),
        sa.column("retry_policy_config_json", sa.JSON()),
    )
    for workflow_type_key, retry_policy_config in backfill_by_type.items():
        bind.execute(
            workflow_types.update()
            .where(workflow_types.c.workflow_type_key == workflow_type_key)
            .values(retry_policy_config_json=retry_policy_config)
        )

    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.retry_policy_config_json.is_(None))
        .values(
            retry_policy_config_json=_default_retry_policy_config(),
        )
    )

    if bind.dialect.name != "sqlite":
        op.alter_column("workflow_types", "retry_policy_config_json", nullable=False)
        if "retry_policy_config_json" in workflow_type_operation_columns:
            op.drop_column("workflow_type_operations", "retry_policy_config_json")
        if "retry_policy" in workflow_type_operation_columns:
            op.drop_column("workflow_type_operations", "retry_policy")
    else:
        with op.batch_alter_table("workflow_types") as batch_op:
            batch_op.alter_column("retry_policy_config_json", nullable=False)
        if (
            "retry_policy_config_json" in workflow_type_operation_columns
            or "retry_policy" in workflow_type_operation_columns
        ):
            with op.batch_alter_table("workflow_type_operations") as batch_op:
                if "retry_policy_config_json" in workflow_type_operation_columns:
                    batch_op.drop_column("retry_policy_config_json")
                if "retry_policy" in workflow_type_operation_columns:
                    batch_op.drop_column("retry_policy")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    workflow_type_columns = {
        column["name"] for column in inspector.get_columns("workflow_types")
    }
    workflow_type_operation_columns = {
        column["name"] for column in inspector.get_columns("workflow_type_operations")
    }

    if bind.dialect.name != "sqlite":
        if "retry_policy" not in workflow_type_operation_columns:
            op.add_column(
                "workflow_type_operations",
                sa.Column("retry_policy", sa.Text(), nullable=True),
            )
        if "retry_policy_config_json" not in workflow_type_operation_columns:
            op.add_column(
                "workflow_type_operations",
                sa.Column("retry_policy_config_json", sa.JSON(), nullable=True),
            )
    else:
        if (
            "retry_policy" not in workflow_type_operation_columns
            or "retry_policy_config_json" not in workflow_type_operation_columns
        ):
            with op.batch_alter_table("workflow_type_operations") as batch_op:
                if "retry_policy" not in workflow_type_operation_columns:
                    batch_op.add_column(
                        sa.Column("retry_policy", sa.Text(), nullable=True)
                    )
                if "retry_policy_config_json" not in workflow_type_operation_columns:
                    batch_op.add_column(
                        sa.Column("retry_policy_config_json", sa.JSON(), nullable=True)
                    )

    workflow_types = sa.table(
        "workflow_types",
        sa.column("workflow_type_key", sa.String()),
        sa.column("retry_policy_config_json", sa.JSON()),
    )
    workflow_type_operations = sa.table(
        "workflow_type_operations",
        sa.column("workflow_type_key", sa.String()),
        sa.column("retry_policy", sa.Text()),
        sa.column("retry_policy_config_json", sa.JSON()),
    )
    workflow_policies = bind.execute(
        sa.select(
            workflow_types.c.workflow_type_key,
            workflow_types.c.retry_policy_config_json,
        )
    ).mappings()
    for row in workflow_policies:
        bind.execute(
            workflow_type_operations.update()
            .where(
                workflow_type_operations.c.workflow_type_key == row["workflow_type_key"]
            )
            .values(
                retry_policy="Workflow-level retry policy",
                retry_policy_config_json=row["retry_policy_config_json"],
            )
        )

    if bind.dialect.name != "sqlite":
        op.alter_column("workflow_type_operations", "retry_policy", nullable=False)
        op.alter_column(
            "workflow_type_operations", "retry_policy_config_json", nullable=False
        )
        if "retry_policy_config_json" in workflow_type_columns:
            op.drop_column("workflow_types", "retry_policy_config_json")
    else:
        with op.batch_alter_table("workflow_type_operations") as batch_op:
            batch_op.alter_column("retry_policy", nullable=False)
            batch_op.alter_column("retry_policy_config_json", nullable=False)
        if "retry_policy_config_json" in workflow_type_columns:
            with op.batch_alter_table("workflow_types") as batch_op:
                batch_op.drop_column("retry_policy_config_json")
