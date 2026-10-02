"""canonicalize remaining legacy run.plan payloads into snapshot v1

Revision ID: 20260410_0057
Revises: 20260410_0056
Create Date: 2026-04-10 20:00:00.000000
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "20260410_0057"
down_revision = "20260410_0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    runs = sa.Table("runs", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(runs.c.run_id, runs.c.plan)
        .where(runs.c.plan.is_not(None))
        .order_by(runs.c.created_at.asc(), runs.c.run_id.asc())
    ).mappings()
    for row in rows:
        run_id = str(row.get("run_id") or "").strip()
        if not run_id:
            continue
        payload = row.get("plan")
        if not isinstance(payload, dict):
            continue
        if _is_canonical_snapshot(payload):
            continue
        canonical = _to_snapshot_v1(payload)
        bind.execute(
            runs.update().where(runs.c.run_id == run_id).values(plan=canonical)
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
    return (
        isinstance(context, dict)
        and isinstance(workflow, dict)
        and isinstance(events, dict)
        and isinstance(stages, dict)
    )


def _to_snapshot_v1(payload: dict[str, Any]) -> dict[str, Any]:
    trigger_context = payload.get("trigger_context")
    execution_context: dict[str, Any] = {}
    if isinstance(payload.get("pre_check"), dict):
        pre_check_outcome = payload["pre_check"].get("outcome")
        if isinstance(pre_check_outcome, str) and pre_check_outcome.strip():
            execution_context["pre_check_outcome"] = pre_check_outcome.strip()
    orchestration_mode = payload.get("orchestration_mode")
    if isinstance(orchestration_mode, str) and orchestration_mode.strip():
        execution_context["orchestration_mode"] = orchestration_mode.strip()
    attempts = payload.get("attempts")
    normalized_attempts = attempts if isinstance(attempts, int) and attempts >= 0 else 0
    summary = _coerce_string_list(payload.get("summary"))
    stage_updates = _coerce_dict_list(payload.get("stage_updates"))
    live_stage_updates = _coerce_dict_list(payload.get("live_stage_updates"))
    stage_trace = _coerce_dict_list(payload.get("stage_trace"))
    workstream_trace = _coerce_dict_list(payload.get("workstream_trace"))
    outcome = _derive_outcome(payload)
    blocker_message = _derive_blocker_message(payload)

    return {
        "version": 1,
        "context": {
            "trigger_context": dict(trigger_context)
            if isinstance(trigger_context, dict)
            else {},
            "execution_context": execution_context,
        },
        "workflow": {
            "outcome": outcome,
            "attempts": normalized_attempts,
            "summary": summary,
            "blocker_message": blocker_message,
            "requeue_target": None,
            "requeue_reason": None,
        },
        "events": {
            "stage_updates": stage_updates,
            "live_stage_updates": live_stage_updates,
            "stage_trace": stage_trace,
            "workstream_trace": workstream_trace,
        },
        "stages": {},
    }


def _derive_outcome(payload: dict[str, Any]) -> str | None:
    explicit_outcome = payload.get("outcome")
    if isinstance(explicit_outcome, str):
        normalized = explicit_outcome.strip().lower()
        if normalized in {
            "success",
            "requeue",
            "waiting_for_input",
            "blocked",
            "failed",
        }:
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
