from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.workflow.checkpoint_codec import (
    decode_dev_result_payload,
    decode_pm_plan_payload,
    decode_review_result_payload,
    decode_test_result_payload,
)
from orchestrator.core.workflow.execution_snapshot import (
    ExecutionSnapshot,
    ExecutionStageRecord,
    StageName,
)
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
    if not payload:
        return ExecutionSnapshot.empty()
    return _coerce_legacy_payload(payload)


def _coerce_legacy_payload(payload: dict[str, Any]) -> ExecutionSnapshot | None:
    trigger_context = payload.get("trigger_context")
    snapshot = ExecutionSnapshot.empty(
        trigger_context=dict(trigger_context) if isinstance(trigger_context, dict) else None
    )

    legacy_execution_context = payload.get("execution_context")
    if isinstance(legacy_execution_context, dict):
        snapshot.context.execution_context.update(dict(legacy_execution_context))

    legacy_pre_check = payload.get("pre_check")
    if (
        "pre_check_outcome" not in snapshot.context.execution_context
        and isinstance(legacy_pre_check, dict)
    ):
        pre_check_outcome = _normalize_optional_str(legacy_pre_check.get("outcome"))
        if pre_check_outcome is not None:
            snapshot.context.execution_context["pre_check_outcome"] = pre_check_outcome

    for legacy_key in ("run_not_ready", "required_worker_label", "stale_branch_snapshot"):
        if legacy_key in payload and legacy_key not in snapshot.context.execution_context:
            snapshot.context.execution_context[legacy_key] = payload.get(legacy_key)

    legacy_required_capability = _normalize_optional_str(payload.get("required_worker_capability"))
    legacy_requeue_target = _normalize_optional_str(payload.get("requeue_target"))
    snapshot.workflow.requeue_target = legacy_requeue_target or legacy_required_capability
    snapshot.workflow.requeue_reason = _normalize_optional_str(payload.get("requeue_reason"))
    snapshot.workflow.outcome = _normalize_optional_str(payload.get("outcome"))

    legacy_attempts = payload.get("attempts")
    if isinstance(legacy_attempts, int) and legacy_attempts >= 0:
        snapshot.workflow.attempts = legacy_attempts
    snapshot.workflow.summary = _normalize_string_list(payload.get("summary"))
    snapshot.workflow.blocker_message = _normalize_optional_str(payload.get("blocker_message"))

    for attr_name, legacy_key in (
        ("stage_updates", "stage_updates"),
        ("live_stage_updates", "live_stage_updates"),
        ("stage_trace", "stage_trace"),
        ("workstream_trace", "workstream_trace"),
    ):
        normalized = _normalize_dict_list(payload.get(legacy_key))
        if normalized is not None:
            setattr(snapshot.events, attr_name, normalized)

    stage_checkpoints = payload.get("stage_checkpoints")
    if isinstance(stage_checkpoints, dict):
        for stage_name, stage_payload in stage_checkpoints.items():
            try:
                stage = StageName(stage_name)
            except ValueError:
                continue
            record = _coerce_legacy_stage_record(
                stage=stage,
                raw=stage_payload,
                payload=payload,
            )
            if record is not None:
                snapshot.stages[stage.value] = record

    if "pm" not in snapshot.stages:
        legacy_plan = payload.get("plan")
        if isinstance(legacy_plan, dict):
            decoded_plan = decode_pm_plan_payload(legacy_plan)
            if decoded_plan is not None:
                snapshot.stages["pm"] = ExecutionStageRecord(
                    attempt=1,
                    status="completed",
                    summary="Recovered PM plan from legacy payload.",
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    artifact=dict(legacy_plan),
                )

    _coerce_legacy_top_level_stage_artifacts(payload=payload, snapshot=snapshot)
    return snapshot


def _coerce_legacy_stage_record(
    *,
    stage: StageName,
    raw: object,
    payload: dict[str, Any],
) -> ExecutionStageRecord | None:
    if not isinstance(raw, dict):
        return None
    attempt_raw = raw.get("attempt")
    attempt = attempt_raw if isinstance(attempt_raw, int) and attempt_raw > 0 else 1
    status = _normalize_optional_str(raw.get("status")) or "completed"
    summary = _normalize_optional_str(raw.get("summary")) or f"Recovered {stage.value} stage checkpoint."
    completed_at = _normalize_optional_str(raw.get("completed_at")) or datetime.now(timezone.utc).isoformat()
    artifact = raw.get("artifact")
    normalized_artifact: dict[str, Any] | None = None
    if isinstance(artifact, dict):
        if stage == StageName.PM and decode_pm_plan_payload(artifact) is not None:
            normalized_artifact = dict(artifact)
        elif stage == StageName.DEV and decode_dev_result_payload(artifact) is not None:
            normalized_artifact = dict(artifact)
        elif stage == StageName.TEST and decode_test_result_payload(artifact) is not None:
            normalized_artifact = dict(artifact)
        elif stage == StageName.REVIEW and decode_review_result_payload(artifact) is not None:
            normalized_artifact = dict(artifact)
    if normalized_artifact is None:
        normalized_artifact = _coerce_legacy_stage_artifact(stage=stage, payload=payload)
    return ExecutionStageRecord(
        attempt=attempt,
        status=status,
        summary=summary,
        completed_at=completed_at,
        artifact=normalized_artifact,
    )


def _coerce_legacy_stage_artifact(*, stage: StageName, payload: dict[str, Any]) -> dict[str, Any] | None:
    if stage == StageName.PM:
        legacy_plan = payload.get("plan")
        if isinstance(legacy_plan, dict) and decode_pm_plan_payload(legacy_plan) is not None:
            return dict(legacy_plan)
        return None
    if stage == StageName.DEV:
        change_summary = _normalize_string_list(payload.get("dev_rationale"))
        if not change_summary:
            return None
        candidate = {
            "outcome": "continue",
            "change_summary": change_summary,
            "pr_url": _normalize_optional_str(payload.get("pr_url")),
            "blocker_message": None,
        }
        return candidate if decode_dev_result_payload(candidate) is not None else None
    if stage == StageName.TEST:
        guidance = _normalize_string_list(payload.get("test_guidance"))
        if not guidance:
            return None
        candidate = {
            "outcome": "continue",
            "guidance": guidance,
            "feedback": _normalize_optional_str(payload.get("test_feedback")),
            "blocker_message": None,
        }
        return candidate if decode_test_result_payload(candidate) is not None else None
    if stage == StageName.REVIEW:
        review_summary = _normalize_string_list(payload.get("review_summary"))
        if not review_summary:
            return None
        candidate = {
            "outcome": "continue",
            "summary": review_summary,
            "feedback": _normalize_optional_str(payload.get("review_feedback")),
            "pr_url": _normalize_optional_str(payload.get("pr_url")),
            "blocker_message": None,
        }
        return candidate if decode_review_result_payload(candidate) is not None else None
    return None


def _coerce_legacy_top_level_stage_artifacts(*, payload: dict[str, Any], snapshot: ExecutionSnapshot) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for stage in (StageName.DEV, StageName.TEST, StageName.REVIEW):
        if stage.value in snapshot.stages:
            continue
        artifact = _coerce_legacy_stage_artifact(stage=stage, payload=payload)
        if artifact is None:
            continue
        snapshot.stages[stage.value] = ExecutionStageRecord(
            attempt=1,
            status="completed",
            summary=f"Recovered {stage.value} stage artifact from legacy payload.",
            completed_at=now,
            artifact=artifact,
        )


def _normalize_optional_str(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_dict_list(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    normalized: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        normalized.append(dict(item))
    return normalized


def _normalize_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        text = _normalize_optional_str(item)
        if text is not None:
            normalized.append(text)
    return normalized
