"""add knowledge jira sync runtime status tables

Revision ID: 20260313_0026
Revises: 20260311_0025
Create Date: 2026-03-13 11:40:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260313_0026"
down_revision = "20260311_0025"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    if not _has_table("knowledge_jira_sync_runtime_states"):
        op.create_table(
            "knowledge_jira_sync_runtime_states",
            sa.Column("runtime_name", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("database_backend", sa.String(length=32), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "last_pass_started_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column(
                "last_pass_finished_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("leader_acquired", sa.Boolean(), nullable=False),
            sa.Column("service_instance_id", sa.String(length=128), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("runtime_name"),
        )
    if not _has_index(
        "knowledge_jira_sync_runtime_states",
        "ix_knowledge_jira_sync_runtime_states_updated_at",
    ):
        op.create_index(
            "ix_knowledge_jira_sync_runtime_states_updated_at",
            "knowledge_jira_sync_runtime_states",
            ["updated_at"],
            unique=False,
        )

    if not _has_table("knowledge_jira_sync_project_states"):
        op.create_table(
            "knowledge_jira_sync_project_states",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("runtime_name", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=False),
            sa.Column("jira_project_key", sa.String(length=64), nullable=False),
            sa.Column("state", sa.String(length=32), nullable=False),
            sa.Column("failure_category", sa.String(length=64), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "last_successful_sync_at", sa.DateTime(timezone=True), nullable=True
            ),
            sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["runtime_name"],
                ["knowledge_jira_sync_runtime_states.runtime_name"],
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "runtime_name",
                "tenant_id",
                "project_id",
                name="uq_knowledge_jira_sync_project_states_scope",
            ),
        )
    if not _has_index(
        "knowledge_jira_sync_project_states",
        "ix_knowledge_jira_sync_project_states_runtime_name",
    ):
        op.create_index(
            "ix_knowledge_jira_sync_project_states_runtime_name",
            "knowledge_jira_sync_project_states",
            ["runtime_name"],
            unique=False,
        )
    if not _has_index(
        "knowledge_jira_sync_project_states",
        "ix_knowledge_jira_sync_project_states_tenant_id",
    ):
        op.create_index(
            "ix_knowledge_jira_sync_project_states_tenant_id",
            "knowledge_jira_sync_project_states",
            ["tenant_id"],
            unique=False,
        )
    if not _has_index(
        "knowledge_jira_sync_project_states",
        "ix_knowledge_jira_sync_project_states_project_id",
    ):
        op.create_index(
            "ix_knowledge_jira_sync_project_states_project_id",
            "knowledge_jira_sync_project_states",
            ["project_id"],
            unique=False,
        )
    if not _has_index(
        "knowledge_jira_sync_project_states",
        "ix_knowledge_jira_sync_project_states_updated_at",
    ):
        op.create_index(
            "ix_knowledge_jira_sync_project_states_updated_at",
            "knowledge_jira_sync_project_states",
            ["updated_at"],
            unique=False,
        )


def downgrade() -> None:
    if _has_table("knowledge_jira_sync_project_states"):
        if _has_index(
            "knowledge_jira_sync_project_states",
            "ix_knowledge_jira_sync_project_states_updated_at",
        ):
            op.drop_index(
                "ix_knowledge_jira_sync_project_states_updated_at",
                table_name="knowledge_jira_sync_project_states",
            )
        if _has_index(
            "knowledge_jira_sync_project_states",
            "ix_knowledge_jira_sync_project_states_project_id",
        ):
            op.drop_index(
                "ix_knowledge_jira_sync_project_states_project_id",
                table_name="knowledge_jira_sync_project_states",
            )
        if _has_index(
            "knowledge_jira_sync_project_states",
            "ix_knowledge_jira_sync_project_states_tenant_id",
        ):
            op.drop_index(
                "ix_knowledge_jira_sync_project_states_tenant_id",
                table_name="knowledge_jira_sync_project_states",
            )
        if _has_index(
            "knowledge_jira_sync_project_states",
            "ix_knowledge_jira_sync_project_states_runtime_name",
        ):
            op.drop_index(
                "ix_knowledge_jira_sync_project_states_runtime_name",
                table_name="knowledge_jira_sync_project_states",
            )
        op.drop_table("knowledge_jira_sync_project_states")
    if _has_table("knowledge_jira_sync_runtime_states"):
        if _has_index(
            "knowledge_jira_sync_runtime_states",
            "ix_knowledge_jira_sync_runtime_states_updated_at",
        ):
            op.drop_index(
                "ix_knowledge_jira_sync_runtime_states_updated_at",
                table_name="knowledge_jira_sync_runtime_states",
            )
        op.drop_table("knowledge_jira_sync_runtime_states")
