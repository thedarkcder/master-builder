"""backfill tenant discord thread channels into project-scoped config

Revision ID: 20260210_0012
Revises: 20260209_0011
Create Date: 2026-02-10 17:45:00.000000
"""

from __future__ import annotations

from collections import defaultdict

from alembic import op
import sqlalchemy as sa


revision = "20260210_0012"
down_revision = "20260209_0011"
branch_labels = None
depends_on = None


def _normalize_id_list(raw_value: object) -> list[str]:
    if not isinstance(raw_value, list):
        return []
    normalized: list[str] = []
    for value in raw_value:
        text = str(value or "").strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def _issue_project_key(issue_key: str) -> str:
    normalized = issue_key.strip().upper()
    if "-" not in normalized:
        return ""
    return normalized.split("-", 1)[0]


def _project_id_for_entry(*, entry: dict, project_rows: list[dict]) -> str | None:
    raw_channel_ids = _normalize_id_list(entry.get("channel_ids"))
    issue_keys = _normalize_id_list(entry.get("issue_keys"))
    for row in project_rows:
        project_channel_id = str(
            (row.get("discord_config") or {}).get("channel_id") or ""
        ).strip()
        if project_channel_id and project_channel_id in raw_channel_ids:
            return str(row["project_id"])
    if issue_keys:
        for row in project_rows:
            jira_key = str(row.get("jira_project_key") or "").strip().upper()
            if not jira_key:
                continue
            if any(
                _issue_project_key(issue_key) == jira_key for issue_key in issue_keys
            ):
                return str(row["project_id"])
    return None


def upgrade() -> None:
    bind = op.get_bind()
    tenants = sa.table(
        "tenants",
        sa.column("tenant_id", sa.String()),
        sa.column("discord_config", sa.JSON()),
    )
    projects = sa.table(
        "projects",
        sa.column("project_id", sa.String()),
        sa.column("tenant_id", sa.String()),
        sa.column("jira_project_key", sa.String()),
        sa.column("discord_config", sa.JSON()),
        sa.column("is_archived", sa.Boolean()),
    )

    tenant_rows = bind.execute(
        sa.select(tenants.c.tenant_id, tenants.c.discord_config)
    ).all()
    project_rows_raw = bind.execute(
        sa.select(
            projects.c.project_id,
            projects.c.tenant_id,
            projects.c.jira_project_key,
            projects.c.discord_config,
        ).where(projects.c.is_archived.is_(False))
    ).all()
    projects_by_tenant: dict[str, list[dict]] = defaultdict(list)
    for row in project_rows_raw:
        projects_by_tenant[str(row.tenant_id)].append(
            {
                "project_id": str(row.project_id),
                "tenant_id": str(row.tenant_id),
                "jira_project_key": str(row.jira_project_key or "").strip().upper(),
                "discord_config": dict(row.discord_config or {}),
            }
        )

    for tenant_row in tenant_rows:
        tenant_id = str(tenant_row.tenant_id)
        tenant_discord_config = dict(tenant_row.discord_config or {})
        tenant_ask_thread_list = _normalize_id_list(
            tenant_discord_config.get("ask_thread_channel_ids")
        )
        tenant_seed_thread_list = _normalize_id_list(
            tenant_discord_config.get("seed_followup_thread_channel_ids")
        )
        tenant_ask_threads = set(tenant_ask_thread_list)
        tenant_seed_threads = set(tenant_seed_thread_list)
        if not tenant_ask_threads and not tenant_seed_threads:
            continue

        tenant_projects = projects_by_tenant.get(tenant_id, [])
        if not tenant_projects:
            continue

        assigned_ask: dict[str, set[str]] = defaultdict(set)
        assigned_seed: dict[str, set[str]] = defaultdict(set)

        if len(tenant_projects) == 1:
            only_project_id = str(tenant_projects[0]["project_id"])
            assigned_ask[only_project_id].update(tenant_ask_threads)
            assigned_seed[only_project_id].update(tenant_seed_threads)
        else:
            seed_followups = tenant_discord_config.get("seed_followups")
            if isinstance(seed_followups, list):
                for raw_entry in seed_followups:
                    if not isinstance(raw_entry, dict):
                        continue
                    project_id = _project_id_for_entry(
                        entry=raw_entry, project_rows=tenant_projects
                    )
                    if not project_id:
                        continue
                    channel_ids = set(_normalize_id_list(raw_entry.get("channel_ids")))
                    assigned_ask[project_id].update(
                        channel_ids.intersection(tenant_ask_threads)
                    )
                    assigned_seed[project_id].update(
                        channel_ids.intersection(tenant_seed_threads)
                    )

        for project in tenant_projects:
            project_id = str(project["project_id"])
            project_discord_config = dict(project.get("discord_config") or {})
            ask_threads = _normalize_id_list(
                project_discord_config.get("ask_thread_channel_ids")
            )
            seed_threads = _normalize_id_list(
                project_discord_config.get("seed_followup_thread_channel_ids")
            )
            changed = False
            for channel_id in sorted(assigned_ask.get(project_id, set())):
                if channel_id not in ask_threads:
                    ask_threads.append(channel_id)
                    changed = True
            for channel_id in sorted(assigned_seed.get(project_id, set())):
                if channel_id not in seed_threads:
                    seed_threads.append(channel_id)
                    changed = True
            if changed:
                project_discord_config["ask_thread_channel_ids"] = ask_threads[-200:]
                project_discord_config["seed_followup_thread_channel_ids"] = (
                    seed_threads[-200:]
                )
                bind.execute(
                    projects.update()
                    .where(projects.c.project_id == project_id)
                    .values(discord_config=project_discord_config)
                )

        assigned_ask_channels = (
            set().union(*assigned_ask.values()) if assigned_ask else set()
        )
        assigned_seed_channels = (
            set().union(*assigned_seed.values()) if assigned_seed else set()
        )
        remaining_ask_channels = [
            channel_id
            for channel_id in tenant_ask_thread_list
            if channel_id not in assigned_ask_channels
        ]
        remaining_seed_channels = [
            channel_id
            for channel_id in tenant_seed_thread_list
            if channel_id not in assigned_seed_channels
        ]
        if remaining_ask_channels:
            tenant_discord_config["ask_thread_channel_ids"] = remaining_ask_channels
        else:
            tenant_discord_config.pop("ask_thread_channel_ids", None)
        if remaining_seed_channels:
            tenant_discord_config["seed_followup_thread_channel_ids"] = (
                remaining_seed_channels
            )
        else:
            tenant_discord_config.pop("seed_followup_thread_channel_ids", None)
        bind.execute(
            tenants.update()
            .where(tenants.c.tenant_id == tenant_id)
            .values(discord_config=tenant_discord_config)
        )


def downgrade() -> None:
    # Migration is intentionally non-destructive.
    pass
