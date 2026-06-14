from __future__ import annotations

from dataclasses import asdict

from orchestrator.core.worker.capability_normalization import KNOWN_WORKER_CAPABILITIES
from orchestrator.core.workflow.runner import DemoRequirement
from orchestrator.core.workflow.runner import DevResult
from orchestrator.core.workflow.runner import PmPlan
from orchestrator.core.workflow.runner import QaRecording
from orchestrator.core.workflow.runner import QaResult
from orchestrator.core.workflow.runner import QaScenario
from orchestrator.core.workflow.runner import QaStep
from orchestrator.core.workflow.runner import ReviewResult
from orchestrator.core.workflow.runner import StageOutcome
from orchestrator.core.workflow.runner import TestResult
from orchestrator.core.workflow.runner import WorkflowStageCheckpoint

_VALID_STAGE_OUTCOMES: set[str] = {"continue", "requeue", "waiting_for_input", "blocked", "failed"}
_VALID_PM_NEXT_STAGES: set[str] = {"dev", "test"}
_VALID_TEST_VALIDATION_SCOPES: set[str] = {"targeted_only", "current_head_acceptance", "full_suite"}
_VALID_QA_CAPTURE_TARGETS: set[str] = {"browser", "ios", "android", "desktop"}


def encode_stage_checkpoint_artifact(checkpoint: WorkflowStageCheckpoint) -> dict | None:
    if checkpoint.plan is not None:
        return encode_pm_plan(checkpoint.plan)
    if checkpoint.dev_result is not None:
        return encode_dev_result(checkpoint.dev_result)
    if checkpoint.test_result is not None:
        return encode_test_result(checkpoint.test_result)
    if checkpoint.review_result is not None:
        return encode_review_result(checkpoint.review_result)
    if checkpoint.qa_result is not None:
        return encode_qa_result(checkpoint.qa_result)
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
    demo_requirements = _require_demo_requirements(
        payload.get("demo_requirements"),
        field="PM plan demo_requirements",
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
    payload["demo_requirements"] = demo_requirements
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
    validation_scope = _parse_test_validation_scope(payload.get("validation_scope"))
    if validation_scope is None:
        raise ValueError("Test result validation_scope is required")
    payload["guidance"] = _require_non_empty_string_list(
        payload.get("guidance"),
        field="Test result guidance",
    )
    payload["validation_scope"] = validation_scope
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


def encode_qa_result(result: QaResult) -> dict:
    payload = asdict(result)
    outcome = _parse_stage_outcome(payload.get("outcome"))
    if outcome is None:
        raise ValueError("QA result outcome is required")
    payload["summary"] = _require_non_empty_string_list(
        payload.get("summary"),
        field="QA result summary",
    )
    if outcome == "continue":
        payload["scenarios"] = _require_qa_scenarios(
            payload.get("scenarios"),
            field="QA result scenarios",
        )
    else:
        payload["scenarios"] = _require_optional_qa_scenarios(
            payload.get("scenarios"),
            field="QA result scenarios",
        )
    payload["recordings"] = _require_qa_recordings(
        payload.get("recordings"),
        field="QA result recordings",
    )
    payload["feedback"] = _parse_optional_string(payload.get("feedback"))
    payload["blocker_message"] = _parse_optional_string(payload.get("blocker_message"))
    if outcome == "blocked" and payload["blocker_message"] is None:
        raise ValueError("Blocked QA result must include blocker_message")
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
    demo_requirements = _parse_demo_requirements(payload.get("demo_requirements"))
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
        or demo_requirements is None
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
        demo_requirements=demo_requirements,
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
    validation_scope = _parse_test_validation_scope(payload.get("validation_scope"))
    if outcome is None:
        return None
    guidance = _parse_string_list(payload.get("guidance"), require_non_empty=True)
    blocker_message = _parse_optional_string(payload.get("blocker_message"))
    if guidance is None or validation_scope is None:
        return None
    if outcome == "blocked" and blocker_message is None:
        return None
    feedback = _parse_optional_string(payload.get("feedback"))
    return TestResult(
        guidance=guidance,
        validation_scope=validation_scope,
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


def decode_qa_result_payload(payload: dict | None) -> QaResult | None:
    if not isinstance(payload, dict):
        return None
    outcome = _parse_stage_outcome(payload.get("outcome"))
    if outcome is None:
        return None
    summary = _parse_string_list(payload.get("summary"), require_non_empty=True)
    scenarios = _parse_qa_scenarios(payload.get("scenarios"))
    recordings = _parse_qa_recordings(payload.get("recordings"))
    blocker_message = _parse_optional_string(payload.get("blocker_message"))
    if summary is None or scenarios is None or recordings is None:
        return None
    if outcome == "blocked" and blocker_message is None:
        return None
    feedback = _parse_optional_string(payload.get("feedback"))
    return QaResult(
        summary=summary,
        scenarios=scenarios,
        recordings=recordings,
        outcome=outcome,
        feedback=feedback,
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


def _parse_test_validation_scope(value: object) -> str | None:
    if isinstance(value, str) and value in _VALID_TEST_VALIDATION_SCOPES:
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


def _parse_demo_requirements(value: object) -> list[DemoRequirement] | None:
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    parsed: list[DemoRequirement] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        title = _parse_optional_string(item.get("title"))
        acceptance_criterion = _parse_optional_string(item.get("acceptance_criterion"))
        capture_target = _parse_optional_string(item.get("capture_target"))
        variants = _parse_string_list(item.get("variants"), require_non_empty=True)
        if title is None or acceptance_criterion is None or capture_target is None or variants is None:
            return None
        if capture_target not in _VALID_QA_CAPTURE_TARGETS:
            return None
        parsed.append(
            DemoRequirement(
                title=title,
                acceptance_criterion=acceptance_criterion,
                capture_target=capture_target,  # type: ignore[arg-type]
                variants=variants,
            )
        )
    return parsed


def _require_demo_requirements(value: object, *, field: str) -> list[dict[str, object]]:
    parsed = _parse_demo_requirements(value)
    if parsed is None:
        raise ValueError(f"{field} must be a demo requirement list")
    return [asdict(item) for item in parsed]


def _parse_qa_steps(value: object) -> list[QaStep] | None:
    if not isinstance(value, list):
        return None
    parsed: list[QaStep] = []
    allowed_actions = {
        "goto",
        "relaunch_app",
        "click",
        "fill",
        "press",
        "select_option",
        "wait_for_text",
        "wait_for_url",
        "assert_text",
        "assert_visible",
    }
    for item in value:
        if not isinstance(item, dict):
            return None
        action = _parse_optional_string(item.get("action"))
        if action is None or action not in allowed_actions:
            return None
        parsed.append(
            QaStep(
                action=action,  # type: ignore[arg-type]
                selector=_parse_optional_string(item.get("selector")),
                value=_parse_optional_string(item.get("value")),
            )
        )
    return parsed


def _parse_qa_scenarios(value: object) -> list[QaScenario] | None:
    if not isinstance(value, list):
        return None
    parsed: list[QaScenario] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        name = _parse_optional_string(item.get("name"))
        objective = _parse_optional_string(item.get("objective"))
        capture_target = _parse_optional_string(item.get("capture_target"))
        start_path = _parse_optional_string(item.get("start_path")) or "/"
        expected_outcomes = _parse_string_list(item.get("expected_outcomes"), require_non_empty=False)
        steps = _parse_qa_steps(item.get("steps"))
        if name is None or objective is None or capture_target is None or expected_outcomes is None or steps is None:
            return None
        if capture_target not in _VALID_QA_CAPTURE_TARGETS:
            return None
        parsed.append(
            QaScenario(
                name=name,
                objective=objective,
                capture_target=capture_target,  # type: ignore[arg-type]
                start_path=start_path,
                expected_outcomes=expected_outcomes,
                steps=steps,
            )
        )
    return parsed


def _require_qa_scenarios(value: object, *, field: str) -> list[dict[str, object]]:
    parsed = _parse_qa_scenarios(value)
    if parsed is None or not parsed:
        raise ValueError(f"{field} must be a non-empty QA scenario list")
    return [asdict(item) for item in parsed]


def _require_optional_qa_scenarios(value: object, *, field: str) -> list[dict[str, object]]:
    parsed = _parse_qa_scenarios(value)
    if parsed is None:
        raise ValueError(f"{field} must be a QA scenario list")
    return [asdict(item) for item in parsed]


def _parse_qa_recordings(value: object) -> list[QaRecording] | None:
    if value is None:
        return []
    if not isinstance(value, list):
        return None
    parsed: list[QaRecording] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        name = _parse_optional_string(item.get("name"))
        artifact_url = _parse_optional_string(item.get("artifact_url"))
        object_key = _parse_optional_string(item.get("object_key"))
        capture_target = _parse_optional_string(item.get("capture_target"))
        capture_reference = _parse_optional_string(item.get("capture_reference"))
        if name is None or artifact_url is None or object_key is None or capture_target is None or capture_reference is None:
            return None
        if capture_target not in _VALID_QA_CAPTURE_TARGETS:
            return None
        parsed.append(
            QaRecording(
                name=name,
                artifact_url=artifact_url,
                object_key=object_key,
                capture_target=capture_target,  # type: ignore[arg-type]
                capture_reference=capture_reference,
            )
        )
    return parsed


def _require_qa_recordings(value: object, *, field: str) -> list[dict[str, object]]:
    parsed = _parse_qa_recordings(value)
    if parsed is None:
        raise ValueError(f"{field} must be a QA recording list")
    return [asdict(item) for item in parsed]
