"""add project deployment restore runs

Revision ID: 20260414_0065
Revises: 20260414_0064
Create Date: 2026-04-14 13:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260507_0110"
down_revision = "20260507_0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_name = "project_deployment_restore_runs"
    existing_tables = set(inspector.get_table_names())

    if table_name not in existing_tables:
        op.create_table(
            table_name,
            sa.Column("restore_run_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=False),
            sa.Column("app_id", sa.String(length=128), nullable=False),
            sa.Column("backup_policy_key", sa.String(length=255), nullable=False),
            sa.Column("resource_key", sa.String(length=255), nullable=False),
            sa.Column("backup_uuid", sa.String(length=128), nullable=True),
            sa.Column("execution_uuid", sa.String(length=128), nullable=False),
            sa.Column("database_type", sa.String(length=32), nullable=False),
            sa.Column("database_uuid", sa.String(length=128), nullable=False),
            sa.Column(
                "restore_mode",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'replace'"),
            ),
            sa.Column("requested_by_user_id", sa.String(length=64), nullable=True),
            sa.Column("confirmation_value", sa.String(length=255), nullable=False),
            sa.Column(
                "execution_payload",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
            sa.Column(
                "status",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'queued'"),
            ),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["app_id"], ["project_apps.app_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("restore_run_id"),
        )
        inspector = inspect(bind)

    existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if (
        "ix_project_deployment_restore_runs_tenant_project_created_at"
        not in existing_indexes
    ):
        op.create_index(
            "ix_project_deployment_restore_runs_tenant_project_created_at",
            table_name,
            ["tenant_id", "project_id", "created_at"],
            unique=False,
        )
    if "ix_project_deployment_restore_runs_app_status" not in existing_indexes:
        op.create_index(
            "ix_project_deployment_restore_runs_app_status",
            table_name,
            ["app_id", "status"],
            unique=False,
        )
    if "ix_project_deployment_restore_runs_tenant_status" not in existing_indexes:
        op.create_index(
            "ix_project_deployment_restore_runs_tenant_status",
            table_name,
            ["tenant_id", "status"],
            unique=False,
        )
    if "ix_project_deployment_restore_runs_tenant_id" not in existing_indexes:
        op.create_index(
            "ix_project_deployment_restore_runs_tenant_id",
            table_name,
            ["tenant_id"],
            unique=False,
        )
    if "ix_project_deployment_restore_runs_project_id" not in existing_indexes:
        op.create_index(
            "ix_project_deployment_restore_runs_project_id",
            table_name,
            ["project_id"],
            unique=False,
        )
    if "ix_project_deployment_restore_runs_app_id" not in existing_indexes:
        op.create_index(
            "ix_project_deployment_restore_runs_app_id",
            table_name,
            ["app_id"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    table_name = "project_deployment_restore_runs"
    if table_name not in set(inspector.get_table_names()):
        return

    existing_indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if "ix_project_deployment_restore_runs_app_id" in existing_indexes:
        op.drop_index(
            "ix_project_deployment_restore_runs_app_id", table_name=table_name
        )
    if "ix_project_deployment_restore_runs_project_id" in existing_indexes:
        op.drop_index(
            "ix_project_deployment_restore_runs_project_id", table_name=table_name
        )
    if "ix_project_deployment_restore_runs_tenant_id" in existing_indexes:
        op.drop_index(
            "ix_project_deployment_restore_runs_tenant_id", table_name=table_name
        )
    if "ix_project_deployment_restore_runs_tenant_status" in existing_indexes:
        op.drop_index(
            "ix_project_deployment_restore_runs_tenant_status", table_name=table_name
        )
    if "ix_project_deployment_restore_runs_app_status" in existing_indexes:
        op.drop_index(
            "ix_project_deployment_restore_runs_app_status", table_name=table_name
        )
    if (
        "ix_project_deployment_restore_runs_tenant_project_created_at"
        in existing_indexes
    ):
        op.drop_index(
            "ix_project_deployment_restore_runs_tenant_project_created_at",
            table_name=table_name,
        )
    op.drop_table(table_name)
