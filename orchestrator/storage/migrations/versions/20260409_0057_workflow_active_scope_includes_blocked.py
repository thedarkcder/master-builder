"""align workflow active-scope uniqueness with blocked state

Revision ID: 20260409_0057
Revises: 20260409_0056
Create Date: 2026-04-09 18:20:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone
from collections import defaultdict

from alembic import op
import sqlalchemy as sa


revision = "20260409_0057"
down_revision = "20260409_0056"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def _normalize_active_scope_duplicates() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT workflow_id, tenant_id, issue_key, dedupe_scope, status, created_at, updated_at
            FROM workflow_executions
            WHERE status IN ('queued', 'running', 'waiting_for_input', 'blocked')
            """
        )
    ).mappings().all()
    grouped: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        tenant_id = str(row["tenant_id"] or "").strip()
        issue_key = str(row["issue_key"] or "").strip()
        dedupe_scope = str(row["dedupe_scope"] or "").strip()
        grouped[(tenant_id, issue_key, dedupe_scope)].append(dict(row))

    def _epoch(value: object) -> float:
        if value is None:
            return 0.0
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return 0.0
            normalized = text.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(normalized).timestamp()
            except ValueError:
                return 0.0
        return 0.0

    def _priority(status: str) -> int:
        normalized = status.strip().lower()
        if normalized == "running":
            return 0
        if normalized == "queued":
            return 1
        if normalized == "waiting_for_input":
            return 2
        if normalized == "blocked":
            return 3
        return 4

    now = datetime.now(timezone.utc)
    for _, duplicates in grouped.items():
        if len(duplicates) <= 1:
            continue
        duplicates.sort(
            key=lambda row: (
                _priority(str(row.get("status") or "")),
                -_epoch(row.get("updated_at")),
                -_epoch(row.get("created_at")),
            )
        )
        canonical = duplicates[0]
        for duplicate in duplicates[1:]:
            bind.execute(
                sa.text(
                    """
                    UPDATE workflow_executions
                    SET status = 'cancelled',
                        finished_at = COALESCE(finished_at, :now),
                        updated_at = :now,
                        blocked_reason = COALESCE(blocked_reason, 'superseded_by_active_workflow:' || :canonical_workflow_id)
                    WHERE workflow_id = :workflow_id
                    """
                ),
                {
                    "workflow_id": str(duplicate["workflow_id"]),
                    "canonical_workflow_id": str(canonical["workflow_id"]),
                    "now": now,
                },
            )


def upgrade() -> None:
    if "workflow_executions" not in _table_names():
        return
    _normalize_active_scope_duplicates()
    if _has_index("workflow_executions", "uq_workflow_executions_active_scope"):
        op.drop_index("uq_workflow_executions_active_scope", table_name="workflow_executions")
    op.create_index(
        "uq_workflow_executions_active_scope",
        "workflow_executions",
        ["tenant_id", "issue_key", "dedupe_scope"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'waiting_for_input', 'blocked')"),
        sqlite_where=sa.text("status IN ('queued', 'running', 'waiting_for_input', 'blocked')"),
    )


def downgrade() -> None:
    if "workflow_executions" not in _table_names():
        return
    if _has_index("workflow_executions", "uq_workflow_executions_active_scope"):
        op.drop_index("uq_workflow_executions_active_scope", table_name="workflow_executions")
    op.create_index(
        "uq_workflow_executions_active_scope",
        "workflow_executions",
        ["tenant_id", "issue_key", "dedupe_scope"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'waiting_for_input')"),
        sqlite_where=sa.text("status IN ('queued', 'running', 'waiting_for_input')"),
    )
