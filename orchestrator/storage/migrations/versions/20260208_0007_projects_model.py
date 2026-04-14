"""add projects model and backfill tenant default projects

Revision ID: 20260208_0007
Revises: 20260207_0006
Create Date: 2026-02-08 19:50:00.000000
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from enum import Enum

from alembic import op
import sqlalchemy as sa


revision = "20260208_0007"
down_revision = "20260207_0006"
branch_labels = None
depends_on = None


class JiraConfigKey(str, Enum):
    PROJECT_KEYS = "project_keys"
    READY_JQL = "ready_jql"


def _normalize_repo_url(raw: object) -> str:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return ""


def _as_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _repo_from_repos_config(repos_config: object) -> str:
    repos_config = _as_dict(repos_config)

    candidates: list[object] = [
        repos_config.get("github_repository"),
        repos_config.get("fallback_repo"),
    ]

    allowlist = repos_config.get("allowlist")
    if isinstance(allowlist, list):
        candidates.extend(allowlist)

    by_project = repos_config.get("mapping_rules_by_project_key")
    if isinstance(by_project, dict):
        candidates.extend(by_project.values())

    by_component = repos_config.get("mapping_rules_by_component")
    if isinstance(by_component, dict):
        candidates.extend(by_component.values())

    for candidate in candidates:
        normalized = _normalize_repo_url(candidate)
        if normalized:
            return normalized
    return ""


def _jira_project_key_from_config(jira_config: object) -> str:
    jira_config = _as_dict(jira_config)

    project_keys = jira_config.get(JiraConfigKey.PROJECT_KEYS.value)
    if isinstance(project_keys, list):
        for key in project_keys:
            if isinstance(key, str) and key.strip():
                return key.strip().upper()

    ready_jql = jira_config.get(JiraConfigKey.READY_JQL.value)
    if isinstance(ready_jql, str):
        match = re.search(r"\bproject\s*(?:=|IN\s*\()\s*\"?([A-Z][A-Z0-9_]+)", ready_jql, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip().upper()
    return ""


def _default_project_name(repo_url: str, tenant_id: str) -> str:
    cleaned = repo_url.rstrip("/")
    if cleaned.endswith(".git"):
        cleaned = cleaned[:-4]
    candidate = cleaned.rsplit("/", 1)[-1].strip()
    if candidate:
        return candidate
    return f"{tenant_id}-project"


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("project_id", sa.String(length=128), nullable=False),
        sa.Column("tenant_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("github_repository", sa.String(length=512), nullable=False),
        sa.Column("jira_project_key", sa.String(length=64), nullable=False),
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.tenant_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("project_id"),
        sa.UniqueConstraint("tenant_id", "github_repository", name="uq_projects_tenant_repo"),
        sa.UniqueConstraint("tenant_id", "jira_project_key", name="uq_projects_tenant_jira_key"),
    )
    op.create_index("ix_projects_tenant_id", "projects", ["tenant_id"], unique=False)
    op.create_index("ix_projects_is_archived", "projects", ["is_archived"], unique=False)

    connection = op.get_bind()
    projects_table = sa.table(
        "projects",
        sa.column("project_id", sa.String),
        sa.column("tenant_id", sa.String),
        sa.column("name", sa.String),
        sa.column("github_repository", sa.String),
        sa.column("jira_project_key", sa.String),
        sa.column("is_archived", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )

    tenant_rows = connection.execute(
        sa.text("SELECT tenant_id, jira_config, repos_config FROM tenants")
    ).mappings()

    now = datetime.now(timezone.utc)
    for row in tenant_rows:
        tenant_id = str(row["tenant_id"])
        existing = connection.execute(
            sa.text("SELECT 1 FROM projects WHERE tenant_id = :tenant_id LIMIT 1"),
            {"tenant_id": tenant_id},
        ).first()
        if existing:
            continue

        repo_url = _repo_from_repos_config(row.get("repos_config"))
        jira_key = _jira_project_key_from_config(row.get("jira_config"))
        if not repo_url or not jira_key:
            continue

        connection.execute(
            projects_table.insert().values(
                project_id=f"{tenant_id}-default",
                tenant_id=tenant_id,
                name=_default_project_name(repo_url=repo_url, tenant_id=tenant_id),
                github_repository=repo_url,
                jira_project_key=jira_key,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
        )


def downgrade() -> None:
    op.drop_index("ix_projects_is_archived", table_name="projects")
    op.drop_index("ix_projects_tenant_id", table_name="projects")
    op.drop_table("projects")
