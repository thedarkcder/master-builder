"""persist worker runtime auth requests

Revision ID: 20260413_0063
Revises: 20260412_0062
Create Date: 2026-04-13 00:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260413_0063"
down_revision = "20260412_0062"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    table_names = set(inspector.get_table_names())
    if "worker_runtime_auth_requests" not in table_names:
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
    index_names = {index["name"] for index in inspector.get_indexes("worker_runtime_auth_requests")}
    if "ix_worker_runtime_auth_requests_scope" not in index_names:
        op.create_index(
            "ix_worker_runtime_auth_requests_scope",
            "worker_runtime_auth_requests",
            ["service_instance_id", "runtime_kind", "status"],
        )
    if "ix_worker_runtime_auth_requests_requested_at" not in index_names:
        op.create_index(
            "ix_worker_runtime_auth_requests_requested_at",
            "worker_runtime_auth_requests",
            ["requested_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_worker_runtime_auth_requests_requested_at", table_name="worker_runtime_auth_requests")
    op.drop_index("ix_worker_runtime_auth_requests_scope", table_name="worker_runtime_auth_requests")
    op.drop_table("worker_runtime_auth_requests")
