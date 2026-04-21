"""backfill observability stream attempt ids

Revision ID: 20260421_0086
Revises: 20260421_0085
Create Date: 2026-04-21 14:25:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260421_0086"
down_revision = "20260421_0085"
branch_labels = None
depends_on = None


observability_stream_events = sa.table(
    "observability_stream_events",
    sa.column("stream_offset", sa.Integer),
    sa.column("operation_id", sa.String),
    sa.column("attempt_id", sa.String),
    sa.column("payload_json", sa.JSON),
)

workflow_operation_attempts = sa.table(
    "workflow_operation_attempts",
    sa.column("attempt_id", sa.String),
    sa.column("operation_id", sa.String),
    sa.column("attempt_number", sa.Integer),
)


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            observability_stream_events.c.stream_offset,
            observability_stream_events.c.operation_id,
            observability_stream_events.c.payload_json,
        ).where(
            observability_stream_events.c.attempt_id.is_(None),
            observability_stream_events.c.operation_id.is_not(None),
        )
    ).mappings()

    for row in rows:
        payload = row.get("payload_json")
        if not isinstance(payload, dict):
            continue
        raw_attempt = payload.get("attempt")
        try:
            attempt_number = int(raw_attempt)
        except (TypeError, ValueError):
            continue
        operation_id = str(row.get("operation_id") or "").strip()
        if not operation_id:
            continue
        attempt_id = bind.execute(
            sa.select(workflow_operation_attempts.c.attempt_id)
            .where(
                workflow_operation_attempts.c.operation_id == operation_id,
                workflow_operation_attempts.c.attempt_number == attempt_number,
            )
            .limit(1)
        ).scalar_one_or_none()
        if not attempt_id:
            continue
        bind.execute(
            sa.update(observability_stream_events)
            .where(observability_stream_events.c.stream_offset == row["stream_offset"])
            .values(attempt_id=str(attempt_id))
        )


def downgrade() -> None:
    pass
