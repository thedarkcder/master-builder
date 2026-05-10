from __future__ import annotations

from sqlalchemy import select

from orchestrator.core.workflow.checkpoints import checkpoint_kind_for_stage
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.trigger_context import GithubPrRemediationTriggerContext
from orchestrator.storage.models import WorkflowCheckpoint


def extract_trigger_context(plan: object) -> dict | None:
    if plan is None or (isinstance(plan, dict) and not plan):
        return None
    snapshot = ExecutionSnapshot.load(plan)
    if snapshot is None:
        raise ValueError("Unsupported execution snapshot version/shape")
    trigger_context = snapshot.trigger_context()
    return trigger_context or None


def extract_pr_number(
    *,
    parsed_trigger_context: object,
    trigger_context: dict | None,
) -> int | None:
    if isinstance(parsed_trigger_context, GithubPrRemediationTriggerContext):
        return parsed_trigger_context.pr_number
    if not isinstance(trigger_context, dict):
        return None
    value = trigger_context.get("pr_number")
    if isinstance(value, int) and value > 0:
        return value
    return None


def extract_remediation_head_ref(parsed_trigger_context: object, normalize_branch_fn) -> str | None:
    if not isinstance(parsed_trigger_context, GithubPrRemediationTriggerContext):
        return None
    return normalize_branch_fn(parsed_trigger_context.head_ref)


def extract_remediation_base_ref(parsed_trigger_context: object, normalize_branch_fn) -> str | None:
    if not isinstance(parsed_trigger_context, GithubPrRemediationTriggerContext):
        return None
    return normalize_branch_fn(parsed_trigger_context.base_ref)


def entry_checkpoint(*, session, run) -> WorkflowCheckpoint | None:  # noqa: ANN001
    checkpoint_id = str(getattr(run, "entry_checkpoint_id", "") or "").strip()
    if checkpoint_id:
        checkpoint = session.get(WorkflowCheckpoint, checkpoint_id)
        if checkpoint is not None:
            return checkpoint
    if str(getattr(run, "entry_mode", "fresh") or "fresh").strip().lower() != "resume":
        return None
    entry_stage = str(getattr(run, "entry_stage", "") or "").strip().lower() or None
    if not entry_stage:
        return None
    checkpoint_kind = checkpoint_kind_for_stage(entry_stage)
    return session.execute(
        select(WorkflowCheckpoint)
        .where(
            WorkflowCheckpoint.workflow_id == run.workflow_id,
            WorkflowCheckpoint.checkpoint_kind == checkpoint_kind,
        )
        .order_by(WorkflowCheckpoint.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
