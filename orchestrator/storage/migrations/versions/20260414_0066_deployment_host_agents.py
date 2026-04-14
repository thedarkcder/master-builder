"""add deployment host agents and commands

Revision ID: 20260414_0066
Revises: 20260414_0065
Create Date: 2026-04-14 18:05:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260414_0066"
down_revision = "20260414_0065"
branch_labels = None
depends_on = None


def _has_column(inspector, table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "deployment_hosts" not in existing_tables:
        op.create_table(
            "deployment_hosts",
            sa.Column("host_id", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("provider", sa.String(length=64), nullable=False),
            sa.Column("infrastructure_provider", sa.String(length=64), nullable=True),
            sa.Column("region", sa.String(length=128), nullable=True),
            sa.Column("capability_keys_json", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("agent_version", sa.String(length=64), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("state", sa.String(length=32), nullable=False, server_default=sa.text("'provisioning'")),
            sa.Column("bootstrap_token_hash", sa.String(length=255), nullable=True),
            sa.Column("access_token_hash", sa.String(length=255), nullable=True),
            sa.Column("registered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("host_id"),
            sa.UniqueConstraint("bootstrap_token_hash"),
            sa.UniqueConstraint("access_token_hash"),
        )
        inspector = inspect(bind)

    deployment_host_indexes = {index["name"] for index in inspector.get_indexes("deployment_hosts")}
    for index_name, columns in (
        ("ix_deployment_hosts_provider", ["provider"]),
        ("ix_deployment_hosts_state", ["state"]),
        ("ix_deployment_hosts_last_seen_at", ["last_seen_at"]),
        ("ix_deployment_hosts_state_last_seen_at", ["state", "last_seen_at"]),
        ("ix_deployment_hosts_provider_region", ["provider", "region"]),
    ):
        if index_name not in deployment_host_indexes:
            op.create_index(index_name, "deployment_hosts", columns, unique=False)

    if "deployment_host_commands" not in existing_tables:
        op.create_table(
            "deployment_host_commands",
            sa.Column("command_id", sa.String(length=64), nullable=False),
            sa.Column("host_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=True),
            sa.Column("project_id", sa.String(length=128), nullable=True),
            sa.Column("app_id", sa.String(length=128), nullable=True),
            sa.Column("restore_run_id", sa.String(length=64), nullable=True),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'queued'")),
            sa.Column("claim_id", sa.String(length=64), nullable=True),
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("payload_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("result_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["host_id"], ["deployment_hosts.host_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["app_id"], ["project_apps.app_id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["restore_run_id"], ["project_deployment_restore_runs.restore_run_id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("command_id"),
        )
        inspector = inspect(bind)

    command_indexes = {index["name"] for index in inspector.get_indexes("deployment_host_commands")}
    for index_name, columns in (
        ("ix_deployment_host_commands_host_id", ["host_id"]),
        ("ix_deployment_host_commands_tenant_id", ["tenant_id"]),
        ("ix_deployment_host_commands_project_id", ["project_id"]),
        ("ix_deployment_host_commands_app_id", ["app_id"]),
        ("ix_deployment_host_commands_kind", ["kind"]),
        ("ix_deployment_host_commands_status", ["status"]),
        ("ix_deployment_host_commands_claim_id", ["claim_id"]),
        ("ix_deployment_host_commands_lease_expires_at", ["lease_expires_at"]),
        ("ix_deployment_host_commands_available_at", ["available_at"]),
        ("ix_deployment_host_commands_host_status_available_at", ["host_id", "status", "available_at"]),
        ("ix_deployment_host_commands_restore_run_id", ["restore_run_id"]),
        ("ix_deployment_host_commands_tenant_project_created_at", ["tenant_id", "project_id", "created_at"]),
    ):
        if index_name not in command_indexes:
            op.create_index(index_name, "deployment_host_commands", columns, unique=False)

    inspector = inspect(bind)
    if _has_column(inspector, "project_deployment_restore_runs", "host_id") is False:
        op.add_column("project_deployment_restore_runs", sa.Column("host_id", sa.String(length=64), nullable=True))
    if _has_column(inspector, "project_deployment_restore_runs", "command_id") is False:
        op.add_column("project_deployment_restore_runs", sa.Column("command_id", sa.String(length=64), nullable=True))
    inspector = inspect(bind)
    restore_indexes = {index["name"] for index in inspector.get_indexes("project_deployment_restore_runs")}
    if "ix_project_deployment_restore_runs_host_id" not in restore_indexes:
        op.create_index("ix_project_deployment_restore_runs_host_id", "project_deployment_restore_runs", ["host_id"], unique=False)
    if "ix_project_deployment_restore_runs_command_id" not in restore_indexes:
        op.create_index("ix_project_deployment_restore_runs_command_id", "project_deployment_restore_runs", ["command_id"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if "project_deployment_restore_runs" in set(inspector.get_table_names()):
        restore_indexes = {index["name"] for index in inspector.get_indexes("project_deployment_restore_runs")}
        if "ix_project_deployment_restore_runs_command_id" in restore_indexes:
            op.drop_index("ix_project_deployment_restore_runs_command_id", table_name="project_deployment_restore_runs")
        if "ix_project_deployment_restore_runs_host_id" in restore_indexes:
            op.drop_index("ix_project_deployment_restore_runs_host_id", table_name="project_deployment_restore_runs")
        inspector = inspect(bind)
        if _has_column(inspector, "project_deployment_restore_runs", "command_id"):
            op.drop_column("project_deployment_restore_runs", "command_id")
        if _has_column(inspector, "project_deployment_restore_runs", "host_id"):
            op.drop_column("project_deployment_restore_runs", "host_id")

    if "deployment_host_commands" in set(inspector.get_table_names()):
        command_indexes = {index["name"] for index in inspector.get_indexes("deployment_host_commands")}
        for index_name in (
            "ix_deployment_host_commands_tenant_project_created_at",
            "ix_deployment_host_commands_restore_run_id",
            "ix_deployment_host_commands_host_status_available_at",
            "ix_deployment_host_commands_available_at",
            "ix_deployment_host_commands_lease_expires_at",
            "ix_deployment_host_commands_claim_id",
            "ix_deployment_host_commands_status",
            "ix_deployment_host_commands_kind",
            "ix_deployment_host_commands_app_id",
            "ix_deployment_host_commands_project_id",
            "ix_deployment_host_commands_tenant_id",
            "ix_deployment_host_commands_host_id",
        ):
            if index_name in command_indexes:
                op.drop_index(index_name, table_name="deployment_host_commands")
        op.drop_table("deployment_host_commands")

    if "deployment_hosts" in set(inspector.get_table_names()):
        host_indexes = {index["name"] for index in inspector.get_indexes("deployment_hosts")}
        for index_name in (
            "ix_deployment_hosts_provider_region",
            "ix_deployment_hosts_state_last_seen_at",
            "ix_deployment_hosts_last_seen_at",
            "ix_deployment_hosts_state",
            "ix_deployment_hosts_provider",
        ):
            if index_name in host_indexes:
                op.drop_index(index_name, table_name="deployment_hosts")
        op.drop_table("deployment_hosts")
