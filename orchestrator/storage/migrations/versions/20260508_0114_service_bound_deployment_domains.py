"""Bind deployment domains to services.

Revision ID: 20260508_0114
Revises: 20260507_0113
Create Date: 2026-05-08 00:00:00.000000
"""

from __future__ import annotations

import json

from alembic import op
from sqlalchemy import inspect, text


revision = "20260508_0114"
down_revision = "20260507_0113"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _loads_json(value: object) -> object:
    if isinstance(value, (dict, list)):
        return value
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_key(value: object) -> str:
    return str(value or "").strip().lower().replace(" ", "-") or "app"


def _service_key_for_domain(
    domain: dict[str, object], config: dict[str, object]
) -> str:
    existing = str(domain.get("service_key") or "").strip()
    if existing:
        return _normalize_key(existing)
    resources = config.get("resources")
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            resource_config = resource.get("config")
            if not isinstance(resource_config, dict):
                continue
            service_type = (
                str(resource_config.get("service_type") or "").strip().lower()
            )
            compose_service = str(
                resource_config.get("compose_service") or resource.get("key") or ""
            ).strip()
            if service_type in {"web", "website", "api"} or any(
                marker in compose_service.lower()
                for marker in ("web", "ui", "frontend", "app", "api")
            ):
                return _normalize_key(compose_service)
    return "app"


def _rewrite_config(value: object) -> object:
    if not isinstance(value, dict):
        return value
    domains = value.get("domains")
    if not isinstance(domains, list):
        return value
    changed = False
    rewritten: list[object] = []
    for domain in domains:
        if not isinstance(domain, dict):
            rewritten.append(domain)
            continue
        next_domain = dict(domain)
        if "is_primary" in next_domain:
            next_domain.pop("is_primary", None)
            changed = True
        if not str(next_domain.get("service_key") or "").strip():
            next_domain["service_key"] = _service_key_for_domain(next_domain, value)
            changed = True
        rewritten.append(next_domain)
    if not changed:
        return value
    return {**value, "domains": rewritten}


def _update_json_rows(*, table_name: str, id_column: str, json_column: str) -> None:
    if not _table_exists(table_name):
        return
    bind = op.get_bind()
    rows = (
        bind.execute(text(f"SELECT {id_column}, {json_column} FROM {table_name}"))
        .mappings()
        .all()
    )
    for row in rows:
        row_id = str(row.get(id_column) or "").strip()
        if not row_id:
            continue
        original = _loads_json(row.get(json_column))
        rewritten = _rewrite_config(original)
        if rewritten == original:
            continue
        bind.execute(
            text(
                f"UPDATE {table_name} SET {json_column} = :payload WHERE {id_column} = :row_id"
            ),
            {"payload": json.dumps(rewritten, sort_keys=True), "row_id": row_id},
        )


def upgrade() -> None:
    _update_json_rows(
        table_name="projects", id_column="project_id", json_column="deployment_config"
    )
    _update_json_rows(
        table_name="project_apps", id_column="app_id", json_column="deployment_config"
    )
    _update_json_rows(
        table_name="project_deployment_releases",
        id_column="release_id",
        json_column="deployment_snapshot",
    )


def downgrade() -> None:
    pass
