"""persist workflow type lifecycle contract metadata

Revision ID: 20260417_0076
Revises: 20260417_0075
Create Date: 2026-04-17 20:10:00.000000
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "20260417_0076"
down_revision = "20260417_0075"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns(table_name)
    }


def _read_json_object(
    value: object, *, workflow_type_key: str, field_name: str
) -> dict:
    raw_value = value
    if isinstance(raw_value, str):
        try:
            raw_value = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"workflow type {workflow_type_key} has invalid {field_name}"
            ) from exc
    if not isinstance(raw_value, dict):
        raise RuntimeError(
            f"workflow type {workflow_type_key} is missing {field_name} object"
        )
    return dict(raw_value)


def _validate_lifecycle(lifecycle: dict, *, workflow_type_key: str) -> None:
    state_path_kind = str(lifecycle.get("state_path_kind") or "").strip().lower()
    if state_path_kind not in {"run", "operation"}:
        raise RuntimeError(
            f"workflow type {workflow_type_key} has invalid lifecycle.state_path_kind"
        )
    execution_modes = lifecycle.get("execution_modes")
    conditional_paths = lifecycle.get("conditional_paths")
    states = lifecycle.get("states")
    transitions = lifecycle.get("transitions")
    if not isinstance(execution_modes, list) or not all(
        str(value or "").strip() for value in execution_modes
    ):
        raise RuntimeError(
            f"workflow type {workflow_type_key} is missing lifecycle.execution_modes"
        )
    if not isinstance(conditional_paths, list) or not all(
        str(value or "").strip() for value in conditional_paths
    ):
        raise RuntimeError(
            f"workflow type {workflow_type_key} is missing lifecycle.conditional_paths"
        )
    if not isinstance(states, list) or not all(
        isinstance(value, dict) for value in states
    ):
        raise RuntimeError(
            f"workflow type {workflow_type_key} is missing lifecycle.states"
        )
    if not isinstance(transitions, list) or not all(
        isinstance(value, dict) for value in transitions
    ):
        raise RuntimeError(
            f"workflow type {workflow_type_key} is missing lifecycle.transitions"
        )


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    added_lifecycle_json = not _has_column("workflow_types", "lifecycle_json")
    if added_lifecycle_json:
        op.add_column(
            "workflow_types", sa.Column("lifecycle_json", sa.JSON(), nullable=True)
        )

    workflow_types = sa.table(
        "workflow_types",
        sa.column("workflow_type_key", sa.String()),
        sa.column("lifecycle_json", sa.JSON()),
    )
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key == "issue_execution")
        .values(
            lifecycle_json={
                "state_path_kind": "run",
                "execution_modes": ["fresh", "restart", "resume"],
                "conditional_paths": ["Human input resume", "Retry failed operation"],
                "states": [
                    {
                        "key": "queued",
                        "label": "Queued",
                        "terminal": False,
                        "waits_for_input": False,
                    },
                    {
                        "key": "running",
                        "label": "Running",
                        "terminal": False,
                        "waits_for_input": False,
                    },
                    {
                        "key": "waiting_for_input",
                        "label": "Waiting for input",
                        "terminal": False,
                        "waits_for_input": True,
                    },
                    {
                        "key": "completed",
                        "label": "Completed",
                        "terminal": True,
                        "waits_for_input": False,
                    },
                    {
                        "key": "failed",
                        "label": "Failed",
                        "terminal": True,
                        "waits_for_input": False,
                    },
                    {
                        "key": "cancelled",
                        "label": "Cancelled",
                        "terminal": True,
                        "waits_for_input": False,
                    },
                ],
                "transitions": [
                    {"from": "queued", "to": "running", "label": "Dispatch execution"},
                    {
                        "from": "running",
                        "to": "waiting_for_input",
                        "label": "Request human input",
                    },
                    {
                        "from": "waiting_for_input",
                        "to": "running",
                        "label": "Resume from answer",
                    },
                    {"from": "running", "to": "completed", "label": "Complete run"},
                    {"from": "running", "to": "failed", "label": "Fail execution"},
                    {"from": "running", "to": "cancelled", "label": "Cancel execution"},
                ],
            }
        )
    )
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key == "parent_planning")
        .values(
            lifecycle_json={
                "state_path_kind": "operation",
                "execution_modes": ["fresh", "resume"],
                "conditional_paths": [
                    "Human input clarification",
                    "Retry failed operation",
                    "Child issue fanout",
                ],
                "states": [
                    {
                        "key": "running",
                        "label": "Running",
                        "terminal": False,
                        "waits_for_input": False,
                    },
                    {
                        "key": "waiting_for_input",
                        "label": "Waiting for input",
                        "terminal": False,
                        "waits_for_input": True,
                    },
                    {
                        "key": "completed",
                        "label": "Completed",
                        "terminal": True,
                        "waits_for_input": False,
                    },
                    {
                        "key": "failed",
                        "label": "Failed",
                        "terminal": True,
                        "waits_for_input": False,
                    },
                    {
                        "key": "cancelled",
                        "label": "Cancelled",
                        "terminal": True,
                        "waits_for_input": False,
                    },
                ],
                "transitions": [
                    {
                        "from": "running",
                        "to": "waiting_for_input",
                        "label": "Ask PM clarification",
                    },
                    {
                        "from": "waiting_for_input",
                        "to": "running",
                        "label": "Resume from answer",
                    },
                    {
                        "from": "running",
                        "to": "completed",
                        "label": "Fan out child work",
                    },
                    {
                        "from": "running",
                        "to": "failed",
                        "label": "Persist operation failure",
                    },
                ],
            }
        )
    )
    bind.execute(
        workflow_types.update()
        .where(workflow_types.c.workflow_type_key == "pr_remediation")
        .values(
            lifecycle_json={
                "state_path_kind": "operation",
                "execution_modes": ["fresh", "restart", "resume"],
                "conditional_paths": ["Human input resume", "Retry failed operation"],
                "states": [
                    {
                        "key": "queued",
                        "label": "Queued",
                        "terminal": False,
                        "waits_for_input": False,
                    },
                    {
                        "key": "running",
                        "label": "Running",
                        "terminal": False,
                        "waits_for_input": False,
                    },
                    {
                        "key": "waiting_for_input",
                        "label": "Waiting for input",
                        "terminal": False,
                        "waits_for_input": True,
                    },
                    {
                        "key": "completed",
                        "label": "Completed",
                        "terminal": True,
                        "waits_for_input": False,
                    },
                    {
                        "key": "failed",
                        "label": "Failed",
                        "terminal": True,
                        "waits_for_input": False,
                    },
                    {
                        "key": "cancelled",
                        "label": "Cancelled",
                        "terminal": True,
                        "waits_for_input": False,
                    },
                ],
                "transitions": [
                    {
                        "from": "queued",
                        "to": "running",
                        "label": "Dispatch remediation",
                    },
                    {
                        "from": "running",
                        "to": "waiting_for_input",
                        "label": "Request human input",
                    },
                    {
                        "from": "waiting_for_input",
                        "to": "running",
                        "label": "Resume from answer",
                    },
                    {
                        "from": "running",
                        "to": "completed",
                        "label": "Complete remediation",
                    },
                    {"from": "running", "to": "failed", "label": "Fail remediation"},
                ],
            }
        )
    )

    rows = (
        bind.execute(
            sa.text("SELECT workflow_type_key, lifecycle_json FROM workflow_types")
        )
        .mappings()
        .all()
    )
    for row in rows:
        lifecycle = _read_json_object(
            row["lifecycle_json"],
            workflow_type_key=str(row["workflow_type_key"]),
            field_name="lifecycle_json",
        )
        _validate_lifecycle(lifecycle, workflow_type_key=str(row["workflow_type_key"]))

    if not added_lifecycle_json:
        return
    if dialect == "sqlite":
        with op.batch_alter_table("workflow_types") as batch_op:
            batch_op.alter_column("lifecycle_json", nullable=False)
    else:
        op.alter_column("workflow_types", "lifecycle_json", nullable=False)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("workflow_types") as batch_op:
            batch_op.drop_column("lifecycle_json")
    else:
        op.drop_column("workflow_types", "lifecycle_json")
