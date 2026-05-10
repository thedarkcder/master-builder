"""add generic webhook job queue tables

Revision ID: 20260323_0038
Revises: 20260323_0037
Create Date: 2026-03-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260323_0038"
down_revision = "20260323_0037"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    if not _table_exists("webhook_jobs"):
        op.create_table(
            "webhook_jobs",
            sa.Column("job_id", sa.String(length=64), nullable=False),
            sa.Column("transport", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=True),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("subject_key", sa.String(length=512), nullable=False),
            sa.Column("dedupe_key", sa.String(length=255), nullable=True),
            sa.Column("request_id", sa.String(length=64), nullable=False),
            sa.Column("event_type", sa.String(length=128), nullable=True),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("owner_id", sa.String(length=128), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("payload_json", sa.JSON(), nullable=False),
            sa.Column("context_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("job_id"),
            sa.UniqueConstraint(
                "transport",
                "tenant_id",
                "dedupe_key",
                name="uq_webhook_jobs_transport_tenant_dedupe",
            ),
        )

    if not _has_index("webhook_jobs", "ix_webhook_jobs_status_available_created"):
        op.create_index(
            "ix_webhook_jobs_status_available_created",
            "webhook_jobs",
            ["status", "available_at", "created_at"],
            unique=False,
        )
    if not _has_index("webhook_jobs", "ix_webhook_jobs_transport_subject_status_created"):
        op.create_index(
            "ix_webhook_jobs_transport_subject_status_created",
            "webhook_jobs",
            ["transport", "subject_key", "status", "created_at"],
            unique=False,
        )
    for index_name, column_name in (
        ("ix_webhook_jobs_transport", "transport"),
        ("ix_webhook_jobs_tenant_id", "tenant_id"),
        ("ix_webhook_jobs_project_id", "project_id"),
        ("ix_webhook_jobs_subject_key", "subject_key"),
        ("ix_webhook_jobs_dedupe_key", "dedupe_key"),
        ("ix_webhook_jobs_request_id", "request_id"),
        ("ix_webhook_jobs_event_type", "event_type"),
        ("ix_webhook_jobs_status", "status"),
        ("ix_webhook_jobs_owner_id", "owner_id"),
        ("ix_webhook_jobs_lease_expires_at", "lease_expires_at"),
        ("ix_webhook_jobs_available_at", "available_at"),
        ("ix_webhook_jobs_created_at", "created_at"),
        ("ix_webhook_jobs_started_at", "started_at"),
        ("ix_webhook_jobs_completed_at", "completed_at"),
    ):
        if not _has_index("webhook_jobs", index_name):
            op.create_index(index_name, "webhook_jobs", [column_name], unique=False)

    if not _table_exists("webhook_subject_claims"):
        op.create_table(
            "webhook_subject_claims",
            sa.Column("subject_key", sa.String(length=512), nullable=False),
            sa.Column("owner_id", sa.String(length=128), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("subject_key"),
        )
    for index_name, columns in (
        ("ix_webhook_subject_claims_owner_id", ["owner_id"]),
        ("ix_webhook_subject_claims_lease_expires_at", ["lease_expires_at"]),
    ):
        if not _has_index("webhook_subject_claims", index_name):
            op.create_index(index_name, "webhook_subject_claims", columns, unique=False)


def downgrade() -> None:
    for table_name, index_names in (
        (
            "webhook_subject_claims",
            [
                "ix_webhook_subject_claims_lease_expires_at",
                "ix_webhook_subject_claims_owner_id",
            ],
        ),
        (
            "webhook_jobs",
            [
                "ix_webhook_jobs_completed_at",
                "ix_webhook_jobs_started_at",
                "ix_webhook_jobs_created_at",
                "ix_webhook_jobs_available_at",
                "ix_webhook_jobs_lease_expires_at",
                "ix_webhook_jobs_owner_id",
                "ix_webhook_jobs_status",
                "ix_webhook_jobs_event_type",
                "ix_webhook_jobs_request_id",
                "ix_webhook_jobs_dedupe_key",
                "ix_webhook_jobs_subject_key",
                "ix_webhook_jobs_project_id",
                "ix_webhook_jobs_tenant_id",
                "ix_webhook_jobs_transport",
                "ix_webhook_jobs_transport_subject_status_created",
                "ix_webhook_jobs_status_available_created",
            ],
        ),
    ):
        if _table_exists(table_name):
            for index_name in index_names:
                if _has_index(table_name, index_name):
                    op.drop_index(index_name, table_name=table_name)
            op.drop_table(table_name)
