"""split run dedupe scope for issue execution vs pr remediation

Revision ID: 20260320_0031
Revises: 20260320_0030
Create Date: 2026-03-20 17:10:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "20260320_0031"
down_revision = "20260320_0030"
branch_labels = None
depends_on = None

_ISSUE_EXECUTION_SCOPE = "issue_execution"
_PR_REMEDIATION_SCOPE = "pr_remediation"
_RUN_DEDUPE_SCOPE_LOCK_KEY = 202603200031


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(column.get("name") == column_name for column in inspector.get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    return any(index.get("name") == index_name for index in inspector.get_indexes(table_name))


def _drop_index_if_exists(index_name: str) -> None:
    bind = op.get_bind()
    bind.execute(text(f"DROP INDEX IF EXISTS {index_name}"))


def _classify_existing_runs() -> None:
    bind = op.get_bind()
    bind.execute(
        text(
            """
            UPDATE runs
            SET dedupe_scope = :pr_scope
            WHERE issue_summary LIKE :summary_pattern
               OR issue_description LIKE :description_pattern
            """
        ),
        {
            "pr_scope": _PR_REMEDIATION_SCOPE,
            "summary_pattern": "%: PR remediation for #%",
            "description_pattern": "Automated remediation run triggered from GitHub PR #%",
        },
    )


def _rebuild_run_locks() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())
    if "run_locks" not in table_names:
        return
    _drop_index_if_exists("ix_run_locks_v2_run_id")
    if "run_locks_v2" in table_names:
        op.drop_table("run_locks_v2")

    op.create_table(
        "run_locks_v2",
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("issue_key", sa.String(length=64), nullable=False),
        sa.Column("dedupe_scope", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "issue_key", "dedupe_scope"),
    )
    op.create_index("ix_run_locks_v2_run_id", "run_locks_v2", ["run_id"], unique=True)

    bind.execute(
        text(
            """
            INSERT INTO run_locks_v2 (tenant_id, issue_key, dedupe_scope, run_id, locked_at)
            SELECT rl.tenant_id,
                   rl.issue_key,
                   COALESCE(r.dedupe_scope, :default_scope),
                   rl.run_id,
                   rl.locked_at
            FROM run_locks rl
            LEFT JOIN runs r ON r.run_id = rl.run_id
            """
        ),
        {"default_scope": _ISSUE_EXECUTION_SCOPE},
    )

    if _has_index("run_locks", "ix_run_locks_run_id"):
        op.drop_index("ix_run_locks_run_id", table_name="run_locks")
    op.drop_table("run_locks")
    op.rename_table("run_locks_v2", "run_locks")
    if not _has_index("run_locks", "ix_run_locks_run_id"):
        op.create_index("ix_run_locks_run_id", "run_locks", ["run_id"], unique=True)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        bind.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _RUN_DEDUPE_SCOPE_LOCK_KEY},
        )

    if not _has_column("runs", "dedupe_scope"):
        op.add_column(
            "runs",
            sa.Column(
                "dedupe_scope",
                sa.String(length=32),
                nullable=False,
                server_default=_ISSUE_EXECUTION_SCOPE,
            ),
        )
    _classify_existing_runs()
    if not _has_index("runs", "ix_runs_dedupe_scope"):
        op.create_index("ix_runs_dedupe_scope", "runs", ["dedupe_scope"], unique=False)

    _rebuild_run_locks()


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "run_locks" in table_names:
        _drop_index_if_exists("ix_run_locks_legacy_run_id")
        if "run_locks_legacy" in table_names:
            op.drop_table("run_locks_legacy")
        op.create_table(
            "run_locks_legacy",
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("run_id", sa.String(length=64), nullable=False),
            sa.Column("locked_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("tenant_id", "issue_key"),
        )
        op.create_index("ix_run_locks_legacy_run_id", "run_locks_legacy", ["run_id"], unique=True)
        bind.execute(
            text(
                """
                INSERT INTO run_locks_legacy (tenant_id, issue_key, run_id, locked_at)
                SELECT tenant_id, issue_key, run_id, locked_at
                FROM run_locks
                """
            )
        )
        if _has_index("run_locks", "ix_run_locks_run_id"):
            op.drop_index("ix_run_locks_run_id", table_name="run_locks")
        op.drop_table("run_locks")
        op.rename_table("run_locks_legacy", "run_locks")
        if not _has_index("run_locks", "ix_run_locks_run_id"):
            op.create_index("ix_run_locks_run_id", "run_locks", ["run_id"], unique=True)

    if _has_index("runs", "ix_runs_dedupe_scope"):
        op.drop_index("ix_runs_dedupe_scope", table_name="runs")
    if _has_column("runs", "dedupe_scope"):
        op.drop_column("runs", "dedupe_scope")
