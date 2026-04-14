"""canonicalize remaining workflow checkpoint snapshot payloads into snapshot v1

Revision ID: 20260410_0058
Revises: 20260410_0057
Create Date: 2026-04-10 22:15:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "20260410_0058"
down_revision = "20260410_0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    runs = sa.Table("runs", metadata, autoload_with=bind)
    checkpoints = sa.Table("workflow_checkpoints", metadata, autoload_with=bind)

    run_rows = bind.execute(
        sa.select(runs.c.run_id, runs.c.plan)
        .where(runs.c.plan.is_not(None))
        .order_by(runs.c.created_at.asc(), runs.c.run_id.asc())
    ).mappings()
    for row in run_rows:
        run_id = str(row.get("run_id") or "").strip()
        if not run_id:
            continue
        payload = row.get("plan")
        if not isinstance(payload, dict):
            continue
        if _is_canonical_snapshot(payload):
            continue
        bind.execute(
            runs.update().where(runs.c.run_id == run_id).values(plan=_to_snapshot_v1(payload))
        )

    checkpoint_rows = bind.execute(
        sa.select(checkpoints.c.checkpoint_id, checkpoints.c.payload_json)
        .where(checkpoints.c.payload_json.is_not(None))
        .order_by(checkpoints.c.created_at.asc(), checkpoints.c.checkpoint_id.asc())
    ).mappings()
    now = datetime.now(timezone.utc)
    for row in checkpoint_rows:
        checkpoint_id = str(row.get("checkpoint_id") or "").strip()
        if not checkpoint_id:
            continue
        payload = row.get("payload_json")
        if not isinstance(payload, dict):
            continue
        if _is_canonical_snapshot(payload):
            continue
        bind.execute(
            checkpoints.update()
            .where(checkpoints.c.checkpoint_id == checkpoint_id)
            .values(payload_json=_to_snapshot_v1(payload), updated_at=now)
        )


def downgrade() -> None:
    return None


def _is_canonical_snapshot(payload: dict[str, Any]) -> bool:
    if payload.get("version") != 1:
        return False
    context = payload.get("context")
    workflow = payload.get("workflow")
    events = payload.get("events")
    stages = payload.get("stages")
    return isinstance(context, dict) and isinstance(workflow, dict) and isinstance(events, dict) and isinstance(stages, dict)


def _to_snapshot_v1(payload: dict[str, Any]) -> dict[str, Any]:
    trigger_context = _coerce_dict(payload.get("trigger_context"))
    execution_context = _coerce_dict(payload.get("execution_context"))
    pre_check = payload.get("pre_check")
    if isinstance(pre_check, dict):
        outcome = pre_check.get("outcome")
        if isinstance(outcome, str) and outcome.strip():
            execution_context.setdefault("pre_check_outcome", outcome.strip())
    orchestration_mode = payload.get("orchestration_mode")
    if isinstance(orchestration_mode, str) and orchestration_mode.strip():
        execution_context.setdefault("orchestration_mode", orchestration_mode.strip())

    return {
        "version": 1,
        "context": {
            "trigger_context": trigger_context,
            "execution_context": execution_context,
        },
        "workflow": {
            "outcome": _derive_outcome(payload),
            "attempts": _coerce_non_negative_int(payload.get("attempts")),
            "summary": _coerce_string_list(payload.get("summary")),
            "blocker_message": _derive_blocker_message(payload),
            "requeue_target": None,
            "requeue_reason": None,
        },
        "events": {
            "stage_updates": _coerce_dict_list(payload.get("stage_updates")),
            "live_stage_updates": _coerce_dict_list(payload.get("live_stage_updates")),
            "stage_trace": _coerce_dict_list(payload.get("stage_trace")),
            "workstream_trace": _coerce_dict_list(payload.get("workstream_trace")),
        },
        "stages": {},
    }


def _derive_outcome(payload: dict[str, Any]) -> str | None:
    explicit_outcome = payload.get("outcome")
    if isinstance(explicit_outcome, str):
        normalized = explicit_outcome.strip().lower()
        if normalized in {"success", "requeue", "waiting_for_input", "blocked", "failed"}:
            return normalized
    succeeded = payload.get("succeeded")
    if succeeded is True:
        return "success"
    if succeeded is False:
        decision_gate = payload.get("decision_gate")
        if isinstance(decision_gate, dict) and decision_gate.get("triggered") is True:
            return "blocked"
        return "failed"
    return None


def _derive_blocker_message(payload: dict[str, Any]) -> str | None:
    blocker_message = payload.get("blocker_message")
    if isinstance(blocker_message, str) and blocker_message.strip():
        return blocker_message.strip()
    decision_gate = payload.get("decision_gate")
    if isinstance(decision_gate, dict):
        reason = decision_gate.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason.strip()
    return None


def _coerce_non_negative_int(value: object) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    return 0


def _coerce_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        text = item.strip()
        if text:
            normalized.append(text)
    return normalized


def _coerce_dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    normalized: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            normalized.append(dict(item))
    return normalized


def _coerce_dict(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return dict(value)
