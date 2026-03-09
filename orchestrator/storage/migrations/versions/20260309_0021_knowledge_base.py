"""add project knowledge base storage

Revision ID: 20260309_0021
Revises: 20260224_0020
Create Date: 2026-03-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260309_0021"
down_revision = "20260224_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "DO $$ BEGIN CREATE EXTENSION IF NOT EXISTS vector; EXCEPTION WHEN undefined_file THEN NULL; END $$;"
        )

    op.create_table(
        "knowledge_assets",
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=128), nullable=True),
        sa.Column("source_ref", sa.String(length=1024), nullable=True),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("checksum", sa.String(length=64), nullable=True),
        sa.Column("text_content", sa.Text(), nullable=True),
        sa.Column("binary_content", sa.LargeBinary(), nullable=True),
        sa.Column("chunk_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'ready'")),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("asset_id"),
    )
    op.create_index("ix_knowledge_assets_tenant_id", "knowledge_assets", ["tenant_id"], unique=False)
    op.create_index("ix_knowledge_assets_project_id", "knowledge_assets", ["project_id"], unique=False)
    op.create_index("ix_knowledge_assets_source_type", "knowledge_assets", ["source_type"], unique=False)
    op.create_index("ix_knowledge_assets_source_timestamp", "knowledge_assets", ["source_timestamp"], unique=False)
    op.create_index("ix_knowledge_assets_checksum", "knowledge_assets", ["checksum"], unique=False)
    op.create_index("ix_knowledge_assets_status", "knowledge_assets", ["status"], unique=False)
    op.create_index("ix_knowledge_assets_updated_at", "knowledge_assets", ["updated_at"], unique=False)

    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("embedding", sa.JSON(), nullable=True),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["knowledge_assets.asset_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chunk_id"),
        sa.UniqueConstraint("asset_id", "chunk_index", name="uq_knowledge_chunks_asset_chunk_index"),
    )
    op.create_index("ix_knowledge_chunks_asset_id", "knowledge_chunks", ["asset_id"], unique=False)
    op.create_index("ix_knowledge_chunks_tenant_id", "knowledge_chunks", ["tenant_id"], unique=False)
    op.create_index("ix_knowledge_chunks_project_id", "knowledge_chunks", ["project_id"], unique=False)
    op.create_index("ix_knowledge_chunks_source_timestamp", "knowledge_chunks", ["source_timestamp"], unique=False)

    op.create_table(
        "knowledge_facts",
        sa.Column("fact_id", sa.String(length=64), nullable=False),
        sa.Column("asset_id", sa.String(length=64), nullable=False),
        sa.Column("chunk_id", sa.String(length=64), nullable=True),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("slot_name", sa.String(length=64), nullable=False),
        sa.Column("slot_value", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False, server_default=sa.text("1.0")),
        sa.Column("is_inferred", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["knowledge_assets.asset_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["chunk_id"], ["knowledge_chunks.chunk_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.project_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("fact_id"),
    )
    op.create_index("ix_knowledge_facts_asset_id", "knowledge_facts", ["asset_id"], unique=False)
    op.create_index("ix_knowledge_facts_chunk_id", "knowledge_facts", ["chunk_id"], unique=False)
    op.create_index("ix_knowledge_facts_tenant_id", "knowledge_facts", ["tenant_id"], unique=False)
    op.create_index("ix_knowledge_facts_project_id", "knowledge_facts", ["project_id"], unique=False)
    op.create_index("ix_knowledge_facts_slot_name", "knowledge_facts", ["slot_name"], unique=False)
    op.create_index("ix_knowledge_facts_source_timestamp", "knowledge_facts", ["source_timestamp"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_knowledge_facts_source_timestamp", table_name="knowledge_facts")
    op.drop_index("ix_knowledge_facts_slot_name", table_name="knowledge_facts")
    op.drop_index("ix_knowledge_facts_project_id", table_name="knowledge_facts")
    op.drop_index("ix_knowledge_facts_tenant_id", table_name="knowledge_facts")
    op.drop_index("ix_knowledge_facts_chunk_id", table_name="knowledge_facts")
    op.drop_index("ix_knowledge_facts_asset_id", table_name="knowledge_facts")
    op.drop_table("knowledge_facts")

    op.drop_index("ix_knowledge_chunks_source_timestamp", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_project_id", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_tenant_id", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_asset_id", table_name="knowledge_chunks")
    op.drop_table("knowledge_chunks")

    op.drop_index("ix_knowledge_assets_updated_at", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_status", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_checksum", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_source_timestamp", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_source_type", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_project_id", table_name="knowledge_assets")
    op.drop_index("ix_knowledge_assets_tenant_id", table_name="knowledge_assets")
    op.drop_table("knowledge_assets")
