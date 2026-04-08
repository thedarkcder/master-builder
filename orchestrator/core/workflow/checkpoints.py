from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.workflow.checkpoint_codec import decode_pm_plan_payload
from orchestrator.core.workflow.checkpoint_codec import encode_pm_plan
from orchestrator.storage.models import WorkflowCheckpoint, WorkflowExecution

_CHECKPOINT_KIND_BY_STAGE = {
    "orchestrated": "orchestrated",
    "orchestrated_run": "orchestrated",
    "pm": "pm",
    "dev": "execution",
    "test": "execution",
    "review": "execution",
}


def checkpoint_kind_for_stage(stage: str | None) -> str | None:
    normalized_stage = str(stage or "").strip().lower()
    return _CHECKPOINT_KIND_BY_STAGE.get(normalized_stage)


def normalize_checkpoint_stage(stage: str | None) -> str | None:
    normalized_stage = str(stage or "").strip().lower()
    if not normalized_stage:
        return None
    if normalized_stage == "orchestrated_run":
        return "orchestrated"
    return normalized_stage


def checkpoint_id_for(*, run_id: str, checkpoint_kind: str) -> str:
    base = f"{run_id}-{checkpoint_kind}"
    if len(base) <= 64:
        return base
    digest = hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]
    prefix_budget = max(1, 64 - len(checkpoint_kind) - len(digest) - 2)
    return f"{run_id[:prefix_budget]}-{checkpoint_kind}-{digest}"


def checkpoint_payload_for_plan(*, checkpoint_kind: str, plan: object) -> dict:
    if not isinstance(plan, dict):
        return {}
    if checkpoint_kind == "pm":
        payload = plan.get("plan")
        decoded = decode_pm_plan_payload(payload if isinstance(payload, dict) else None)
        return encode_pm_plan(decoded) if decoded is not None else {}
    if checkpoint_kind == "execution":
        return {
            "plan": dict(plan.get("plan")) if isinstance(plan.get("plan"), dict) else {},
            "dev_rationale": list(plan.get("dev_rationale", []) or []),
            "test_guidance": list(plan.get("test_guidance", []) or []),
            "test_feedback": str(plan.get("test_feedback") or "").strip() or None,
            "review_summary": list(plan.get("review_summary", []) or []),
            "review_feedback": str(plan.get("review_feedback") or "").strip() or None,
            "pr_url": str(plan.get("pr_url") or "").strip() or None,
            "summary": list(plan.get("summary", []) or []),
        }
    return dict(plan)


def load_checkpoint(
    session: Session,
    *,
    checkpoint_id: str | None = None,
    run_id: str | None = None,
    checkpoint_kind: str | None = None,
) -> WorkflowCheckpoint | None:
    normalized_checkpoint_id = str(checkpoint_id or "").strip()
    if normalized_checkpoint_id:
        return session.get(WorkflowCheckpoint, normalized_checkpoint_id)
    normalized_run_id = str(run_id or "").strip()
    normalized_kind = str(checkpoint_kind or "").strip().lower()
    if not normalized_run_id or not normalized_kind:
        return None
    return session.execute(
        select(WorkflowCheckpoint).where(
            WorkflowCheckpoint.run_id == normalized_run_id,
            WorkflowCheckpoint.checkpoint_kind == normalized_kind,
        )
    ).scalar_one_or_none()


def upsert_workflow_checkpoint(
    session: Session,
    *,
    workflow_id: str,
    run_id: str,
    checkpoint_kind: str,
    stage: str | None,
    payload: dict | None,
    codex_session_id: str | None = None,
    now: datetime | None = None,
) -> WorkflowCheckpoint:
    normalized_workflow_id = str(workflow_id or "").strip()
    normalized_run_id = str(run_id or "").strip()
    normalized_kind = str(checkpoint_kind or "").strip().lower()
    if not normalized_workflow_id or not normalized_run_id or not normalized_kind:
        raise ValueError("workflow_id, run_id, and checkpoint_kind are required")
    timestamp = now or datetime.now(timezone.utc)
    checkpoint = load_checkpoint(
        session,
        run_id=normalized_run_id,
        checkpoint_kind=normalized_kind,
    )
    if checkpoint is None:
        checkpoint = WorkflowCheckpoint(
            checkpoint_id=checkpoint_id_for(run_id=normalized_run_id, checkpoint_kind=normalized_kind),
            workflow_id=normalized_workflow_id,
            run_id=normalized_run_id,
            checkpoint_kind=normalized_kind,
            stage=normalize_checkpoint_stage(stage),
            payload_json=dict(payload or {}),
            codex_session_id=str(codex_session_id or "").strip() or None,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(checkpoint)
    else:
        checkpoint.stage = normalize_checkpoint_stage(stage) or checkpoint.stage
        checkpoint.payload_json = dict(payload or checkpoint.payload_json or {})
        if str(codex_session_id or "").strip():
            checkpoint.codex_session_id = str(codex_session_id).strip()
        checkpoint.updated_at = timestamp
    workflow = session.get(WorkflowExecution, normalized_workflow_id)
    if workflow is not None:
        workflow.latest_checkpoint_id = checkpoint.checkpoint_id
        workflow.updated_at = timestamp
    return checkpoint


def snapshot_checkpoint_for_run(
    session: Session,
    *,
    run,
    stage: str | None,
    checkpoint_kind: str | None,
    payload: dict | None,
) -> WorkflowCheckpoint:
    normalized_kind = str(checkpoint_kind or "").strip().lower()
    if not normalized_kind:
        raise ValueError("checkpoint_kind is required")
    workflow_id = str(getattr(run, "workflow_id", "") or "").strip()
    run_id = str(getattr(run, "run_id", "") or "").strip()
    if not workflow_id or not run_id:
        raise ValueError("run.workflow_id and run.run_id are required")
    return upsert_workflow_checkpoint(
        session,
        workflow_id=workflow_id,
        run_id=run_id,
        checkpoint_kind=normalized_kind,
        stage=stage,
        payload=payload,
    )
