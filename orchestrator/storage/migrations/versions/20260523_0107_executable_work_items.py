"""Add executable work item projection.

Revision ID: 20260523_0107
Revises: 20260505_0106
Create Date: 2026-05-23 14:00:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "20260523_0107"
down_revision = "20260505_0106"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def _parse_json_payload(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _json_value(bind: sa.engine.Connection, value: dict[str, object]) -> object:
    serialized = json.dumps(value, sort_keys=True)
    if bind.dialect.name == "postgresql":
        return serialized
    return serialized


def _backfilled_parent_work_state(status: object) -> str:
    normalized_status = str(status or "").strip().lower()
    if normalized_status in {"queued", "pending", "completed"}:
        return "planning_candidate"
    return "not_planning"


def _insert_work_item(bind: sa.engine.Connection, values: dict[str, object]) -> None:
    payload_value_expr = "CAST(:source_payload_json AS JSON)" if bind.dialect.name == "postgresql" else ":source_payload_json"
    insert_sql = """
        INSERT INTO workflow_executable_work_items (
            work_item_id,
            item_kind,
            tenant_id,
            project_id,
            parent_workflow_id,
            parent_execution_id,
            issue_key,
            parent_issue_key,
            issue_summary,
            issue_status,
            issue_type,
            mb_work_state,
            source_system,
            source_external_id,
            source_payload_json,
            created_at,
            updated_at,
            last_seen_at
        )
        VALUES (
            :work_item_id,
            :item_kind,
            :tenant_id,
            :project_id,
            :parent_workflow_id,
            :parent_execution_id,
            :issue_key,
            :parent_issue_key,
            :issue_summary,
            :issue_status,
            :issue_type,
            :mb_work_state,
            :source_system,
            :source_external_id,
            {payload_value_expr},
            :created_at,
            :updated_at,
            :last_seen_at
        )
    """.format(payload_value_expr=payload_value_expr)
    if bind.dialect.name == "sqlite":
        insert_sql = insert_sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
    elif bind.dialect.name == "postgresql":
        insert_sql += " ON CONFLICT (work_item_id) DO NOTHING"
    bind.execute(sa.text(insert_sql), values)


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "workflow_executable_work_items"):
        op.create_table(
            "workflow_executable_work_items",
            sa.Column("work_item_id", sa.String(length=255), primary_key=True),
            sa.Column("item_kind", sa.String(length=32), nullable=False),
            sa.Column(
                "tenant_id",
                sa.String(length=128),
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "project_id",
                sa.String(length=128),
                sa.ForeignKey("projects.project_id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "parent_workflow_id",
                sa.String(length=64),
                sa.ForeignKey("workflow_executions.workflow_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("parent_execution_id", sa.String(length=64), nullable=False),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("parent_issue_key", sa.String(length=64), nullable=True),
            sa.Column("issue_summary", sa.Text(), nullable=True),
            sa.Column("issue_status", sa.String(length=128), nullable=True),
            sa.Column("issue_type", sa.String(length=128), nullable=True),
            sa.Column("mb_work_state", sa.String(length=64), nullable=True),
            sa.Column("source_system", sa.String(length=64), nullable=False, server_default="jira"),
            sa.Column("source_external_id", sa.String(length=255), nullable=True),
            sa.Column("source_payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("item_kind IN ('parent', 'child')", name="ck_workflow_executable_work_items_kind"),
            sa.UniqueConstraint(
                "parent_workflow_id",
                "item_kind",
                "issue_key",
                name="uq_workflow_executable_work_items_parent_kind_issue",
            ),
        )
        op.create_index(
            "ix_workflow_executable_work_items_tenant_project",
            "workflow_executable_work_items",
            ["tenant_id", "project_id"],
        )
        op.create_index(
            "ix_workflow_executable_work_items_parent_workflow",
            "workflow_executable_work_items",
            ["parent_workflow_id"],
        )
        op.create_index(
            "ix_workflow_executable_work_items_issue",
            "workflow_executable_work_items",
            ["tenant_id", "project_id", "issue_key"],
        )

    if not _table_exists(bind, "workflow_executions"):
        return

    parent_rows = bind.execute(
        sa.text(
            """
            SELECT
                workflow_id,
                execution_id,
                tenant_id,
                project_id,
                source_system,
                source_ref,
                source_external_id,
                display_name,
                status,
                created_at,
                updated_at
            FROM workflow_executions
            WHERE workflow_type_key = 'parent_planning'
              AND status != 'cancelled'
            """
        )
    ).mappings()
    parent_by_issue_key: dict[str, dict[str, object]] = {}
    for row in parent_rows:
        issue_key = str(row["source_ref"] or "").strip().upper()
        if not issue_key:
            continue
        source_payload = {"backfilled_from": "workflow_executions"}
        values = {
            "work_item_id": f"parent:{row['execution_id']}",
            "item_kind": "parent",
            "tenant_id": row["tenant_id"],
            "project_id": row["project_id"],
            "parent_workflow_id": row["workflow_id"],
            "parent_execution_id": row["execution_id"],
            "issue_key": issue_key,
            "parent_issue_key": None,
            "issue_summary": row["display_name"],
            "issue_status": row["status"],
            "issue_type": None,
            "mb_work_state": _backfilled_parent_work_state(row["status"]),
            "source_system": row["source_system"],
            "source_external_id": row["source_external_id"],
            "source_payload_json": _json_value(bind, source_payload),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "last_seen_at": row["updated_at"],
        }
        _insert_work_item(bind, values)
        parent_by_issue_key[issue_key] = values

    if not (_table_exists(bind, "workflow_operation_work_units") and _table_exists(bind, "workflow_operations")):
        return

    classification_rows = bind.execute(
        sa.text(
            """
            SELECT wu.output_json
            FROM workflow_operation_work_units wu
            JOIN workflow_operations opn ON opn.operation_id = wu.operation_id
            WHERE wu.unit_key = 'jira_issue_classification.compute'
              AND wu.status = 'completed'
            ORDER BY wu.completed_at ASC, wu.updated_at ASC
            """
        )
    ).mappings()
    seen_child_ids: set[str] = set()
    for row in classification_rows:
        payload = _parse_json_payload(row["output_json"])
        for raw_item in list(payload.get("items") or []):
            if not isinstance(raw_item, dict):
                continue
            if str(raw_item.get("classification") or "").strip() != "engineering_child":
                continue
            issue = raw_item.get("issue")
            if not isinstance(issue, dict):
                continue
            issue_key = str(issue.get("key") or "").strip().upper()
            parent_key = str(issue.get("parent_key") or "").strip().upper()
            if not issue_key or not parent_key:
                continue
            parent = parent_by_issue_key.get(parent_key)
            if parent is None:
                continue
            work_item_id = f"child:{parent['parent_execution_id']}:{issue_key}"
            if work_item_id in seen_child_ids:
                continue
            seen_child_ids.add(work_item_id)
            _insert_work_item(
                bind,
                {
                    "work_item_id": work_item_id,
                    "item_kind": "child",
                    "tenant_id": parent["tenant_id"],
                    "project_id": parent["project_id"],
                    "parent_workflow_id": parent["parent_workflow_id"],
                    "parent_execution_id": parent["parent_execution_id"],
                    "issue_key": issue_key,
                    "parent_issue_key": parent_key,
                    "issue_summary": str(issue.get("summary") or "").strip() or None,
                    "issue_status": str(issue.get("status") or "").strip() or None,
                    "issue_type": str(issue.get("issue_type") or "").strip() or None,
                    "mb_work_state": str(issue.get("mb_work_state") or "").strip() or None,
                    "source_system": "jira",
                    "source_external_id": str(issue.get("issue_id") or "").strip() or None,
                    "source_payload_json": _json_value(bind, {"backfilled_from": "jira_issue_classification.compute"}),
                    "created_at": parent["created_at"],
                    "updated_at": parent["updated_at"],
                    "last_seen_at": parent["last_seen_at"],
                },
            )


def downgrade() -> None:
    raise RuntimeError("Executable work item projection migration cannot be downgraded")
