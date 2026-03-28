"""add worker runtime states and widen knowledge fact slot names

Revision ID: 20260328_0045
Revises: 20260328_0044
Create Date: 2026-03-28 16:20:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260328_0045"
down_revision = "20260328_0044"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return table_name in inspector.get_table_names()


def _column_length(table_name: str, column_name: str) -> int | None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for column in inspector.get_columns(table_name):
        if column.get("name") != column_name:
            continue
        column_type = column.get("type")
        return getattr(column_type, "length", None)
    return None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    if not _has_table("worker_runtime_states"):
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
        op.create_index("ix_worker_runtime_states_agent_id", "worker_runtime_states", ["agent_id"], unique=False)
        op.create_index("ix_worker_runtime_states_worker_mode", "worker_runtime_states", ["worker_mode"], unique=False)
        op.create_index("ix_worker_runtime_states_state", "worker_runtime_states", ["state"], unique=False)
        op.create_index(
            "ix_worker_runtime_states_last_heartbeat_at",
            "worker_runtime_states",
            ["last_heartbeat_at"],
            unique=False,
        )
        op.create_index("ix_worker_runtime_states_updated_at", "worker_runtime_states", ["updated_at"], unique=False)

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

    if _has_table("worker_runtime_states"):
        op.drop_index("ix_worker_runtime_states_updated_at", table_name="worker_runtime_states")
        op.drop_index("ix_worker_runtime_states_last_heartbeat_at", table_name="worker_runtime_states")
        op.drop_index("ix_worker_runtime_states_state", table_name="worker_runtime_states")
        op.drop_index("ix_worker_runtime_states_worker_mode", table_name="worker_runtime_states")
        op.drop_index("ix_worker_runtime_states_agent_id", table_name="worker_runtime_states")
        op.drop_table("worker_runtime_states")
