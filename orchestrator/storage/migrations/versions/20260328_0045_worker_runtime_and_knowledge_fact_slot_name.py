"""add worker runtime states and widen knowledge fact slot names

Revision ID: 20260328_0045
Revises: 20260328_0044
Create Date: 2026-03-28 16:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, ProgrammingError


revision = "20260328_0045"
down_revision = "20260328_0044"
branch_labels = None
depends_on = None

_WORKER_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_worker_runtime_states_agent_id", ["agent_id"]),
    ("ix_worker_runtime_states_worker_mode", ["worker_mode"]),
    ("ix_worker_runtime_states_state", ["state"]),
    ("ix_worker_runtime_states_last_heartbeat_at", ["last_heartbeat_at"]),
    ("ix_worker_runtime_states_updated_at", ["updated_at"]),
)


def _table_exists(bind, table_name: str) -> bool:
    if bind.dialect.name == "sqlite":
        inspector = sa.inspect(bind)
        return table_name in inspector.get_table_names()
    # Postgres: pg_catalog is authoritative (avoids missing public tables vs. inspector/search_path).
    return bool(
        bind.execute(
            text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public'
                      AND c.relname = :name
                      AND c.relkind IN ('r', 'p')
                )
                """
            ),
            {"name": table_name},
        ).scalar()
    )


def _index_exists(bind, table_name: str, index_name: str) -> bool:
    if bind.dialect.name == "sqlite":
        inspector = sa.inspect(bind)
        return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))
    return bool(
        bind.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = :t AND indexname = :i)"
            ),
            {"t": table_name, "i": index_name},
        ).scalar()
    )


def _column_length(table_name: str, column_name: str) -> int | None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for column in inspector.get_columns(table_name):
        if column.get("name") != column_name:
            continue
        column_type = column.get("type")
        return getattr(column_type, "length", None)
    return None


def _is_duplicate_table_error(exc: Exception) -> bool:
    orig = getattr(exc, "orig", None)
    sqlstate = str(getattr(orig, "pgcode", "") or getattr(orig, "sqlstate", "") or "").strip()
    if sqlstate == "42P07":
        return True
    message = str(orig or exc)
    return "already exists" in message.lower()


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    if not _table_exists(bind, "worker_runtime_states"):
        try:
            op.create_table(
                "worker_runtime_states",
                sa.Column("service_instance_id", sa.String(length=128), nullable=False),
                sa.Column("agent_id", sa.String(length=128), nullable=True),
                sa.Column("worker_mode", sa.String(length=32), nullable=True),
                sa.Column("capabilities_json", sa.JSON(), nullable=False),
                sa.Column("state", sa.String(length=32), nullable=False, server_default="starting"),
                sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
                sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
                sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
                sa.PrimaryKeyConstraint("service_instance_id"),
            )
        except (IntegrityError, ProgrammingError) as exc:
            # Table/type may already exist (concurrent startup, partial run); confirm before ignoring.
            if not _is_duplicate_table_error(exc) and not _table_exists(bind, "worker_runtime_states"):
                raise

    if _table_exists(bind, "worker_runtime_states"):
        for index_name, columns in _WORKER_INDEXES:
            if not _index_exists(bind, "worker_runtime_states", index_name):
                op.create_index(index_name, "worker_runtime_states", columns, unique=False)

    current_slot_name_length = _column_length("knowledge_facts", "slot_name")
    if current_slot_name_length is not None and current_slot_name_length < 128:
        with op.batch_alter_table("knowledge_facts", recreate="auto" if is_sqlite else "never") as batch_op:
            batch_op.alter_column(
                "slot_name",
                existing_type=sa.String(length=current_slot_name_length),
                type_=sa.String(length=128),
                existing_nullable=False,
            )


def downgrade() -> None:
    current_slot_name_length = _column_length("knowledge_facts", "slot_name")
    if current_slot_name_length is not None and current_slot_name_length > 64:
        with op.batch_alter_table("knowledge_facts", recreate="auto") as batch_op:
            batch_op.alter_column(
                "slot_name",
                existing_type=sa.String(length=current_slot_name_length),
                type_=sa.String(length=64),
                existing_nullable=False,
            )

    bind = op.get_bind()
    if _table_exists(bind, "worker_runtime_states"):
        op.drop_index("ix_worker_runtime_states_updated_at", table_name="worker_runtime_states")
        op.drop_index("ix_worker_runtime_states_last_heartbeat_at", table_name="worker_runtime_states")
        op.drop_index("ix_worker_runtime_states_state", table_name="worker_runtime_states")
        op.drop_index("ix_worker_runtime_states_worker_mode", table_name="worker_runtime_states")
        op.drop_index("ix_worker_runtime_states_agent_id", table_name="worker_runtime_states")
        op.drop_table("worker_runtime_states")
