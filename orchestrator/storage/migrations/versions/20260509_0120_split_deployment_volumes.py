"""split deployment volumes from resources

Revision ID: 20260509_0120
Revises: 20260509_0119
Create Date: 2026-05-09 17:10:00.000000
"""

from __future__ import annotations

import json
from typing import Any

from alembic import op
import sqlalchemy as sa


revision = "20260509_0120"
down_revision = "20260509_0119"
branch_labels = None
depends_on = None

_VOLUME_RESOURCE_KINDS = {"file", "persistent", "persistent_volume", "volume"}


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


def _volume_from_resource(resource: dict[str, Any]) -> dict[str, Any]:
    kind = str(resource.get("kind") or "").strip().lower()
    config = _coerce_dict(resource.get("config"))
    if kind not in {"volume", "persistent"}:
        config["legacy_resource_kind"] = kind
    return {
        "key": resource.get("key"),
        "type": "file" if kind == "file" else "persistent",
        "name": resource.get("name"),
        "config": config,
    }


def _split_resources_and_volumes(resources: Any, existing_volumes: Any) -> tuple[list[Any], list[Any], bool]:
    retained_resources: list[Any] = []
    volumes = _coerce_list(existing_volumes)
    changed = False
    existing_volume_keys = {
        str(volume.get("key") or "").strip().lower()
        for volume in volumes
        if isinstance(volume, dict)
    }
    for item in _coerce_list(resources):
        if not isinstance(item, dict):
            retained_resources.append(item)
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in _VOLUME_RESOURCE_KINDS:
            retained_resources.append(item)
            continue
        changed = True
        volume = _volume_from_resource(item)
        volume_key = str(volume.get("key") or "").strip().lower()
        if volume_key and volume_key not in existing_volume_keys:
            volumes.append(volume)
            existing_volume_keys.add(volume_key)
    return retained_resources, volumes, changed


def _split_deployment_config(payload: Any) -> tuple[dict[str, Any], bool]:
    config = _coerce_dict(payload)
    changed = False
    resources, volumes, resources_changed = _split_resources_and_volumes(
        config.get("resources"),
        config.get("volumes"),
    )
    if resources_changed:
        config["resources"] = resources
        config["volumes"] = volumes
        changed = True

    plan = _coerce_dict(config.get("deployment_plan"))
    if plan:
        plan_resources, plan_volumes, plan_changed = _split_resources_and_volumes(
            plan.get("resources"),
            plan.get("volumes"),
        )
        if plan_changed:
            plan["resources"] = plan_resources
            plan["volumes"] = plan_volumes
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
        updated, changed = _split_deployment_config(row["deployment_config"])
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
