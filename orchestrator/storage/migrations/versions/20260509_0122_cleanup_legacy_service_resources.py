"""cleanup legacy deployment service resources

Revision ID: 20260509_0122
Revises: 20260509_0121
Create Date: 2026-05-09 18:05:00.000000
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260509_0122"
down_revision = "20260509_0121"
branch_labels = None
depends_on = None

_PUBLIC_SERVICE_TYPES = {"api", "web", "website", "frontend"}
_HELPER_SERVICE_TYPES = {
    "admin_ui",
    "dashboard",
    "database_browser",
    "diagnostic",
    "diagnostics",
    "elasticsearch_browser",
    "helper",
    "temporal_ui",
}


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names()


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    if table_name not in inspector.get_table_names():
        return False
    return any(column["name"] == column_name for column in inspector.get_columns(table_name))


def _coerce_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        decoded = json.loads(value)
        return dict(decoded) if isinstance(decoded, dict) else {}
    return {}


def _coerce_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _legacy_service_to_service(resource: dict[str, Any]) -> dict[str, Any] | None:
    if str(resource.get("kind") or "").strip().lower() != "service":
        return None
    config = _coerce_dict(resource.get("config"))
    service_type = str(config.get("service_type") or "").strip().lower()
    if service_type not in _PUBLIC_SERVICE_TYPES:
        return None
    service = {
        "key": resource.get("key"),
        "kind": "api" if service_type == "api" else "website",
        "name": resource.get("name"),
        "source_path": config.pop("source_path", None),
        "compose_service": config.pop("compose_service", None) or resource.get("key"),
        "build_strategy": config.pop("build_strategy", None),
        "container_port": config.pop("container_port", None)
        or config.pop("target_port", None)
        or config.pop("exposed_port", None)
        or config.pop("port", None),
        "public": config.pop("public", True) is not False,
        "config": {
            key: value
            for key, value in config.items()
            if key != "service_type"
        },
    }
    return {key: value for key, value in service.items() if value is not None and value != {}}


def _legacy_service_to_resource(resource: dict[str, Any]) -> dict[str, Any] | None:
    if str(resource.get("kind") or "").strip().lower() != "service":
        return resource
    config = _coerce_dict(resource.get("config"))
    service_type = str(config.get("service_type") or resource.get("key") or "").strip().lower()
    if service_type in _PUBLIC_SERVICE_TYPES or service_type in _HELPER_SERVICE_TYPES:
        return None
    normalized = dict(resource)
    normalized["kind"] = service_type
    config.pop("service_type", None)
    normalized["config"] = config
    return normalized


def _clean_resources(resources_value: Any, services_value: Any) -> tuple[list[Any], list[Any], bool]:
    changed = False
    resources: list[Any] = []
    services = _coerce_list(services_value)
    existing_service_keys = {
        str(service.get("key") or "").strip().lower()
        for service in services
        if isinstance(service, dict)
    }
    for item in _coerce_list(resources_value):
        if not isinstance(item, dict):
            resources.append(item)
            continue
        service = _legacy_service_to_service(dict(item))
        if service is not None:
            changed = True
            service_key = str(service.get("key") or "").strip().lower()
            if service_key and service_key not in existing_service_keys:
                services.append(service)
                existing_service_keys.add(service_key)
            continue
        resource = _legacy_service_to_resource(dict(item))
        if resource is None:
            changed = True
            continue
        if resource != item:
            changed = True
        resources.append(resource)
    return resources, services, changed


def _cleanup_deployment_config(payload: Any) -> tuple[dict[str, Any], bool]:
    config = _coerce_dict(payload)
    changed = False
    resources, services, resources_changed = _clean_resources(config.get("resources"), config.get("services"))
    if resources_changed:
        config["resources"] = resources
        config["services"] = services
        changed = True

    plan = _coerce_dict(config.get("deployment_plan"))
    if plan:
        plan_resources, plan_services, plan_changed = _clean_resources(plan.get("resources"), plan.get("services"))
        if plan_changed:
            plan["resources"] = plan_resources
            plan["services"] = plan_services
            config["deployment_plan"] = plan
            changed = True
    return config, changed


def _migrate_table(table_name: str, id_column: str) -> None:
    if not (_table_exists(table_name) and _column_exists(table_name, "deployment_config")):
        return
    bind = op.get_bind()
    table = sa.table(
        table_name,
        sa.column(id_column, sa.String()),
        sa.column("deployment_config", sa.JSON()),
    )
    rows = bind.execute(sa.text(f"SELECT {id_column}, deployment_config FROM {table_name}")).mappings().all()
    for row in rows:
        updated, changed = _cleanup_deployment_config(row["deployment_config"])
        if not changed:
            continue
        bind.execute(
            table.update()
            .where(getattr(table.c, id_column) == row[id_column])
            .values(deployment_config=updated)
        )


def upgrade() -> None:
    _migrate_table("projects", "project_id")
    _migrate_table("project_apps", "app_id")


def downgrade() -> None:
    return None
