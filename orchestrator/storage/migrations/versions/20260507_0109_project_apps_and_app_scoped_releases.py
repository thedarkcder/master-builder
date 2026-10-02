"""add project apps and app scoped deployment releases

Revision ID: 20260409_0057
Revises: 20260409_0056
Create Date: 2026-04-09 15:20:00.000000
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from alembic import op
import sqlalchemy as sa
from sqlalchemy import text


revision = "20260507_0109"
down_revision = "20260507_0108"
branch_labels = None
depends_on = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        column.get("name") == column_name
        for column in inspector.get_columns(table_name)
    )


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(
        index.get("name") == index_name for index in inspector.get_indexes(table_name)
    )


def _loads_json(value: object) -> dict:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        normalized = value.strip()
        if not normalized:
            return {}
        loaded = json.loads(normalized)
        if isinstance(loaded, dict):
            return dict(loaded)
    return {}


def _create_project_apps_table() -> None:
    if _table_exists("project_apps"):
        return
    op.create_table(
        "project_apps",
        sa.Column("app_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=255), nullable=False),
        sa.Column("source_path", sa.String(length=512), nullable=False),
        sa.Column("detection_confidence", sa.Float(), nullable=True),
        sa.Column("detected_runtime", sa.String(length=128), nullable=True),
        sa.Column("detected_language", sa.String(length=128), nullable=True),
        sa.Column("analysis_source", sa.String(length=128), nullable=True),
        sa.Column("build_strategy", sa.String(length=32), nullable=True),
        sa.Column("exposed_port", sa.Integer(), nullable=True),
        sa.Column("healthcheck", sa.Text(), nullable=True),
        sa.Column("start_command", sa.Text(), nullable=True),
        sa.Column(
            "env_schema_json", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "secret_schema_json",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "deployment_config",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'draft'"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.project_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("app_id"),
        sa.UniqueConstraint(
            "project_id", "source_path", name="uq_project_apps_project_source_path"
        ),
        sa.UniqueConstraint("project_id", "slug", name="uq_project_apps_project_slug"),
    )
    for index_name, columns in (
        ("ix_project_apps_tenant_id", ["tenant_id"]),
        ("ix_project_apps_project_id", ["project_id"]),
        ("ix_project_apps_status", ["status"]),
        (
            "ix_project_apps_tenant_project_created_at",
            ["tenant_id", "project_id", "created_at"],
        ),
        ("ix_project_apps_project_status", ["project_id", "status"]),
        ("ix_project_apps_tenant_status", ["tenant_id", "status"]),
    ):
        if not _index_exists("project_apps", index_name):
            op.create_index(index_name, "project_apps", columns, unique=False)


def _create_project_app_analysis_runs_table() -> None:
    if _table_exists("project_app_analysis_runs"):
        return
    op.create_table(
        "project_app_analysis_runs",
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'queued'"),
        ),
        sa.Column("planner_version", sa.String(length=64), nullable=True),
        sa.Column(
            "request_payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "result_payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.project_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    for index_name, columns in (
        ("ix_project_app_analysis_runs_tenant_id", ["tenant_id"]),
        ("ix_project_app_analysis_runs_project_id", ["project_id"]),
        ("ix_project_app_analysis_runs_status", ["status"]),
        (
            "ix_project_app_analysis_runs_tenant_project_created_at",
            ["tenant_id", "project_id", "created_at"],
        ),
        ("ix_project_app_analysis_runs_project_status", ["project_id", "status"]),
        ("ix_project_app_analysis_runs_tenant_status", ["tenant_id", "status"]),
    ):
        if not _index_exists("project_app_analysis_runs", index_name):
            op.create_index(
                index_name, "project_app_analysis_runs", columns, unique=False
            )


def _add_release_app_id_column() -> None:
    if not _table_exists("project_deployment_releases") or _column_exists(
        "project_deployment_releases", "app_id"
    ):
        return
    with op.batch_alter_table("project_deployment_releases", schema=None) as batch_op:
        batch_op.add_column(sa.Column("app_id", sa.String(length=128), nullable=True))
        batch_op.create_foreign_key(
            "fk_project_deployment_releases_app_id_project_apps",
            "project_apps",
            ["app_id"],
            ["app_id"],
            ondelete="SET NULL",
        )


def _add_release_app_index() -> None:
    if _table_exists("project_deployment_releases") and not _index_exists(
        "project_deployment_releases",
        "ix_project_deployment_releases_app_id",
    ):
        op.create_index(
            "ix_project_deployment_releases_app_id",
            "project_deployment_releases",
            ["app_id"],
            unique=False,
        )


def _backfill_default_apps_and_release_app_ids() -> None:
    bind = op.get_bind()
    project_rows = (
        bind.execute(
            text(
                """
            SELECT project_id, tenant_id, name, deployment_config, created_at, updated_at
            FROM projects
            """
            )
        )
        .mappings()
        .all()
    )
    existing_default_apps = {
        str(row.get("project_id") or "").strip(): str(row.get("app_id") or "").strip()
        for row in bind.execute(
            text(
                """
                SELECT app_id, project_id
                FROM project_apps
                WHERE source_path = '.'
                """
            )
        )
        .mappings()
        .all()
        if str(row.get("project_id") or "").strip()
        and str(row.get("app_id") or "").strip()
    }
    project_default_app_ids: dict[str, str] = dict(existing_default_apps)
    for row in project_rows:
        project_id = str(row.get("project_id") or "").strip()
        tenant_id = str(row.get("tenant_id") or "").strip()
        if not project_id or not tenant_id:
            continue
        if project_id in project_default_app_ids:
            continue
        now = _now()
        app_id = str(uuid4())
        bind.execute(
            text(
                """
                INSERT INTO project_apps (
                    app_id,
                    tenant_id,
                    project_id,
                    name,
                    slug,
                    source_path,
                    detection_confidence,
                    detected_runtime,
                    detected_language,
                    analysis_source,
                    build_strategy,
                    exposed_port,
                    healthcheck,
                    start_command,
                    env_schema_json,
                    secret_schema_json,
                    deployment_config,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (
                    :app_id,
                    :tenant_id,
                    :project_id,
                    :name,
                    'default',
                    '.',
                    NULL,
                    NULL,
                    NULL,
                    'compatibility_default',
                    NULL,
                    NULL,
                    NULL,
                    NULL,
                    :env_schema_json,
                    :secret_schema_json,
                    :deployment_config,
                    'draft',
                    :created_at,
                    :updated_at
                )
                """
            ),
            {
                "app_id": app_id,
                "tenant_id": tenant_id,
                "project_id": project_id,
                "name": str(row.get("name") or project_id).strip() or project_id,
                "env_schema_json": json.dumps({}, sort_keys=True),
                "secret_schema_json": json.dumps({}, sort_keys=True),
                "deployment_config": json.dumps(
                    _loads_json(row.get("deployment_config")), sort_keys=True
                ),
                "created_at": row.get("created_at") or now,
                "updated_at": row.get("updated_at") or now,
            },
        )
        project_default_app_ids[project_id] = app_id

    for row in (
        bind.execute(
            text(
                """
            SELECT release_id, project_id
            FROM project_deployment_releases
            WHERE app_id IS NULL
            """
            )
        )
        .mappings()
        .all()
    ):
        release_id = str(row.get("release_id") or "").strip()
        project_id = str(row.get("project_id") or "").strip()
        app_id = project_default_app_ids.get(project_id)
        if not release_id or not project_id or not app_id:
            continue
        bind.execute(
            text(
                """
                UPDATE project_deployment_releases
                SET app_id = :app_id
                WHERE release_id = :release_id AND app_id IS NULL
                """
            ),
            {"release_id": release_id, "app_id": app_id},
        )


def upgrade() -> None:
    _create_project_apps_table()
    _create_project_app_analysis_runs_table()
    _add_release_app_id_column()
    _add_release_app_index()
    _backfill_default_apps_and_release_app_ids()


def downgrade() -> None:
    if _table_exists("project_deployment_releases") and _index_exists(
        "project_deployment_releases", "ix_project_deployment_releases_app_id"
    ):
        op.drop_index(
            "ix_project_deployment_releases_app_id",
            table_name="project_deployment_releases",
        )
    if _table_exists("project_deployment_releases") and _column_exists(
        "project_deployment_releases", "app_id"
    ):
        with op.batch_alter_table(
            "project_deployment_releases", schema=None
        ) as batch_op:
            batch_op.drop_constraint(
                "fk_project_deployment_releases_app_id_project_apps", type_="foreignkey"
            )
            batch_op.drop_column("app_id")

    for table_name, index_names in (
        (
            "project_app_analysis_runs",
            [
                "ix_project_app_analysis_runs_tenant_status",
                "ix_project_app_analysis_runs_project_status",
                "ix_project_app_analysis_runs_tenant_project_created_at",
                "ix_project_app_analysis_runs_status",
                "ix_project_app_analysis_runs_project_id",
                "ix_project_app_analysis_runs_tenant_id",
            ],
        ),
        (
            "project_apps",
            [
                "ix_project_apps_tenant_status",
                "ix_project_apps_project_status",
                "ix_project_apps_tenant_project_created_at",
                "ix_project_apps_status",
                "ix_project_apps_project_id",
                "ix_project_apps_tenant_id",
            ],
        ),
    ):
        if _table_exists(table_name):
            for index_name in index_names:
                if _index_exists(table_name, index_name):
                    op.drop_index(index_name, table_name=table_name)
            op.drop_table(table_name)
