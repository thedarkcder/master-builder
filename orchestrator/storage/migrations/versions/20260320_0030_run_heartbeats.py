"""add worker heartbeat ownership fields to runs

Revision ID: 20260320_0030
Revises: 20260313_0029
Create Date: 2026-03-20 13:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "20260320_0030"
down_revision = "20260313_0029"
branch_labels = None
depends_on = None
_RUN_HEARTBEAT_LOCK_KEY = 202603200030


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _RUN_HEARTBEAT_LOCK_KEY},
        )

    if not _has_column("runs", "last_heartbeat_at"):
        op.add_column("runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column("runs", "worker_service_instance_id"):
        op.add_column("runs", sa.Column("worker_service_instance_id", sa.String(length=128), nullable=True))

    for index_name, columns in (
        ("ix_runs_last_heartbeat_at", ["last_heartbeat_at"]),
        ("ix_runs_worker_service_instance_id", ["worker_service_instance_id"]),
        ("ix_runs_status_last_heartbeat_at", ["status", "last_heartbeat_at"]),
    ):
        if not _has_index("runs", index_name):
            op.create_index(index_name, "runs", columns, unique=False)


def downgrade() -> None:
    for index_name in (
        "ix_runs_status_last_heartbeat_at",
        "ix_runs_worker_service_instance_id",
        "ix_runs_last_heartbeat_at",
    ):
        if _has_index("runs", index_name):
            op.drop_index(index_name, table_name="runs")

    if _has_column("runs", "worker_service_instance_id"):
        op.drop_column("runs", "worker_service_instance_id")
    if _has_column("runs", "last_heartbeat_at"):
        op.drop_column("runs", "last_heartbeat_at")
