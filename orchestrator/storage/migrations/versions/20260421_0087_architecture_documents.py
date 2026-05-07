"""add architecture documents

Revision ID: 20260421_0087
Revises: 20260421_0086
Create Date: 2026-04-21 17:45:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "20260421_0087"
down_revision = "20260421_0086"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_column("projects", "architecture_docs_config"):
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("projects") as batch_op:
                batch_op.add_column(
                    sa.Column("architecture_docs_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'"))
                )
        else:
            op.add_column(
                "projects",
                sa.Column("architecture_docs_config", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            )

    if not _has_table("architecture_documents"):
        op.create_table(
            "architecture_documents",
            sa.Column("document_id", sa.String(), nullable=False),
            sa.Column("tenant_id", sa.String(), nullable=False),
            sa.Column("project_id", sa.String(), nullable=False),
            sa.Column("parent_issue_key", sa.String(), nullable=False),
            sa.Column("provider", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("canonical_url", sa.String(), nullable=False),
            sa.Column("provider_ref", sa.String(), nullable=True),
            sa.Column("knowledge_asset_id", sa.String(), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_by", sa.String(), nullable=True),
            sa.Column("updated_by", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["knowledge_asset_id"], ["knowledge_assets.asset_id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("document_id"),
        )

    if not _has_index("architecture_documents", "ix_architecture_documents_scope"):
        op.create_index(
            "ix_architecture_documents_scope",
            "architecture_documents",
            ["tenant_id", "project_id", "parent_issue_key"],
            unique=False,
        )
    if not _has_index("architecture_documents", "ix_architecture_documents_project_status"):
        op.create_index(
            "ix_architecture_documents_project_status",
            "architecture_documents",
            ["project_id", "status", "updated_at"],
            unique=False,
        )
    if not _has_index("architecture_documents", "ux_architecture_documents_active_parent"):
        op.create_index(
            "ux_architecture_documents_active_parent",
            "architecture_documents",
            ["tenant_id", "project_id", "parent_issue_key"],
            unique=True,
            postgresql_where=sa.text("is_active"),
            sqlite_where=sa.text("is_active = 1"),
        )


def downgrade() -> None:
    if _has_table("architecture_documents"):
        if _has_index("architecture_documents", "ux_architecture_documents_active_parent"):
            op.drop_index("ux_architecture_documents_active_parent", table_name="architecture_documents")
        if _has_index("architecture_documents", "ix_architecture_documents_project_status"):
            op.drop_index("ix_architecture_documents_project_status", table_name="architecture_documents")
        if _has_index("architecture_documents", "ix_architecture_documents_scope"):
            op.drop_index("ix_architecture_documents_scope", table_name="architecture_documents")
        op.drop_table("architecture_documents")
    bind = op.get_bind()
    if _has_column("projects", "architecture_docs_config"):
        if bind.dialect.name == "sqlite":
            with op.batch_alter_table("projects") as batch_op:
                batch_op.drop_column("architecture_docs_config")
        else:
            op.drop_column("projects", "architecture_docs_config")
