"""add managed project knowledge sources

Revision ID: 20260313_0028
Revises: 20260313_0027
Create Date: 2026-03-13 18:10:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa


revision = "20260313_0028"
down_revision = "20260313_0027"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_table("knowledge_sources"):
        op.create_table(
            "knowledge_sources",
            sa.Column("source_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=False),
            sa.Column("connector_type", sa.String(length=64), nullable=False),
            sa.Column("display_name", sa.String(length=255), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
            sa.Column("sync_mode", sa.String(length=32), nullable=False, server_default="manual"),
            sa.Column("config_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("source_id"),
        )
    if not _has_index("knowledge_sources", "ix_knowledge_sources_tenant_id"):
        op.create_index("ix_knowledge_sources_tenant_id", "knowledge_sources", ["tenant_id"], unique=False)
    if not _has_index("knowledge_sources", "ix_knowledge_sources_project_id"):
        op.create_index("ix_knowledge_sources_project_id", "knowledge_sources", ["project_id"], unique=False)
    if not _has_index("knowledge_sources", "ix_knowledge_sources_connector_type"):
        op.create_index("ix_knowledge_sources_connector_type", "knowledge_sources", ["connector_type"], unique=False)
    if not _has_index("knowledge_sources", "ix_knowledge_sources_status"):
        op.create_index("ix_knowledge_sources_status", "knowledge_sources", ["status"], unique=False)
    if not _has_index("knowledge_sources", "ix_knowledge_sources_sync_mode"):
        op.create_index("ix_knowledge_sources_sync_mode", "knowledge_sources", ["sync_mode"], unique=False)
    if not _has_index("knowledge_sources", "ix_knowledge_sources_last_synced_at"):
        op.create_index("ix_knowledge_sources_last_synced_at", "knowledge_sources", ["last_synced_at"], unique=False)
    if not _has_index("knowledge_sources", "ix_knowledge_sources_updated_at"):
        op.create_index("ix_knowledge_sources_updated_at", "knowledge_sources", ["updated_at"], unique=False)

    projects = sa.table(
        "projects",
        sa.column("project_id", sa.String()),
        sa.column("tenant_id", sa.String()),
        sa.column("jira_project_key", sa.String()),
    )
    knowledge_sources = sa.table(
        "knowledge_sources",
        sa.column("source_id", sa.String()),
        sa.column("tenant_id", sa.String()),
        sa.column("project_id", sa.String()),
        sa.column("connector_type", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("status", sa.String()),
        sa.column("sync_mode", sa.String()),
        sa.column("config_json", sa.JSON()),
        sa.column("last_synced_at", sa.DateTime(timezone=True)),
        sa.column("last_error", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    existing_pairs = {
        (str(row[0]), str(row[1]))
        for row in bind.execute(
            sa.select(knowledge_sources.c.project_id, knowledge_sources.c.connector_type).where(
                knowledge_sources.c.connector_type == "jira"
            )
        )
    }
    now = datetime.now(timezone.utc)
    rows = []
    for project_id, tenant_id, jira_project_key in bind.execute(
        sa.select(projects.c.project_id, projects.c.tenant_id, projects.c.jira_project_key)
    ):
        project_key = str(jira_project_key or "").strip().upper()
        if not project_key:
            continue
        if (str(project_id), "jira") in existing_pairs:
            continue
        rows.append(
            {
                "source_id": uuid4().hex,
                "tenant_id": str(tenant_id),
                "project_id": str(project_id),
                "connector_type": "jira",
                "display_name": f"Jira {project_key}",
                "status": "active",
                "sync_mode": "scheduled",
                "config_json": {"project_key": project_key},
                "last_synced_at": None,
                "last_error": None,
                "created_at": now,
                "updated_at": now,
            }
        )
    if rows:
        bind.execute(sa.insert(knowledge_sources), rows)


def downgrade() -> None:
    if not _has_table("knowledge_sources"):
        return
    if _has_index("knowledge_sources", "ix_knowledge_sources_updated_at"):
        op.drop_index("ix_knowledge_sources_updated_at", table_name="knowledge_sources")
    if _has_index("knowledge_sources", "ix_knowledge_sources_last_synced_at"):
        op.drop_index("ix_knowledge_sources_last_synced_at", table_name="knowledge_sources")
    if _has_index("knowledge_sources", "ix_knowledge_sources_sync_mode"):
        op.drop_index("ix_knowledge_sources_sync_mode", table_name="knowledge_sources")
    if _has_index("knowledge_sources", "ix_knowledge_sources_status"):
        op.drop_index("ix_knowledge_sources_status", table_name="knowledge_sources")
    if _has_index("knowledge_sources", "ix_knowledge_sources_connector_type"):
        op.drop_index("ix_knowledge_sources_connector_type", table_name="knowledge_sources")
    if _has_index("knowledge_sources", "ix_knowledge_sources_project_id"):
        op.drop_index("ix_knowledge_sources_project_id", table_name="knowledge_sources")
    if _has_index("knowledge_sources", "ix_knowledge_sources_tenant_id"):
        op.drop_index("ix_knowledge_sources_tenant_id", table_name="knowledge_sources")
    op.drop_table("knowledge_sources")
