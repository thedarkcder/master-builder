"""extend knowledge facts for source agnostic semantic layer

Revision ID: 20260313_0027
Revises: 20260313_0026
Create Date: 2026-03-13 16:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260313_0027"
down_revision = "20260313_0026"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(
        column.get("name") == column_name
        for column in inspector.get_columns(table_name)
    )


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    if not _has_column("knowledge_facts", "fact_type"):
        op.add_column(
            "knowledge_facts",
            sa.Column("fact_type", sa.String(length=64), nullable=True),
        )
    if not _has_column("knowledge_facts", "fact_key"):
        op.add_column(
            "knowledge_facts",
            sa.Column("fact_key", sa.String(length=128), nullable=True),
        )
    if not _has_column("knowledge_facts", "fact_value"):
        op.add_column(
            "knowledge_facts", sa.Column("fact_value", sa.Text(), nullable=True)
        )
    if not _has_column("knowledge_facts", "approval_state"):
        op.add_column(
            "knowledge_facts",
            sa.Column("approval_state", sa.String(length=32), nullable=True),
        )
    if not _has_column("knowledge_facts", "metadata_json"):
        op.add_column(
            "knowledge_facts", sa.Column("metadata_json", sa.JSON(), nullable=True)
        )
    if not _has_column("knowledge_facts", "superseded_at"):
        op.add_column(
            "knowledge_facts",
            sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        )

    op.execute(
        """
        UPDATE knowledge_facts
        SET fact_type = COALESCE(NULLIF(fact_type, ''), 'decision_slot'),
            fact_key = COALESCE(NULLIF(fact_key, ''), slot_name),
            fact_value = COALESCE(NULLIF(fact_value, ''), slot_value),
            approval_state = COALESCE(NULLIF(approval_state, ''), 'approved')
        """
    )

    if not is_sqlite:
        op.alter_column(
            "knowledge_facts",
            "fact_type",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        op.alter_column(
            "knowledge_facts",
            "fact_key",
            existing_type=sa.String(length=128),
            nullable=False,
        )
        op.alter_column(
            "knowledge_facts", "fact_value", existing_type=sa.Text(), nullable=False
        )
        op.alter_column(
            "knowledge_facts",
            "approval_state",
            existing_type=sa.String(length=32),
            nullable=False,
        )

    if not _has_index("knowledge_facts", "ix_knowledge_facts_fact_type"):
        op.create_index(
            "ix_knowledge_facts_fact_type",
            "knowledge_facts",
            ["fact_type"],
            unique=False,
        )
    if not _has_index("knowledge_facts", "ix_knowledge_facts_fact_key"):
        op.create_index(
            "ix_knowledge_facts_fact_key", "knowledge_facts", ["fact_key"], unique=False
        )
    if not _has_index("knowledge_facts", "ix_knowledge_facts_approval_state"):
        op.create_index(
            "ix_knowledge_facts_approval_state",
            "knowledge_facts",
            ["approval_state"],
            unique=False,
        )
    if not _has_index("knowledge_facts", "ix_knowledge_facts_superseded_at"):
        op.create_index(
            "ix_knowledge_facts_superseded_at",
            "knowledge_facts",
            ["superseded_at"],
            unique=False,
        )


def downgrade() -> None:
    if _has_index("knowledge_facts", "ix_knowledge_facts_superseded_at"):
        op.drop_index("ix_knowledge_facts_superseded_at", table_name="knowledge_facts")
    if _has_index("knowledge_facts", "ix_knowledge_facts_approval_state"):
        op.drop_index("ix_knowledge_facts_approval_state", table_name="knowledge_facts")
    if _has_index("knowledge_facts", "ix_knowledge_facts_fact_key"):
        op.drop_index("ix_knowledge_facts_fact_key", table_name="knowledge_facts")
    if _has_index("knowledge_facts", "ix_knowledge_facts_fact_type"):
        op.drop_index("ix_knowledge_facts_fact_type", table_name="knowledge_facts")

    if _has_column("knowledge_facts", "superseded_at"):
        op.drop_column("knowledge_facts", "superseded_at")
    if _has_column("knowledge_facts", "metadata_json"):
        op.drop_column("knowledge_facts", "metadata_json")
    if _has_column("knowledge_facts", "approval_state"):
        op.drop_column("knowledge_facts", "approval_state")
    if _has_column("knowledge_facts", "fact_value"):
        op.drop_column("knowledge_facts", "fact_value")
    if _has_column("knowledge_facts", "fact_key"):
        op.drop_column("knowledge_facts", "fact_key")
    if _has_column("knowledge_facts", "fact_type"):
        op.drop_column("knowledge_facts", "fact_type")
