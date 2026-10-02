"""Backfill single-member Decision Gate owner questions.

Revision ID: 20260526_0109
Revises: 20260526_0108
Create Date: 2026-05-26 13:30:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "20260526_0109"
down_revision = "20260526_0108"
branch_labels = None
depends_on = None

DECISION_OWNER_QUESTION_ID = "decision_owner"
IMPLICIT_OWNER_DETAIL = (
    "Single-member account; the accountable decision owner is implicit."
)


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


def _single_member_tenant_ids(bind: sa.engine.Connection) -> set[str]:
    tenant_ids = {
        str(row["tenant_id"] or "").strip()
        for row in bind.execute(
            sa.text("SELECT DISTINCT tenant_id FROM decision_cycles")
        ).mappings()
        if str(row["tenant_id"] or "").strip()
    }
    active_members: dict[str, set[str]] = {tenant_id: set() for tenant_id in tenant_ids}
    member_rows = bind.execute(
        sa.text(
            """
            SELECT tm.tenant_id, tm.user_id
            FROM tenant_memberships tm
            JOIN tenant_users tu ON tu.user_id = tm.user_id
            WHERE tu.is_active = :active
            """
        ),
        {"active": True},
    ).mappings()
    for row in member_rows:
        tenant_id = str(row["tenant_id"] or "").strip()
        user_id = str(row["user_id"] or "").strip()
        if tenant_id in active_members and user_id:
            active_members[tenant_id].add(user_id)
    return {
        tenant_id
        for tenant_id, user_ids in active_members.items()
        if len(user_ids) <= 1
    }


def _apply_single_member_owner_policy(
    *,
    question_set: list[object],
    unresolved_question_ids: list[object],
) -> tuple[list[object], list[str], bool]:
    changed = False
    updated_question_set: list[object] = []
    for item in question_set:
        if (
            not isinstance(item, dict)
            or str(item.get("id") or "").strip() != DECISION_OWNER_QUESTION_ID
        ):
            updated_question_set.append(item)
            continue
        updated_item = dict(item)
        if updated_item.get("status") != "accepted":
            updated_item["status"] = "accepted"
            changed = True
        if updated_item.get("detail") != IMPLICIT_OWNER_DETAIL:
            updated_item["detail"] = IMPLICIT_OWNER_DETAIL
            changed = True
        if updated_item.get("unresolved") is not False:
            updated_item["unresolved"] = False
            changed = True
        updated_question_set.append(updated_item)

    updated_unresolved = [
        str(item).strip()
        for item in unresolved_question_ids
        if str(item).strip() and str(item).strip() != DECISION_OWNER_QUESTION_ID
    ]
    if updated_unresolved != [
        str(item).strip() for item in unresolved_question_ids if str(item).strip()
    ]:
        changed = True

    return updated_question_set, updated_unresolved, changed


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required_tables = {"decision_cycles", "tenant_memberships", "tenant_users"}
    if not required_tables.issubset(set(inspector.get_table_names())):
        return

    single_member_tenant_ids = _single_member_tenant_ids(bind)
    if not single_member_tenant_ids:
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
            SELECT cycle_id, tenant_id, question_set_json, unresolved_question_ids_json
            FROM decision_cycles
            WHERE status = 'open'
            """
        )
    ).mappings()
    for row in rows:
        tenant_id = str(row["tenant_id"] or "").strip()
        if tenant_id not in single_member_tenant_ids:
            continue
        question_set, unresolved_ids, changed = _apply_single_member_owner_policy(
            question_set=_parse_json_list(row["question_set_json"]),
            unresolved_question_ids=_parse_json_list(
                row["unresolved_question_ids_json"]
            ),
        )
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
        "Single-member Decision Gate owner question backfill cannot be downgraded"
    )
