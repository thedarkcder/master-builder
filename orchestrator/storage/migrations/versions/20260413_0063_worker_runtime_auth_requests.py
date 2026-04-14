"""persist worker runtime auth requests

Revision ID: 20260413_0063
Revises: 20260412_0062
Create Date: 2026-04-13 00:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260413_0063"
down_revision = "20260412_0062"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists("worker_runtime_auth_requests"):
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
    if not _index_exists("worker_runtime_auth_requests", "ix_worker_runtime_auth_requests_scope"):
        op.create_index(
            "ix_worker_runtime_auth_requests_scope",
            "worker_runtime_auth_requests",
            ["service_instance_id", "runtime_kind", "status"],
        )
    if not _index_exists("worker_runtime_auth_requests", "ix_worker_runtime_auth_requests_requested_at"):
        op.create_index(
            "ix_worker_runtime_auth_requests_requested_at",
            "worker_runtime_auth_requests",
            ["requested_at"],
        )


def downgrade() -> None:
    if _index_exists("worker_runtime_auth_requests", "ix_worker_runtime_auth_requests_requested_at"):
        op.drop_index("ix_worker_runtime_auth_requests_requested_at", table_name="worker_runtime_auth_requests")
    if _index_exists("worker_runtime_auth_requests", "ix_worker_runtime_auth_requests_scope"):
        op.drop_index("ix_worker_runtime_auth_requests_scope", table_name="worker_runtime_auth_requests")
    if _table_exists("worker_runtime_auth_requests"):
        op.drop_table("worker_runtime_auth_requests")
