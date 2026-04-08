from __future__ import annotations

from dataclasses import asdict

from orchestrator.core.worker_capabilities import normalize_worker_capability
from orchestrator.core.workflow.runner import DevResult
from orchestrator.core.workflow.runner import PmPlan
from orchestrator.core.workflow.runner import ReviewResult
from orchestrator.core.workflow.runner import StageOutcome
from orchestrator.core.workflow.runner import TestResult
from orchestrator.core.workflow.runner import WorkflowStageCheckpoint

_VALID_STAGE_OUTCOMES: set[str] = {"continue", "requeue", "waiting_for_input", "blocked", "failed"}
_VALID_PM_NEXT_STAGES: set[str] = {"dev", "test"}


def encode_stage_checkpoint_artifact(checkpoint: WorkflowStageCheckpoint) -> dict | None:
    if checkpoint.plan is not None:
        return encode_pm_plan(checkpoint.plan)
    if checkpoint.dev_result is not None:
        return encode_dev_result(checkpoint.dev_result)
    if checkpoint.test_result is not None:
        return encode_test_result(checkpoint.test_result)
    if checkpoint.review_result is not None:
        return encode_review_result(checkpoint.review_result)
    return None


def encode_pm_plan(plan: PmPlan) -> dict:
    payload = asdict(plan)
    payload["execution_worker_capability"] = normalize_worker_capability(plan.execution_worker_capability) or "linux"
    if plan.requeue_target is not None:
        payload["requeue_target"] = normalize_worker_capability(plan.requeue_target)
    return payload


def encode_dev_result(result: DevResult) -> dict:
    return asdict(result)


def encode_test_result(result: TestResult) -> dict:
    return asdict(result)


def encode_review_result(result: ReviewResult) -> dict:
    return asdict(result)


def decode_pm_plan_payload(payload: dict | None) -> PmPlan | None:
    if not isinstance(payload, dict):
        return None
    source_payload = payload
    nested_plan = payload.get("plan")
    if isinstance(nested_plan, dict):
        source_payload = nested_plan

    outcome = _normalize_stage_outcome(source_payload.get("outcome"))
    next_stage = _normalize_pm_next_stage(source_payload.get("next_stage"))
    execution_worker_capability = normalize_worker_capability(source_payload.get("execution_worker_capability"))
    if outcome is None or next_stage is None or execution_worker_capability is None:
        return None

    requeue_target = source_payload.get("requeue_target")
    normalized_requeue_target: str | None = None
    if requeue_target is not None:
        normalized_requeue_target = normalize_worker_capability(requeue_target)
        if normalized_requeue_target is None:
            return None

    return PmPlan(
        plan_steps=_normalize_string_list(source_payload.get("plan_steps")),
        acceptance_criteria=_normalize_string_list(source_payload.get("acceptance_criteria")),
        risks=_normalize_string_list(source_payload.get("risks")),
        outcome=outcome,
        next_stage=next_stage,
        execution_worker_capability=execution_worker_capability,
        blocker_message=_normalize_optional_string(source_payload.get("blocker_message")),
        requeue_target=normalized_requeue_target,
        requeue_reason=_normalize_optional_string(source_payload.get("requeue_reason")),
        resolved_prerequisites=_normalize_string_list(source_payload.get("resolved_prerequisites")),
        unresolved_prerequisites=_normalize_string_list(source_payload.get("unresolved_prerequisites")),
    )


def _normalize_stage_outcome(value: object) -> StageOutcome | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in _VALID_STAGE_OUTCOMES:
        return normalized  # type: ignore[return-value]
    return None


def _normalize_pm_next_stage(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in _VALID_PM_NEXT_STAGES:
        return normalized
    return None


def _normalize_string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in (str(entry) for entry in value) if item.strip()]


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None
