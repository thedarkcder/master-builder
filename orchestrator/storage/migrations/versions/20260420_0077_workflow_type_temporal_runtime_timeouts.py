"""add workflow-level temporal timeout fields to workflow type engine config

Revision ID: 20260420_0077
Revises: 20260417_0076
Create Date: 2026-04-20 15:20:00.000000
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op


revision = "20260420_0077"
down_revision = "20260417_0076"
branch_labels = None
depends_on = None

_DEFAULT_WORKFLOW_EXECUTION_TIMEOUT_SECONDS = 86400
_DEFAULT_WORKFLOW_RUN_TIMEOUT_SECONDS = 86400


def _coerce_json_object(*, raw_value, workflow_type_key: str, field_name: str) -> dict:
    if isinstance(raw_value, dict):
        return dict(raw_value)
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"workflow type {workflow_type_key} has invalid {field_name}") from exc
        if isinstance(parsed, dict):
            return dict(parsed)
    raise RuntimeError(f"workflow type {workflow_type_key} is missing {field_name} object")


def _require_positive_int(*, workflow_type_key: str, temporal: dict, field_name: str) -> int:
    try:
        value = int(temporal.get(field_name) or 0)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"workflow type {workflow_type_key} has invalid temporal config field {field_name}"
        ) from exc
    if value < 1:
        raise RuntimeError(
            f"workflow type {workflow_type_key} is missing temporal config field {field_name}"
        )
    return value


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT workflow_type_key, orchestration_backend, engine_config_json "
            "FROM workflow_types ORDER BY workflow_type_key"
        )
    ).mappings()
    workflow_types = sa.table(
        "workflow_types",
        sa.column("workflow_type_key", sa.String()),
        sa.column("engine_config_json", sa.JSON()),
    )
    for row in rows:
        workflow_type_key = str(row["workflow_type_key"] or "").strip()
        if str(row["orchestration_backend"] or "").strip().lower() != "temporal":
            continue
        engine_config = _coerce_json_object(
            raw_value=row["engine_config_json"],
            workflow_type_key=workflow_type_key,
            field_name="engine_config_json",
        )
        temporal = _coerce_json_object(
            raw_value=engine_config.get("temporal"),
            workflow_type_key=workflow_type_key,
            field_name="engine_config_json.temporal",
        )
        temporal.setdefault(
            "workflow_execution_timeout_seconds",
            _DEFAULT_WORKFLOW_EXECUTION_TIMEOUT_SECONDS,
        )
        temporal.setdefault(
            "workflow_run_timeout_seconds",
            _DEFAULT_WORKFLOW_RUN_TIMEOUT_SECONDS,
        )
        _require_positive_int(
            workflow_type_key=workflow_type_key,
            temporal=temporal,
            field_name="workflow_execution_timeout_seconds",
        )
        _require_positive_int(
            workflow_type_key=workflow_type_key,
            temporal=temporal,
            field_name="workflow_run_timeout_seconds",
        )
        engine_config["temporal"] = temporal
        bind.execute(
            workflow_types.update()
            .where(workflow_types.c.workflow_type_key == workflow_type_key)
            .values(engine_config_json=engine_config)
        )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT workflow_type_key, orchestration_backend, engine_config_json "
            "FROM workflow_types ORDER BY workflow_type_key"
        )
    ).mappings()
    workflow_types = sa.table(
        "workflow_types",
        sa.column("workflow_type_key", sa.String()),
        sa.column("engine_config_json", sa.JSON()),
    )
    for row in rows:
        workflow_type_key = str(row["workflow_type_key"] or "").strip()
        if str(row["orchestration_backend"] or "").strip().lower() != "temporal":
            continue
        engine_config = _coerce_json_object(
            raw_value=row["engine_config_json"],
            workflow_type_key=workflow_type_key,
            field_name="engine_config_json",
        )
        temporal = _coerce_json_object(
            raw_value=engine_config.get("temporal"),
            workflow_type_key=workflow_type_key,
            field_name="engine_config_json.temporal",
        )
        temporal.pop("workflow_execution_timeout_seconds", None)
        temporal.pop("workflow_run_timeout_seconds", None)
        engine_config["temporal"] = temporal
        bind.execute(
            workflow_types.update()
            .where(workflow_types.c.workflow_type_key == workflow_type_key)
            .values(engine_config_json=engine_config)
        )
