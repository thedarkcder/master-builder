"""provider-backed secret crypto and plain human input answers

Revision ID: 20260414_0065
Revises: 20260413_0064
Create Date: 2026-04-14 12:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

from orchestrator.core.config import get_settings
from orchestrator.core.secrets import decrypt_value


revision = "20260414_0065"
down_revision = "20260413_0064"
branch_labels = None
depends_on = None


def _column_exists(bind, table_name: str, column_name: str) -> bool:
    inspector = inspect(bind)
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))

def upgrade() -> None:
    bind = op.get_bind()
    if not _column_exists(bind, "run_human_input_requests", "answer_text"):
        op.add_column("run_human_input_requests", sa.Column("answer_text", sa.Text(), nullable=True))

    settings = get_settings()
    legacy_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()

    answer_rows = bind.execute(
        text(
            """
            SELECT request_id, answer_encrypted
            FROM run_human_input_requests
            WHERE answer_encrypted IS NOT NULL
              AND TRIM(answer_encrypted) <> ''
            """
        )
    ).mappings().all()
    if answer_rows and not legacy_key:
        raise ValueError("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY is required to migrate existing human input answers")
    for row in answer_rows:
        plaintext = decrypt_value(ciphertext=str(row["answer_encrypted"]), encryption_key=legacy_key)
        bind.execute(
            text(
                """
                UPDATE run_human_input_requests
                SET answer_text = :answer_text,
                    answer_encrypted = NULL
                WHERE request_id = :request_id
                """
            ),
            {
                "request_id": row["request_id"],
                "answer_text": plaintext,
            },
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _column_exists(bind, "run_human_input_requests", "answer_text"):
        op.drop_column("run_human_input_requests", "answer_text")
