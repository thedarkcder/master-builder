"""Require attempt identity for operation-scoped observability.

Revision ID: 20260424_0092
Revises: 20260424_0091
Create Date: 2026-04-24 17:30:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa


revision = "20260424_0092"
down_revision = "20260424_0091"
branch_labels = None
depends_on = None


def _has_check_constraint(table_name: str, constraint_name: str) -> bool:
    return constraint_name in {
        constraint["name"]
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(table_name)
    }


def _payload_attempt_number(payload: object) -> int | None:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    attempt = payload.get("attempt")
    if attempt is None:
        return None
    try:
        return int(attempt)
    except (TypeError, ValueError):
        return None


def _backfill_attempt_ids(table_name: str) -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    events = sa.Table(
        table_name,
        metadata,
        sa.Column("event_id", sa.String),
        sa.Column("stream_offset", sa.Integer),
        sa.Column("operation_id", sa.String),
        sa.Column("attempt_id", sa.String),
        sa.Column("payload_json", sa.JSON),
    )
    attempts = sa.Table(
        "workflow_operation_attempts",
        metadata,
        sa.Column("attempt_id", sa.String),
        sa.Column("operation_id", sa.String),
        sa.Column("attempt_number", sa.Integer),
    )

    primary_key_column = (
        events.c.event_id if table_name == "audit_events" else events.c.stream_offset
    )
    rows = bind.execute(
        sa.select(
            primary_key_column.label("row_id"),
            events.c.operation_id,
            events.c.payload_json,
        ).where(
            events.c.operation_id.is_not(None),
            events.c.attempt_id.is_(None),
        )
    ).mappings()

    for row in rows:
        attempt_number = _payload_attempt_number(row["payload_json"])
        if attempt_number is None:
            continue
        attempt_id = bind.execute(
            sa.select(attempts.c.attempt_id)
            .where(
                attempts.c.operation_id == row["operation_id"],
                attempts.c.attempt_number == attempt_number,
            )
            .limit(1)
        ).scalar_one_or_none()
        if attempt_id is None:
            continue
        bind.execute(
            events.update()
            .where(primary_key_column == row["row_id"])
            .values(attempt_id=attempt_id)
        )

    bind.execute(
        events.delete().where(
            events.c.operation_id.is_not(None),
            events.c.attempt_id.is_(None),
        )
    )


def upgrade() -> None:
    _backfill_attempt_ids("observability_stream_events")
    _backfill_attempt_ids("audit_events")
    if not _has_check_constraint(
        "observability_stream_events",
        "ck_observability_stream_events_operation_requires_attempt",
    ):
        with op.batch_alter_table("observability_stream_events") as batch_op:
            batch_op.create_check_constraint(
                "ck_observability_stream_events_operation_requires_attempt",
                "operation_id IS NULL OR attempt_id IS NOT NULL",
            )
    if not _has_check_constraint(
        "audit_events", "ck_audit_events_operation_requires_attempt"
    ):
        with op.batch_alter_table("audit_events") as batch_op:
            batch_op.create_check_constraint(
                "ck_audit_events_operation_requires_attempt",
                "operation_id IS NULL OR attempt_id IS NOT NULL",
            )


def downgrade() -> None:
    with op.batch_alter_table("audit_events") as batch_op:
        batch_op.drop_constraint(
            "ck_audit_events_operation_requires_attempt", type_="check"
        )
    with op.batch_alter_table("observability_stream_events") as batch_op:
        batch_op.drop_constraint(
            "ck_observability_stream_events_operation_requires_attempt", type_="check"
        )
