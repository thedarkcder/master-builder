from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.models import Run, WorkflowCheckpoint


@dataclass(frozen=True)
class ExecutionSnapshotMigrationReport:
    scanned_runs: int
    converted_runs: int
    invalid_runs: int
    scanned_checkpoints: int
    converted_checkpoints: int
    invalid_checkpoints: int
    invalid_run_ids: tuple[str, ...]
    invalid_checkpoint_ids: tuple[str, ...]


def migrate_execution_snapshots(
    session: Session,
    *,
    apply: bool,
    limit: int | None = None,
) -> ExecutionSnapshotMigrationReport:
    run_statement = select(Run).order_by(Run.created_at.asc(), Run.run_id.asc())
    checkpoint_statement = select(WorkflowCheckpoint).order_by(
        WorkflowCheckpoint.created_at.asc(),
        WorkflowCheckpoint.checkpoint_id.asc(),
    )
    if isinstance(limit, int) and limit > 0:
        run_statement = run_statement.limit(limit)
        checkpoint_statement = checkpoint_statement.limit(limit)

    scanned_runs = 0
    converted_runs = 0
    invalid_runs = 0
    invalid_run_ids: list[str] = []

    for run in session.execute(run_statement).scalars().all():
        scanned_runs += 1
        payload = getattr(run, "plan", None)
        if payload is None:
            continue
        canonical = canonicalize_snapshot_payload(payload)
        if canonical is None:
            invalid_runs += 1
            invalid_run_ids.append(str(run.run_id))
            continue
        if ExecutionSnapshot.load(payload) is not None:
            continue
        converted_runs += 1
        if apply:
            run.plan = canonical.dump()

    scanned_checkpoints = 0
    converted_checkpoints = 0
    invalid_checkpoints = 0
    invalid_checkpoint_ids: list[str] = []

    for checkpoint in session.execute(checkpoint_statement).scalars().all():
        scanned_checkpoints += 1
        payload = getattr(checkpoint, "payload_json", None)
        canonical = canonicalize_snapshot_payload(payload)
        if canonical is None:
            invalid_checkpoints += 1
            invalid_checkpoint_ids.append(str(checkpoint.checkpoint_id))
            continue
        if ExecutionSnapshot.load(payload) is not None:
            continue
        converted_checkpoints += 1
        if apply:
            checkpoint.payload_json = canonical.dump()

    if apply:
        session.commit()
    else:
        session.rollback()

    return ExecutionSnapshotMigrationReport(
        scanned_runs=scanned_runs,
        converted_runs=converted_runs,
        invalid_runs=invalid_runs,
        scanned_checkpoints=scanned_checkpoints,
        converted_checkpoints=converted_checkpoints,
        invalid_checkpoints=invalid_checkpoints,
        invalid_run_ids=tuple(invalid_run_ids),
        invalid_checkpoint_ids=tuple(invalid_checkpoint_ids),
    )


def canonicalize_snapshot_payload(payload: object | None) -> ExecutionSnapshot | None:
    if payload is None:
        return None
    loaded = ExecutionSnapshot.load(payload)
    if loaded is not None:
        return loaded
    if not isinstance(payload, dict):
        return None
    return _legacy_to_canonical_snapshot(payload)


def _legacy_to_canonical_snapshot(payload: dict[str, Any]) -> ExecutionSnapshot:
    snapshot = ExecutionSnapshot.empty(
        trigger_context=_coerce_dict(payload.get("trigger_context")),
    )
    snapshot.context.execution_context = _derive_execution_context(payload)
    snapshot.workflow.outcome = _derive_outcome(payload)
    snapshot.workflow.attempts = _coerce_non_negative_int(payload.get("attempts"))
    snapshot.workflow.summary = _coerce_string_list(payload.get("summary"))
    snapshot.workflow.blocker_message = _derive_blocker_message(payload)
    snapshot.events.stage_updates = _coerce_dict_list(payload.get("stage_updates"))
    snapshot.events.live_stage_updates = _coerce_dict_list(payload.get("live_stage_updates"))
    snapshot.events.stage_trace = _coerce_dict_list(payload.get("stage_trace"))
    snapshot.events.workstream_trace = _coerce_dict_list(payload.get("workstream_trace"))
    return snapshot


def _derive_execution_context(payload: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = _coerce_dict(payload.get("execution_context"))
    pre_check = payload.get("pre_check")
    if isinstance(pre_check, dict):
        outcome = pre_check.get("outcome")
        if isinstance(outcome, str) and outcome.strip():
            context.setdefault("pre_check_outcome", outcome.strip())
    orchestration_mode = payload.get("orchestration_mode")
    if isinstance(orchestration_mode, str) and orchestration_mode.strip():
        context.setdefault("orchestration_mode", orchestration_mode.strip())
    return context


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
