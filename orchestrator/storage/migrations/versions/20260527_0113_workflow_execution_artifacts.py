"""Add durable workflow execution artifacts.

Revision ID: 20260527_0113
Revises: 20260526_0112
Create Date: 2026-05-27 11:25:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260527_0113"
down_revision = "20260526_0112"
branch_labels = None
depends_on = None


def _table_exists(bind: sa.engine.Connection, table_name: str) -> bool:
    return sa.inspect(bind).has_table(table_name)


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "workflow_execution_artifacts"):
        op.create_table(
            "workflow_execution_artifacts",
            sa.Column("artifact_id", sa.String(length=64), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(length=128),
                sa.ForeignKey("tenants.tenant_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "project_id",
                sa.String(length=128),
                sa.ForeignKey("projects.project_id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "workflow_id",
                sa.String(length=64),
                sa.ForeignKey("workflow_executions.workflow_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "run_id",
                sa.String(length=64),
                sa.ForeignKey("runs.run_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("artifact_kind", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("repo_url", sa.String(length=512), nullable=False),
            sa.Column("branch", sa.String(length=255), nullable=False),
            sa.Column("commit_sha", sa.String(length=64), nullable=False),
            sa.Column("diff_stat_json", sa.JSON(), nullable=False),
            sa.Column("pushed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("run_id", "artifact_kind", name="uq_workflow_execution_artifacts_run_kind"),
        )
        op.create_index(
            "ix_workflow_execution_artifacts_tenant_project",
            "workflow_execution_artifacts",
            ["tenant_id", "project_id"],
        )
        op.create_index("ix_workflow_execution_artifacts_workflow_id", "workflow_execution_artifacts", ["workflow_id"])
        op.create_index("ix_workflow_execution_artifacts_run_id", "workflow_execution_artifacts", ["run_id"])
        op.create_index("ix_workflow_execution_artifacts_status", "workflow_execution_artifacts", ["status"])

    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE workflow_execution_artifacts ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE workflow_execution_artifacts FORCE ROW LEVEL SECURITY")
        op.execute("DROP POLICY IF EXISTS workflow_execution_artifacts_tenant_isolation ON workflow_execution_artifacts")
        op.execute(
            """
            CREATE POLICY workflow_execution_artifacts_tenant_isolation
            ON workflow_execution_artifacts
            USING (
                current_setting('app.principal_type', true) IN ('platform_admin', 'platform_system')
                OR (
                    current_setting('app.principal_type', true) = 'tenant_system'
                    AND NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL
                    AND tenant_id = current_setting('app.tenant_id', true)
                )
                OR (
                    current_setting('app.principal_type', true) = 'tenant_user'
                    AND NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
                    AND tenant_id IN (
                        SELECT tm.tenant_id
                        FROM tenant_memberships tm
                        WHERE tm.user_id = current_setting('app.user_id', true)
                    )
                    AND (
                        NULLIF(current_setting('app.tenant_id', true), '') IS NULL
                        OR tenant_id = current_setting('app.tenant_id', true)
                    )
                )
            )
            WITH CHECK (
                current_setting('app.principal_type', true) IN ('platform_admin', 'platform_system')
                OR (
                    current_setting('app.principal_type', true) = 'tenant_system'
                    AND NULLIF(current_setting('app.tenant_id', true), '') IS NOT NULL
                    AND tenant_id = current_setting('app.tenant_id', true)
                )
                OR (
                    current_setting('app.principal_type', true) = 'tenant_user'
                    AND NULLIF(current_setting('app.user_id', true), '') IS NOT NULL
                    AND tenant_id IN (
                        SELECT tm.tenant_id
                        FROM tenant_memberships tm
                        WHERE tm.user_id = current_setting('app.user_id', true)
                    )
                    AND (
                        NULLIF(current_setting('app.tenant_id', true), '') IS NULL
                        OR tenant_id = current_setting('app.tenant_id', true)
                    )
                )
            )
            """
        )


def downgrade() -> None:
    raise RuntimeError("Workflow execution artifact migration cannot be downgraded")
