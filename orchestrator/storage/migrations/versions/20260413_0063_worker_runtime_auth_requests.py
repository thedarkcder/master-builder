"""persist worker runtime auth requests

Revision ID: 20260413_0063
Revises: 20260412_0062
Create Date: 2026-04-13 00:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, ProgrammingError


revision = "20260413_0063"
down_revision = "20260412_0062"
branch_labels = None
depends_on = None

_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_worker_runtime_auth_requests_scope", ["service_instance_id", "runtime_kind", "status"]),
    ("ix_worker_runtime_auth_requests_requested_at", ["requested_at"]),
)


def _table_exists(bind, table_name: str) -> bool:
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def _index_exists(bind, table_name: str, index_name: str) -> bool:
    inspector = inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def _is_duplicate_table_error(exc: Exception) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = str(getattr(orig, "pgcode", "") or getattr(orig, "sqlstate", "") or "").strip()
    if sqlstate == "42P07":
        return True
    return "already exists" in str(orig or exc).lower()


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "worker_runtime_auth_requests"):
        try:
            op.create_table(
                "worker_runtime_auth_requests",
                sa.Column("request_id", sa.String(length=64), nullable=False),
                sa.Column("service_instance_id", sa.String(length=128), nullable=False),
                sa.Column("runtime_kind", sa.String(length=64), nullable=False),
                sa.Column("status", sa.String(length=32), nullable=False),
                sa.Column("remediation_text", sa.Text(), nullable=True),
                sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("last_error", sa.Text(), nullable=True),
                sa.ForeignKeyConstraint(
                    ["service_instance_id"],
                    ["worker_runtime_states.service_instance_id"],
                    ondelete="CASCADE",
                ),
                sa.PrimaryKeyConstraint("request_id"),
            )
        except (IntegrityError, ProgrammingError) as exc:
            if not _is_duplicate_table_error(exc) and not _table_exists(bind, "worker_runtime_auth_requests"):
                raise

    if _table_exists(bind, "worker_runtime_auth_requests"):
        for index_name, columns in _INDEXES:
            if not _index_exists(bind, "worker_runtime_auth_requests", index_name):
                op.create_index(index_name, "worker_runtime_auth_requests", columns)


def downgrade() -> None:
    bind = op.get_bind()
    if _table_exists(bind, "worker_runtime_auth_requests"):
        if _index_exists(bind, "worker_runtime_auth_requests", "ix_worker_runtime_auth_requests_requested_at"):
            op.drop_index("ix_worker_runtime_auth_requests_requested_at", table_name="worker_runtime_auth_requests")
        if _index_exists(bind, "worker_runtime_auth_requests", "ix_worker_runtime_auth_requests_scope"):
            op.drop_index("ix_worker_runtime_auth_requests_scope", table_name="worker_runtime_auth_requests")
        op.drop_table("worker_runtime_auth_requests")
