"""add workflow type handler binding and capabilities metadata

Revision ID: 20260417_0075
Revises: 20260417_0074
Create Date: 2026-04-17 18:55:00.000000
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "20260417_0075"
down_revision = "20260417_0074"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _has_unique_constraint(table_name: str, constraint_name: str) -> bool:
    return constraint_name in {
        constraint["name"] for constraint in sa.inspect(op.get_bind()).get_unique_constraints(table_name)
    }


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    added_handler_key = not _has_column("workflow_types", "handler_key")
    added_capabilities_json = not _has_column("workflow_types", "capabilities_json")
    if added_handler_key:
        op.add_column("workflow_types", sa.Column("handler_key", sa.String(length=64), nullable=True))
    if added_capabilities_json:
        op.add_column("workflow_types", sa.Column("capabilities_json", sa.JSON(), nullable=True))

    workflow_types = sa.table(
        "workflow_types",
        sa.column("workflow_type_key", sa.String()),
        sa.column("handler_key", sa.String()),
        sa.column("capabilities_json", sa.JSON()),
    )
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key == "issue_execution")
        .values(
            handler_key="development_team_run",
            capabilities_json={"state_path_kind": "run"},
        )
    )
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key == "parent_planning")
        .values(
            handler_key="jira_parent_feature",
            capabilities_json={"child_issue_links": True, "state_path_kind": "operation"},
        )
    )
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key == "pr_remediation")
        .values(
            handler_key="pr_remediation",
            capabilities_json={"state_path_kind": "operation"},
        )
    )

    rows = bind.execute(
        sa.text("SELECT workflow_type_key, handler_key, capabilities_json FROM workflow_types")
    ).mappings().all()
    for row in rows:
        handler_key = str(row["handler_key"] or "").strip()
        if not handler_key:
            raise RuntimeError(f"workflow type {row['workflow_type_key']} is missing handler_key")
        raw_capabilities = row["capabilities_json"]
        if isinstance(raw_capabilities, str):
            try:
                raw_capabilities = json.loads(raw_capabilities)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"workflow type {row['workflow_type_key']} has invalid capabilities_json"
                ) from exc
        if not isinstance(raw_capabilities, dict):
            raise RuntimeError(f"workflow type {row['workflow_type_key']} is missing capabilities_json object")

    if dialect == "sqlite":
        with op.batch_alter_table("workflow_types") as batch_op:
            if added_handler_key:
                batch_op.alter_column("handler_key", nullable=False)
            if added_capabilities_json:
                batch_op.alter_column("capabilities_json", nullable=False)
            if not _has_unique_constraint("workflow_types", "uq_workflow_types_handler_key"):
                batch_op.create_unique_constraint("uq_workflow_types_handler_key", ["handler_key"])
    else:
        if added_handler_key:
            op.alter_column("workflow_types", "handler_key", nullable=False)
        if added_capabilities_json:
            op.alter_column("workflow_types", "capabilities_json", nullable=False)
        if not _has_unique_constraint("workflow_types", "uq_workflow_types_handler_key"):
            op.create_unique_constraint("uq_workflow_types_handler_key", "workflow_types", ["handler_key"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("workflow_types") as batch_op:
            batch_op.drop_constraint("uq_workflow_types_handler_key", type_="unique")
            batch_op.drop_column("capabilities_json")
            batch_op.drop_column("handler_key")
    else:
        op.drop_constraint("uq_workflow_types_handler_key", "workflow_types", type_="unique")
        op.drop_column("workflow_types", "capabilities_json")
        op.drop_column("workflow_types", "handler_key")
