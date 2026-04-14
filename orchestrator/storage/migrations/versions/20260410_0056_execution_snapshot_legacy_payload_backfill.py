"""backfill legacy execution snapshot payloads into canonical format

Revision ID: 20260410_0056
Revises: 20260409_0055
Create Date: 2026-04-10 19:10:00.000000
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import sqlalchemy as sa
from alembic import op


revision = "20260410_0056"
down_revision = "20260409_0055"
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
        canonical = _canonicalize_snapshot_payload(row.get("plan"))
        if canonical is None:
            continue
        bind.execute(
            runs.update().where(runs.c.run_id == run_id).values(plan=canonical)
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
        canonical = _canonicalize_snapshot_payload(row.get("payload_json"))
        if canonical is None:
            continue
        bind.execute(
            checkpoints.update()
            .where(checkpoints.c.checkpoint_id == checkpoint_id)
            .values(payload_json=canonical, updated_at=now)
        )


def downgrade() -> None:
    return None


def _canonicalize_snapshot_payload(payload: object | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if _is_canonical_snapshot(payload):
        return None
    if not payload:
        return _empty_snapshot_payload()
    legacy_keys = {
        "attempts",
        "summary",
        "succeeded",
        "stage_updates",
        "stage_trace",
        "workstream_trace",
        "decision_gate",
        "test_guidance",
        "pr_url",
    }
    if not any(key in payload for key in legacy_keys):
        return None
    return _legacy_to_canonical_snapshot_payload(payload)


def _is_canonical_snapshot(payload: dict[str, Any]) -> bool:
    if payload.get("version") != 1:
        return False
    context = payload.get("context")
    workflow = payload.get("workflow")
    events = payload.get("events")
    stages = payload.get("stages")
    if not isinstance(context, dict) or not isinstance(workflow, dict) or not isinstance(events, dict):
        return False
    return isinstance(stages, dict)


def _legacy_to_canonical_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    attempts = payload.get("attempts")
    normalized_attempts = attempts if isinstance(attempts, int) and attempts >= 0 else 0
    summary = _coerce_string_list(payload.get("summary"))
    stage_updates = _coerce_dict_list(payload.get("stage_updates"))
    stage_trace = _coerce_dict_list(payload.get("stage_trace"))
    workstream_trace = _coerce_dict_list(payload.get("workstream_trace"))
    decision_gate = payload.get("decision_gate") if isinstance(payload.get("decision_gate"), dict) else {}
    outcome = _derive_legacy_outcome(payload=payload, decision_gate=decision_gate)
    blocker_message = _derive_legacy_blocker_message(payload=payload, decision_gate=decision_gate)
    return {
        "version": 1,
        "context": {
            "trigger_context": {},
            "execution_context": {},
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
            "live_stage_updates": [],
            "stage_trace": stage_trace,
            "workstream_trace": workstream_trace,
        },
        "stages": {},
    }


def _empty_snapshot_payload() -> dict[str, Any]:
    return {
        "version": 1,
        "context": {
            "trigger_context": {},
            "execution_context": {},
        },
        "workflow": {
            "outcome": None,
            "attempts": 0,
            "summary": [],
            "blocker_message": None,
            "requeue_target": None,
            "requeue_reason": None,
        },
        "events": {
            "stage_updates": [],
            "live_stage_updates": [],
            "stage_trace": [],
            "workstream_trace": [],
        },
        "stages": {},
    }


def _derive_legacy_outcome(*, payload: dict[str, Any], decision_gate: dict[str, Any]) -> str | None:
    explicit_outcome = payload.get("outcome")
    if isinstance(explicit_outcome, str):
        normalized = explicit_outcome.strip().lower()
        if normalized in {"success", "requeue", "waiting_for_input", "blocked", "failed"}:
            return normalized
    succeeded = payload.get("succeeded")
    if succeeded is True:
        return "success"
    if succeeded is False:
        if decision_gate.get("triggered") is True:
            return "blocked"
        return "failed"
    return None


def _derive_legacy_blocker_message(*, payload: dict[str, Any], decision_gate: dict[str, Any]) -> str | None:
    blocker = payload.get("blocker_message")
    if isinstance(blocker, str):
        normalized = blocker.strip()
        if normalized:
            return normalized
    reason = decision_gate.get("reason")
    if isinstance(reason, str):
        normalized = reason.strip()
        if normalized:
            return normalized
    return None


def _coerce_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        normalized = item.strip()
        if normalized:
            parsed.append(normalized)
    return parsed


def _coerce_dict_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    parsed: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict):
            parsed.append(dict(item))
    return parsed
