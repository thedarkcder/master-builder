"""add project installs and install requests

Revision ID: 20260409_0055
Revises: 20260407_0054
Create Date: 2026-04-09 12:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision = "20260409_0055"
down_revision = "20260407_0054"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def upgrade() -> None:
    if not _table_exists("project_installs"):
        op.create_table(
            "project_installs",
            sa.Column("install_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=False),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column(
                "enabled", sa.Boolean(), nullable=False, server_default=sa.true()
            ),
            sa.Column("config_json", sa.JSON(), nullable=False),
            sa.Column("binding_names_json", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("install_id"),
        )
    if not _index_exists(
        "project_installs", "ix_project_installs_tenant_project_enabled"
    ):
        op.create_index(
            "ix_project_installs_tenant_project_enabled",
            "project_installs",
            ["tenant_id", "project_id", "enabled"],
            unique=False,
        )
    if not _index_exists("project_installs", "ix_project_installs_tenant_project_kind"):
        op.create_index(
            "ix_project_installs_tenant_project_kind",
            "project_installs",
            ["tenant_id", "project_id", "kind"],
            unique=False,
        )
    if not _index_exists("project_installs", op.f("ix_project_installs_kind")):
        op.create_index(
            op.f("ix_project_installs_kind"), "project_installs", ["kind"], unique=False
        )
    if not _index_exists("project_installs", op.f("ix_project_installs_project_id")):
        op.create_index(
            op.f("ix_project_installs_project_id"),
            "project_installs",
            ["project_id"],
            unique=False,
        )
    if not _index_exists("project_installs", op.f("ix_project_installs_tenant_id")):
        op.create_index(
            op.f("ix_project_installs_tenant_id"),
            "project_installs",
            ["tenant_id"],
            unique=False,
        )

    if not _table_exists("project_install_requests"):
        op.create_table(
            "project_install_requests",
            sa.Column("request_id", sa.String(length=64), nullable=False),
            sa.Column("tenant_id", sa.String(length=128), nullable=False),
            sa.Column("project_id", sa.String(length=128), nullable=False),
            sa.Column("workflow_id", sa.String(length=64), nullable=True),
            sa.Column("run_id", sa.String(length=64), nullable=True),
            sa.Column("issue_key", sa.String(length=64), nullable=False),
            sa.Column("kind", sa.String(length=64), nullable=False),
            sa.Column("label", sa.String(length=255), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("suggested_config_json", sa.JSON(), nullable=False),
            sa.Column("required_bindings_json", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("request_kind", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["project_id"], ["projects.project_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(["run_id"], ["runs.run_id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(
                ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
            ),
            sa.ForeignKeyConstraint(
                ["workflow_id"],
                ["workflow_executions.workflow_id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("request_id"),
        )
    if not _index_exists(
        "project_install_requests", "ix_project_install_requests_scope_status"
    ):
        op.create_index(
            "ix_project_install_requests_scope_status",
            "project_install_requests",
            ["tenant_id", "project_id", "status"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", "ix_project_install_requests_scope_kind_status"
    ):
        op.create_index(
            "ix_project_install_requests_scope_kind_status",
            "project_install_requests",
            ["tenant_id", "project_id", "kind", "status"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", "ix_project_install_requests_run_status"
    ):
        op.create_index(
            "ix_project_install_requests_run_status",
            "project_install_requests",
            ["run_id", "status"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_created_at")
    ):
        op.create_index(
            op.f("ix_project_install_requests_created_at"),
            "project_install_requests",
            ["created_at"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_issue_key")
    ):
        op.create_index(
            op.f("ix_project_install_requests_issue_key"),
            "project_install_requests",
            ["issue_key"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_kind")
    ):
        op.create_index(
            op.f("ix_project_install_requests_kind"),
            "project_install_requests",
            ["kind"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_project_id")
    ):
        op.create_index(
            op.f("ix_project_install_requests_project_id"),
            "project_install_requests",
            ["project_id"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_request_kind")
    ):
        op.create_index(
            op.f("ix_project_install_requests_request_kind"),
            "project_install_requests",
            ["request_kind"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_run_id")
    ):
        op.create_index(
            op.f("ix_project_install_requests_run_id"),
            "project_install_requests",
            ["run_id"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_status")
    ):
        op.create_index(
            op.f("ix_project_install_requests_status"),
            "project_install_requests",
            ["status"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_tenant_id")
    ):
        op.create_index(
            op.f("ix_project_install_requests_tenant_id"),
            "project_install_requests",
            ["tenant_id"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_updated_at")
    ):
        op.create_index(
            op.f("ix_project_install_requests_updated_at"),
            "project_install_requests",
            ["updated_at"],
            unique=False,
        )
    if not _index_exists(
        "project_install_requests", op.f("ix_project_install_requests_workflow_id")
    ):
        op.create_index(
            op.f("ix_project_install_requests_workflow_id"),
            "project_install_requests",
            ["workflow_id"],
            unique=False,
        )


def downgrade() -> None:
    op.drop_index(
        op.f("ix_project_install_requests_workflow_id"),
        table_name="project_install_requests",
    )
    op.drop_index(
        op.f("ix_project_install_requests_updated_at"),
        table_name="project_install_requests",
    )
    op.drop_index(
        op.f("ix_project_install_requests_tenant_id"),
        table_name="project_install_requests",
    )
    op.drop_index(
        op.f("ix_project_install_requests_status"),
        table_name="project_install_requests",
    )
    op.drop_index(
        op.f("ix_project_install_requests_run_id"),
        table_name="project_install_requests",
    )
    op.drop_index(
        op.f("ix_project_install_requests_request_kind"),
        table_name="project_install_requests",
    )
    op.drop_index(
        op.f("ix_project_install_requests_project_id"),
        table_name="project_install_requests",
    )
    op.drop_index(
        op.f("ix_project_install_requests_kind"), table_name="project_install_requests"
    )
    op.drop_index(
        op.f("ix_project_install_requests_issue_key"),
        table_name="project_install_requests",
    )
    op.drop_index(
        op.f("ix_project_install_requests_created_at"),
        table_name="project_install_requests",
    )
    op.drop_index(
        "ix_project_install_requests_run_status", table_name="project_install_requests"
    )
    op.drop_index(
        "ix_project_install_requests_scope_kind_status",
        table_name="project_install_requests",
    )
    op.drop_index(
        "ix_project_install_requests_scope_status",
        table_name="project_install_requests",
    )
    op.drop_table("project_install_requests")

    op.drop_index(op.f("ix_project_installs_tenant_id"), table_name="project_installs")
    op.drop_index(op.f("ix_project_installs_project_id"), table_name="project_installs")
    op.drop_index(op.f("ix_project_installs_kind"), table_name="project_installs")
    op.drop_index(
        "ix_project_installs_tenant_project_kind", table_name="project_installs"
    )
    op.drop_index(
        "ix_project_installs_tenant_project_enabled", table_name="project_installs"
    )
    op.drop_table("project_installs")
