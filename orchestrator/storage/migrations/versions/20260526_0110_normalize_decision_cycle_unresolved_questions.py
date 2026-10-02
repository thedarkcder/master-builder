"""Normalize Decision Gate unresolved question state.

Revision ID: 20260526_0110
Revises: 20260526_0109
Create Date: 2026-05-26 14:00:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "20260526_0110"
down_revision = "20260526_0109"
branch_labels = None
depends_on = None


def _parse_json_list(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    return []


def _json_expr(bind: sa.engine.Connection, parameter_name: str) -> str:
    return (
        f"CAST(:{parameter_name} AS JSON)"
        if bind.dialect.name == "postgresql"
        else f":{parameter_name}"
    )


def _question_requires_followup(item: dict[str, object]) -> bool:
    status = str(item.get("status") or "").strip().lower()
    if status == "accepted":
        return False
    if isinstance(item.get("unresolved"), bool):
        return bool(item["unresolved"])
    return status in {"", "open", "answered"}


def _normalize_question_set(
    question_set: list[object],
) -> tuple[list[object], list[str], bool]:
    changed = False
    normalized_items: list[object] = []
    unresolved_ids: list[str] = []
    for item in question_set:
        if not isinstance(item, dict):
            normalized_items.append(item)
            continue
        normalized = dict(item)
        question_id = str(
            normalized.get("id") or normalized.get("question_id") or ""
        ).strip()
        requires_followup = _question_requires_followup(normalized)
        if normalized.get("unresolved") is not requires_followup:
            normalized["unresolved"] = requires_followup
            changed = True
        if question_id and requires_followup:
            unresolved_ids.append(question_id)
        normalized_items.append(normalized)
    return normalized_items, unresolved_ids, changed


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("decision_cycles"):
        return

    question_set_expr = _json_expr(bind, "question_set_json")
    unresolved_expr = _json_expr(bind, "unresolved_question_ids_json")
    update_sql = sa.text(
        f"""
        UPDATE decision_cycles
        SET question_set_json = {question_set_expr},
            unresolved_question_ids_json = {unresolved_expr},
            updated_at = CURRENT_TIMESTAMP
        WHERE cycle_id = :cycle_id
        """
    )
    rows = bind.execute(
        sa.text(
            """
            SELECT cycle_id, question_set_json, unresolved_question_ids_json
            FROM decision_cycles
            WHERE status = 'open'
            """
        )
    ).mappings()
    for row in rows:
        question_set, unresolved_ids, changed = _normalize_question_set(
            _parse_json_list(row["question_set_json"])
        )
        existing_unresolved_ids = [
            str(item).strip()
            for item in _parse_json_list(row["unresolved_question_ids_json"])
            if str(item).strip()
        ]
        if unresolved_ids != existing_unresolved_ids:
            changed = True
        if not changed:
            continue
        bind.execute(
            update_sql,
            {
                "cycle_id": row["cycle_id"],
                "question_set_json": json.dumps(question_set, sort_keys=True),
                "unresolved_question_ids_json": json.dumps(
                    unresolved_ids, sort_keys=True
                ),
            },
        )


def downgrade() -> None:
    raise RuntimeError(
        "Decision Gate unresolved question normalization cannot be downgraded"
    )
