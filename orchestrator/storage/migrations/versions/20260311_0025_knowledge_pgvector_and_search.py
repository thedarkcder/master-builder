"""migrate knowledge embeddings to pgvector and add search indexes

Revision ID: 20260311_0025
Revises: 20260311_0024
Create Date: 2026-03-11 02:00:00.000000
"""

from __future__ import annotations

import json

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "20260311_0025"
down_revision = "20260311_0024"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def _column_type(table_name: str, column_name: str) -> str | None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return None
    row = bind.execute(
        text(
            """
            SELECT format_type(attribute.atttypid, attribute.atttypmod)
            FROM pg_attribute AS attribute
            JOIN pg_class AS class ON class.oid = attribute.attrelid
            JOIN pg_namespace AS namespace ON namespace.oid = class.relnamespace
            WHERE class.relname = :table_name
              AND attribute.attname = :column_name
              AND namespace.nspname = current_schema()
              AND attribute.attnum > 0
              AND NOT attribute.attisdropped
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).fetchone()
    return str(row[0]).strip().lower() if row and row[0] else None


def _vector_literal(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            return None
        if candidate.startswith("[") and candidate.endswith("]"):
            return candidate
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        value = parsed
    if not isinstance(value, list):
        return None
    normalized = [float(item) for item in value]
    return "[" + ",".join(f"{item:.12g}" for item in normalized) + "]"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    if _column_type("knowledge_chunks", "embedding") != "vector(384)":
        if not _has_column("knowledge_chunks", "embedding_tmp"):
            op.execute("ALTER TABLE knowledge_chunks ADD COLUMN embedding_tmp vector(384)")

        rows = bind.execute(text("SELECT chunk_id, embedding FROM knowledge_chunks WHERE embedding IS NOT NULL")).mappings().all()
        for row in rows:
            literal = _vector_literal(row.get("embedding"))
            if not literal:
                continue
            bind.execute(
                text("UPDATE knowledge_chunks SET embedding_tmp = CAST(:embedding AS vector) WHERE chunk_id = :chunk_id"),
                {"embedding": literal, "chunk_id": row["chunk_id"]},
            )

        op.execute("ALTER TABLE knowledge_chunks DROP COLUMN embedding")
        op.execute("ALTER TABLE knowledge_chunks RENAME COLUMN embedding_tmp TO embedding")

    if not _has_index("knowledge_chunks", "ix_knowledge_chunks_embedding_ivfflat"):
        op.execute(
            """
            CREATE INDEX ix_knowledge_chunks_embedding_ivfflat
            ON knowledge_chunks
            USING ivfflat (embedding vector_cosine_ops)
            WITH (lists = 100)
            """
        )

    if not _has_index("knowledge_chunks", "ix_knowledge_chunks_content_fts"):
        op.execute(
            """
            CREATE INDEX ix_knowledge_chunks_content_fts
            ON knowledge_chunks
            USING gin (to_tsvector('simple', content))
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    if _has_index("knowledge_chunks", "ix_knowledge_chunks_content_fts"):
        op.drop_index("ix_knowledge_chunks_content_fts", table_name="knowledge_chunks")

    if _has_index("knowledge_chunks", "ix_knowledge_chunks_embedding_ivfflat"):
        op.drop_index("ix_knowledge_chunks_embedding_ivfflat", table_name="knowledge_chunks")

    if _column_type("knowledge_chunks", "embedding") == "vector(384)":
        if not _has_column("knowledge_chunks", "embedding_json"):
            op.add_column("knowledge_chunks", sa.Column("embedding_json", sa.JSON(), nullable=True))
        rows = bind.execute(text("SELECT chunk_id, embedding::text AS embedding_text FROM knowledge_chunks WHERE embedding IS NOT NULL")).mappings().all()
        for row in rows:
            literal = _vector_literal(row.get("embedding_text"))
            if not literal:
                continue
            parsed = json.loads(literal)
            bind.execute(
                text("UPDATE knowledge_chunks SET embedding_json = CAST(:embedding AS jsonb) WHERE chunk_id = :chunk_id"),
                {"embedding": json.dumps(parsed), "chunk_id": row["chunk_id"]},
            )
        op.execute("ALTER TABLE knowledge_chunks DROP COLUMN embedding")
        op.execute("ALTER TABLE knowledge_chunks RENAME COLUMN embedding_json TO embedding")
