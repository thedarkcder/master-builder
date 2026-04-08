from __future__ import annotations

from dataclasses import asdict

from orchestrator.core.worker_capability_normalization import KNOWN_WORKER_CAPABILITIES
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
    plan_steps = _require_non_empty_string_list(payload.get("plan_steps"), field="PM plan plan_steps")
    acceptance_criteria = _require_non_empty_string_list(
        payload.get("acceptance_criteria"),
        field="PM plan acceptance_criteria",
    )
    risks = _require_string_list(payload.get("risks"), field="PM plan risks")
    resolved_prerequisites = _require_string_list(
        payload.get("resolved_prerequisites"),
        field="PM plan resolved_prerequisites",
    )
    unresolved_prerequisites = _require_string_list(
        payload.get("unresolved_prerequisites"),
        field="PM plan unresolved_prerequisites",
    )
    outcome = _parse_stage_outcome(payload.get("outcome"))
    if outcome is None:
        raise ValueError("PM plan outcome is required")
    next_stage = _parse_pm_next_stage(payload.get("next_stage"))
    if next_stage is None:
        raise ValueError("PM plan next_stage must be dev or test")
    execution_worker_capability = _parse_worker_capability(plan.execution_worker_capability)
    if execution_worker_capability is None:
        raise ValueError("PM plan execution_worker_capability must be a valid worker capability")
    blocker_message = _parse_optional_string(payload.get("blocker_message"))
    if outcome == "blocked" and blocker_message is None:
        raise ValueError("Blocked PM plan must include blocker_message")
    requeue_reason = _parse_optional_string(payload.get("requeue_reason"))
    requeue_target_raw = payload.get("requeue_target")
    requeue_target = _parse_worker_capability(requeue_target_raw) if requeue_target_raw is not None else None
    if requeue_target_raw is not None and requeue_target is None:
        raise ValueError("PM plan requeue_target must be a valid worker capability when present")
    if outcome != "requeue" and (requeue_target is not None or requeue_reason is not None):
        raise ValueError("PM plan requeue fields are only valid for requeue outcome")
    if outcome == "requeue" and (requeue_target is None or requeue_reason is None):
        raise ValueError("Requeue PM plan must include requeue_target and requeue_reason")
    payload["execution_worker_capability"] = execution_worker_capability
    payload["plan_steps"] = plan_steps
    payload["acceptance_criteria"] = acceptance_criteria
    payload["risks"] = risks
    payload["resolved_prerequisites"] = resolved_prerequisites
    payload["unresolved_prerequisites"] = unresolved_prerequisites
    payload["outcome"] = outcome
    payload["next_stage"] = next_stage
    if requeue_target is not None:
        payload["requeue_target"] = requeue_target
    if requeue_reason is not None:
        payload["requeue_reason"] = requeue_reason
    return payload


def encode_dev_result(result: DevResult) -> dict:
    payload = asdict(result)
    outcome = _parse_stage_outcome(payload.get("outcome"))
    if outcome is None:
        raise ValueError("Dev result outcome is required")
    payload["change_summary"] = _require_non_empty_string_list(
        payload.get("change_summary"),
        field="Dev result change_summary",
    )
    payload["pr_url"] = _parse_optional_string(payload.get("pr_url"))
    payload["blocker_message"] = _parse_optional_string(payload.get("blocker_message"))
    if outcome == "blocked" and payload["blocker_message"] is None:
        raise ValueError("Blocked dev result must include blocker_message")
    payload["outcome"] = outcome
    return payload


def encode_test_result(result: TestResult) -> dict:
    payload = asdict(result)
    outcome = _parse_stage_outcome(payload.get("outcome"))
    if outcome is None:
        raise ValueError("Test result outcome is required")
    payload["guidance"] = _require_non_empty_string_list(
        payload.get("guidance"),
        field="Test result guidance",
    )
    payload["feedback"] = _parse_optional_string(payload.get("feedback"))
    payload["blocker_message"] = _parse_optional_string(payload.get("blocker_message"))
    if outcome == "blocked" and payload["blocker_message"] is None:
        raise ValueError("Blocked test result must include blocker_message")
    payload["outcome"] = outcome
    return payload


def encode_review_result(result: ReviewResult) -> dict:
    payload = asdict(result)
    outcome = _parse_stage_outcome(payload.get("outcome"))
    if outcome is None:
        raise ValueError("Review result outcome is required")
    payload["summary"] = _require_non_empty_string_list(
        payload.get("summary"),
        field="Review result summary",
    )
    payload["feedback"] = _parse_optional_string(payload.get("feedback"))
    payload["pr_url"] = _parse_optional_string(payload.get("pr_url"))
    payload["blocker_message"] = _parse_optional_string(payload.get("blocker_message"))
    if outcome == "blocked" and payload["blocker_message"] is None:
        raise ValueError("Blocked review result must include blocker_message")
    payload["outcome"] = outcome
    return payload


def decode_pm_plan_payload(payload: dict | None) -> PmPlan | None:
    if not isinstance(payload, dict):
        return None
    outcome = _parse_stage_outcome(payload.get("outcome"))
    next_stage = _parse_pm_next_stage(payload.get("next_stage"))
    execution_worker_capability = _parse_worker_capability(payload.get("execution_worker_capability"))
    plan_steps = _parse_string_list(payload.get("plan_steps"), require_non_empty=True)
    acceptance_criteria = _parse_string_list(payload.get("acceptance_criteria"), require_non_empty=True)
    risks = _parse_string_list(payload.get("risks"), require_non_empty=False)
    resolved_prerequisites = _parse_string_list(payload.get("resolved_prerequisites"), require_non_empty=False)
    unresolved_prerequisites = _parse_string_list(payload.get("unresolved_prerequisites"), require_non_empty=False)
    blocker_message = _parse_optional_string(payload.get("blocker_message"))
    requeue_reason = _parse_optional_string(payload.get("requeue_reason"))
    if (
        outcome is None
        or next_stage is None
        or execution_worker_capability is None
        or plan_steps is None
        or acceptance_criteria is None
        or risks is None
        or resolved_prerequisites is None
        or unresolved_prerequisites is None
    ):
        return None

    requeue_target_raw = payload.get("requeue_target")
    requeue_target: str | None = None
    if requeue_target_raw is not None:
        requeue_target = _parse_worker_capability(requeue_target_raw)
        if requeue_target is None:
            return None
    if outcome == "blocked" and blocker_message is None:
        return None
    if outcome == "requeue" and (requeue_target is None or requeue_reason is None):
        return None
    if outcome != "requeue" and (requeue_target is not None or requeue_reason is not None):
        return None

    return PmPlan(
        plan_steps=plan_steps,
        acceptance_criteria=acceptance_criteria,
        risks=risks,
        outcome=outcome,
        next_stage=next_stage,
        execution_worker_capability=execution_worker_capability,
        blocker_message=blocker_message,
        requeue_target=requeue_target,
        requeue_reason=requeue_reason,
        resolved_prerequisites=resolved_prerequisites,
        unresolved_prerequisites=unresolved_prerequisites,
    )


def decode_dev_result_payload(payload: dict | None) -> DevResult | None:
    if not isinstance(payload, dict):
        return None
    outcome = _parse_stage_outcome(payload.get("outcome"))
    if outcome is None:
        return None
    change_summary = _parse_string_list(payload.get("change_summary"), require_non_empty=True)
    blocker_message = _parse_optional_string(payload.get("blocker_message"))
    if change_summary is None:
        return None
    if outcome == "blocked" and blocker_message is None:
        return None
    pr_url = _parse_optional_string(payload.get("pr_url"))
    return DevResult(
        change_summary=change_summary,
        pr_url=pr_url,
        outcome=outcome,
        blocker_message=blocker_message,
    )


def decode_test_result_payload(payload: dict | None) -> TestResult | None:
    if not isinstance(payload, dict):
        return None
    outcome = _parse_stage_outcome(payload.get("outcome"))
    if outcome is None:
        return None
    guidance = _parse_string_list(payload.get("guidance"), require_non_empty=True)
    blocker_message = _parse_optional_string(payload.get("blocker_message"))
    if guidance is None:
        return None
    if outcome == "blocked" and blocker_message is None:
        return None
    feedback = _parse_optional_string(payload.get("feedback"))
    return TestResult(
        guidance=guidance,
        outcome=outcome,
        feedback=feedback,
        blocker_message=blocker_message,
    )


def decode_review_result_payload(payload: dict | None) -> ReviewResult | None:
    if not isinstance(payload, dict):
        return None
    outcome = _parse_stage_outcome(payload.get("outcome"))
    if outcome is None:
        return None
    summary = _parse_string_list(payload.get("summary"), require_non_empty=True)
    blocker_message = _parse_optional_string(payload.get("blocker_message"))
    if summary is None:
        return None
    if outcome == "blocked" and blocker_message is None:
        return None
    feedback = _parse_optional_string(payload.get("feedback"))
    pr_url = _parse_optional_string(payload.get("pr_url"))
    return ReviewResult(
        summary=summary,
        outcome=outcome,
        feedback=feedback,
        pr_url=pr_url,
        blocker_message=blocker_message,
    )


def _parse_stage_outcome(value: object) -> StageOutcome | None:
    if isinstance(value, str) and value in _VALID_STAGE_OUTCOMES:
        return value  # type: ignore[return-value]
    return None


def _parse_pm_next_stage(value: object) -> str | None:
    if isinstance(value, str) and value in _VALID_PM_NEXT_STAGES:
        return value
    return None


def _parse_worker_capability(value: object) -> str | None:
    if isinstance(value, str) and value in KNOWN_WORKER_CAPABILITIES:
        return value
    return None


def _parse_optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    return value


def _require_non_empty_string_list(value: object, *, field: str) -> list[str]:
    parsed = _parse_string_list(value, require_non_empty=True)
    if parsed is None:
        raise ValueError(f"{field} must be a non-empty string list")
    return parsed


def _require_string_list(value: object, *, field: str) -> list[str]:
    parsed = _parse_string_list(value, require_non_empty=False)
    if parsed is None:
        raise ValueError(f"{field} must be a string list")
    return parsed


def _parse_string_list(value: object, *, require_non_empty: bool) -> list[str] | None:
    if not isinstance(value, list):
        return None
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        if not item.strip():
            return None
        parsed.append(item)
    if require_non_empty and not parsed:
        return None
    return parsed
