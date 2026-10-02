"""Redact raw deployment config secret material.

Revision ID: 20260507_0113
Revises: 20260507_0112
Create Date: 2026-05-07 00:00:00.000000
"""

from __future__ import annotations

import json
import re

from alembic import op
from sqlalchemy import inspect, text


revision = "20260507_0113"
down_revision = "20260507_0112"
branch_labels = None
depends_on = None

_RAW_SECRET_CONFIG_KEY_PARTS = {
    "password",
    "secret",
    "token",
    "private_key",
    "access_key",
}


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


def _is_raw_secret_config_key(key: object) -> bool:
    normalized = str(key or "").strip().lower()
    if (
        not normalized
        or normalized.endswith("_secret_ref")
        or normalized.endswith("_secret_refs")
    ):
        return False
    if normalized == "secret_refs":
        return False
    parts = {part for part in re.split(r"[^a-z0-9]+", normalized) if part}
    if "secret" in parts and "ref" in parts:
        return False
    return bool(parts & _RAW_SECRET_CONFIG_KEY_PARTS)


def _redact_secret_config(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _redact_secret_config(child)
            for key, child in value.items()
            if not _is_raw_secret_config_key(key)
        }
    if isinstance(value, list):
        return [_redact_secret_config(child) for child in value]
    return value


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
        redacted = _redact_secret_config(original)
        if redacted == original:
            continue
        bind.execute(
            text(
                f"UPDATE {table_name} SET {json_column} = :payload WHERE {id_column} = :row_id"
            ),
            {
                "payload": json.dumps(redacted, sort_keys=True),
                "row_id": row_id,
            },
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
