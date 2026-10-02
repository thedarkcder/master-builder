"""remove folder-level deployment setup apps

Revision ID: 20260509_0119
Revises: 20260508_0118
Create Date: 2026-05-09 15:35:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260509_0119"
down_revision = "20260508_0118"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column["name"] == column_name for column in inspector.get_columns(table_name)
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not (
        _table_exists("project_apps")
        and _column_exists("project_apps", "analysis_source")
        and _column_exists("project_apps", "source_path")
    ):
        return

    stale_app_ids = [
        row["app_id"]
        for row in bind.execute(
            sa.text(
                """
                SELECT app_id
                FROM project_apps
                WHERE analysis_source = 'deployment_setup'
                  AND source_path <> '.'
                """
            )
        ).mappings()
    ]
    if not stale_app_ids:
        return

    if _table_exists("project_deployment_releases") and _column_exists(
        "project_deployment_releases", "app_id"
    ):
        releases = sa.table(
            "project_deployment_releases",
            sa.column("app_id", sa.String()),
        )
        bind.execute(
            releases.update()
            .where(releases.c.app_id.in_(stale_app_ids))
            .values(app_id=None)
        )
    if _table_exists("deployment_host_commands") and _column_exists(
        "deployment_host_commands", "app_id"
    ):
        commands = sa.table(
            "deployment_host_commands",
            sa.column("app_id", sa.String()),
        )
        bind.execute(
            commands.update()
            .where(commands.c.app_id.in_(stale_app_ids))
            .values(app_id=None)
        )
    if _table_exists("project_deployment_restore_runs") and _column_exists(
        "project_deployment_restore_runs", "app_id"
    ):
        restore_runs = sa.table(
            "project_deployment_restore_runs",
            sa.column("app_id", sa.String()),
        )
        bind.execute(
            restore_runs.delete().where(restore_runs.c.app_id.in_(stale_app_ids))
        )
    apps = sa.table(
        "project_apps",
        sa.column("app_id", sa.String()),
    )
    bind.execute(apps.delete().where(apps.c.app_id.in_(stale_app_ids)))


def downgrade() -> None:
    return None
