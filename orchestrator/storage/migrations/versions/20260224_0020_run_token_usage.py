"""add normalized token usage rows for runs

Revision ID: 20260224_0020
Revises: 20260218_0019
Create Date: 2026-02-24 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260224_0020"
down_revision = "20260218_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_token_usage",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=True),
        sa.Column("stage", sa.String(length=64), nullable=False),
        sa.Column("invocation_id", sa.String(length=64), nullable=True),
        sa.Column("turn_id", sa.String(length=128), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cached_input_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("delta_input", sa.Integer(), nullable=True),
        sa.Column("delta_uncached", sa.Integer(), nullable=True),
        sa.Column("delta_output", sa.Integer(), nullable=True),
        sa.Column("runtime_ms", sa.Integer(), nullable=True),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("command", sa.String(length=256), nullable=True),
        sa.Column("artifact_path", sa.String(length=1024), nullable=True),
        sa.Column("output_chars", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.project_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "run_id",
            "invocation_id",
            "turn_id",
            "recorded_at",
            name="uq_run_token_usage_run_invocation_turn_recorded_at",
        ),
    )
    op.create_index("ix_run_token_usage_tenant_id", "run_token_usage", ["tenant_id"])
    op.create_index("ix_run_token_usage_run_id", "run_token_usage", ["run_id"])
    op.create_index("ix_run_token_usage_project_id", "run_token_usage", ["project_id"])
    op.create_index("ix_run_token_usage_stage", "run_token_usage", ["stage"])
    op.create_index(
        "ix_run_token_usage_invocation_id", "run_token_usage", ["invocation_id"]
    )
    op.create_index("ix_run_token_usage_turn_id", "run_token_usage", ["turn_id"])
    op.create_index(
        "ix_run_token_usage_recorded_at", "run_token_usage", ["recorded_at"]
    )
    op.create_index(
        "ix_run_token_usage_stage_attempt_recorded_at",
        "run_token_usage",
        ["stage", "attempt", "recorded_at"],
    )
    op.create_index(
        "ix_run_token_usage_tenant_recorded_at",
        "run_token_usage",
        ["tenant_id", "recorded_at"],
    )
    op.create_index(
        "ix_run_token_usage_run_recorded_at",
        "run_token_usage",
        ["run_id", "recorded_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_run_token_usage_run_recorded_at", table_name="run_token_usage")
    op.drop_index("ix_run_token_usage_tenant_recorded_at", table_name="run_token_usage")
    op.drop_index(
        "ix_run_token_usage_stage_attempt_recorded_at", table_name="run_token_usage"
    )
    op.drop_index("ix_run_token_usage_recorded_at", table_name="run_token_usage")
    op.drop_index("ix_run_token_usage_turn_id", table_name="run_token_usage")
    op.drop_index("ix_run_token_usage_invocation_id", table_name="run_token_usage")
    op.drop_index("ix_run_token_usage_stage", table_name="run_token_usage")
    op.drop_index("ix_run_token_usage_project_id", table_name="run_token_usage")
    op.drop_index("ix_run_token_usage_run_id", table_name="run_token_usage")
    op.drop_index("ix_run_token_usage_tenant_id", table_name="run_token_usage")
    op.drop_table("run_token_usage")
