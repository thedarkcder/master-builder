"""remove workflow engine config from workflow types

Revision ID: 20260420_0080
Revises: 20260420_0079
Create Date: 2026-04-20 22:30:00.000000
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260420_0080"
down_revision = "20260420_0079"
branch_labels = None
depends_on = None


def _normalize_retry_policy(raw_value) -> dict[str, object]:  # noqa: ANN001
    if isinstance(raw_value, dict):
        normalized = dict(raw_value)
    elif isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed = {}
        normalized = dict(parsed) if isinstance(parsed, dict) else {}
    else:
        normalized = {}
    normalized.pop("non_retryable_error_categories", None)
    return normalized


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    workflow_type_columns = {
        column["name"] for column in inspector.get_columns("workflow_types")
    }
    if "retry_policy_config_json" in workflow_type_columns:
        rows = bind.execute(
            sa.text(
                "SELECT workflow_type_key, retry_policy_config_json FROM workflow_types ORDER BY workflow_type_key"
            )
        ).mappings()
        workflow_types = sa.table(
            "workflow_types",
            sa.column("workflow_type_key", sa.String()),
            sa.column("retry_policy_config_json", sa.JSON()),
        )
        for row in rows:
            bind.execute(
                workflow_types.update()
                .where(workflow_types.c.workflow_type_key == row["workflow_type_key"])
                .values(
                    retry_policy_config_json=_normalize_retry_policy(
                        row["retry_policy_config_json"]
                    )
                )
            )
    if "engine_config_json" in workflow_type_columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("workflow_types") as batch_op:
                batch_op.drop_column("engine_config_json")
        else:
            op.drop_column("workflow_types", "engine_config_json")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    workflow_type_columns = {
        column["name"] for column in inspector.get_columns("workflow_types")
    }
    if "engine_config_json" not in workflow_type_columns:
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("workflow_types") as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "engine_config_json",
                        sa.JSON(),
                        nullable=False,
                        server_default="{}",
                    )
                )
        else:
            op.add_column(
                "workflow_types",
                sa.Column(
                    "engine_config_json", sa.JSON(), nullable=False, server_default="{}"
                ),
            )
        if bind.dialect.name != "sqlite":
            op.alter_column("workflow_types", "engine_config_json", server_default=None)
