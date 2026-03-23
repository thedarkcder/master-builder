from __future__ import annotations

from dataclasses import replace
import logging
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_precheck_mapping import (
    apply_frozen_cycle_to_precheck as apply_frozen_cycle_to_precheck_state,
    decision_from_snapshot as decision_from_snapshot_state,
    decision_result_for_duplicate_event as decision_result_for_duplicate_event_state,
    derive_label_actions,
    normalize_occurred_at as normalize_occurred_at_event,
    resolve_idempotency_key as resolve_idempotency_key_event,
    worker_blocking_gate as worker_blocking_gate_state,
)
from orchestrator.core.decision_evaluation import (
    evaluate_with_labels as evaluate_with_labels_state,
    issue_fingerprint as issue_fingerprint_state,
)
from orchestrator.core.decision_resolution_service import (
    append_auto_resolved_block as append_auto_resolved_block_resolution,
    resolve_slots_before_block as resolve_slots_before_block_resolution,
    resolve_slots_with_codex as resolve_slots_with_codex_resolution,
    slot_resolutions_from_case as slot_resolutions_from_case_resolution,
)
from orchestrator.core.decision_reply_service import (
    active_case_and_cycle_for_issue,
    accepted_question_ids_for_cycle,
    classification_for_cycle_questions,
    latest_recorded_answers_for_issue,
    sync_cycle_answers_from_planner,
    recorded_cycle_answers,
    serialize_recorded_answers_for_policy,
    unresolved_question_ids_for_cycle,
)
from orchestrator.core.decision_planner import DecisionPlannerResult, plan_decision_questions
from orchestrator.core.decision_state_repository import (
    active_cycle as active_cycle_state,
    existing_case_for_issue as existing_case_for_issue_state,
    persist_decision_state as persist_decision_state_repo,
)
from orchestrator.core.decision_effect_service import publish_decision_effects as publish_decision_effects_repo
from orchestrator.core.decision_types import (
    DecisionEngineResult,
    DecisionEventInput,
    DecisionLabelAction,
    DecisionSource,
    IngressDecision,
    WorkerDecision,
    blocking_reason_for_precheck,
)
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.knowledge_base import SlotResolution, resolve_missing_slots_from_knowledge
from orchestrator.core.pre_run_check import PreRunCheckResult, evaluate_pre_run_check
from orchestrator.core.precheck_decision import precheck_classification, precheck_missing_slots
from orchestrator.core.codex_invocation import invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.runs import is_ready_for_agent_precheck, resolve_precheck_outcome_for_enqueue
from orchestrator.storage.models import (
    DecisionCase,
    DecisionCycle,
    DecisionEffectOutbox,
    DecisionEvent,
    Project,
    Tenant,
)
from orchestrator.tools.project_repo_checkout import project_repo_dir

_READY_FOR_AGENT_OVERRIDE_SOURCES = {
    "admin_rerun",
    "cli_run",
    "github_pr_remediation",
}
_PR_REMEDIATION_TRIGGER_SOURCE = "github_pr_review_feedback"
_LEGACY_REMEDIATION_DESCRIPTION_PREFIX = "automated remediation run triggered from github pr #"
_LEGACY_REMEDIATION_SUMMARY_MARKER = ": pr remediation for #"


def _extract_trigger_context_from_plan(plan: object | None) -> dict | None:
    if not isinstance(plan, dict):
        return None
    trigger_context = plan.get("trigger_context")
    return trigger_context if isinstance(trigger_context, dict) else None


def is_pr_remediation_run(
    *,
    run_plan: object | None,
    issue_summary: str | None,
    issue_description: str | None,
) -> bool:
    trigger_context = _extract_trigger_context_from_plan(run_plan)
    if isinstance(trigger_context, dict):
        source = str(trigger_context.get("source") or "").strip().lower()
        if source == _PR_REMEDIATION_TRIGGER_SOURCE:
            return True

    normalized_description = str(issue_description or "").strip().lower()
    if normalized_description.startswith(_LEGACY_REMEDIATION_DESCRIPTION_PREFIX):
        return True

    normalized_summary = str(issue_summary or "").strip().lower()
    return _LEGACY_REMEDIATION_SUMMARY_MARKER in normalized_summary


def _planner_classification(gate_status: str) -> str:
    normalized = str(gate_status or "").strip().lower()
    if normalized == "blocked_decision_gate":
        return "decision_gate"
    if normalized == "blocked_gtd":
        return "gtd"
    if normalized == "blocked_both":
        return "both"
    return "clear"


def _planner_block_reason(classification: str) -> str | None:
    if classification in {"decision_gate", "both"}:
        return "decision_gate_required"
    if classification == "gtd":
        return "gtd_required"
    return None


def _planner_question_set(planner_result: DecisionPlannerResult) -> list[dict[str, object]]:
    open_question_overrides = {
        item.question_id: item
        for item in planner_result.questions
        if str(item.question_id or "").strip()
    }
    states = planner_result.question_states or planner_result.questions
    question_set: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in states:
        if item.question_id in seen:
            continue
        seen.add(item.question_id)
        override = open_question_overrides.get(item.question_id)
        question_set.append(
            {
                "id": item.question_id,
                "kind": override.kind if override is not None else item.kind,
                "text": override.question if override is not None else item.question,
                "status": item.status,
                "detail": override.detail if override is not None else item.detail,
            }
        )
    return question_set


def _planner_question_state_payload(planner_result: DecisionPlannerResult) -> list[dict[str, object]]:
    open_question_overrides = {
        item.question_id: item
        for item in planner_result.questions
        if str(item.question_id or "").strip()
    }
    return [
        {
            "question_id": item.question_id,
            "kind": (
                open_question_overrides[item.question_id].kind
                if item.question_id in open_question_overrides
                else item.kind
            ),
            "question": (
                open_question_overrides[item.question_id].question
                if item.question_id in open_question_overrides
                else item.question
            ),
            "status": item.status,
            "detail": (
                open_question_overrides[item.question_id].detail
                if item.question_id in open_question_overrides
                else item.detail
            ),
        }
        for item in (planner_result.question_states or planner_result.questions)
    ]


def _decision_with_planner_result(
    *,
    decision: IngressDecision,
    planner_result: DecisionPlannerResult,
) -> IngressDecision:
    pre_check = decision.pre_check
    if not isinstance(pre_check, PreRunCheckResult):
        return decision
    classification = _planner_classification(planner_result.gate_status)
    reason = planner_result.reason
    decision_gate_questions = tuple(
        item.question
        for item in planner_result.questions
        if item.kind == "decision_gate"
    )
    gtd_questions = tuple(
        item.question
        for item in planner_result.questions
        if item.kind == "gtd"
    )
    missing_items = tuple(planner_result.missing_items)
    if classification == "clear":
        outcome = "missing_ready_label" if pre_check.ready_label_missing else "ready_for_agent"
        updated_pre_check = replace(
            pre_check,
            outcome=outcome,
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed with execution.",
                tags=(),
            ),
            gtd=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        )
        updated_block_reason = blocking_reason_for_precheck(updated_pre_check)
        return IngressDecision(
            source=decision.source,
            pre_check=updated_pre_check,
            block_reason=updated_block_reason,
            guidance=enqueue_reason_guidance(updated_block_reason) if updated_block_reason else None,
            policy_error=None,
            label_actions=decision.label_actions,
        )

    updated_pre_check = replace(
        pre_check,
        outcome="decision_gate_required" if classification in {"decision_gate", "both"} else "gtd_required",
        decision_gate=DecisionGateResult(
            triggered=classification in {"decision_gate", "both"},
            reason=reason if classification in {"decision_gate", "both"} else "Decision Gate not required",
            missing_sections=missing_items if classification in {"decision_gate", "both"} else (),
            questions=decision_gate_questions,
            recommendation=(
                "Clarification required before execution."
                if classification in {"decision_gate", "both"}
                else pre_check.decision_gate.recommendation
            ),
            tags=pre_check.decision_gate.tags,
        ),
        gtd=GoodToDoValidationResult(
            valid=classification not in {"gtd", "both"},
            missing_criteria=missing_items if classification in {"gtd", "both"} else (),
            clarification_questions=gtd_questions,
        ),
    )
    block_reason = _planner_block_reason(classification)
    return IngressDecision(
        source=decision.source,
        pre_check=updated_pre_check,
        block_reason=block_reason,
        guidance=enqueue_reason_guidance(block_reason) if block_reason else None,
        policy_error=decision.policy_error,
        label_actions=decision.label_actions,
    )


def _clear_planner_result() -> DecisionPlannerResult:
    return DecisionPlannerResult(
        gate_status="clear",
        reason="",
        questions=(),
        question_states=(),
        resolved_items=(),
        missing_items=(),
        captured_answer_summary=None,
    )


def _synthetic_clear_pre_check(*, case: DecisionCase) -> PreRunCheckResult:
    ready_label = str(case.ready_label or "").strip() or None
    ready_label_present = bool(case.ready_label_present)
    outcome = "missing_ready_label" if ready_label and not ready_label_present else "ready_for_agent"
    return PreRunCheckResult(
        outcome=outcome,
        ready_label=ready_label,
        ready_label_present=ready_label_present,
        required_worker_capability=str(case.required_worker_capability or "").strip(),
        required_worker_label=str(case.required_worker_label or "").strip(),
        required_worker_label_present=bool(case.required_worker_label_present),
        decision_gate=DecisionGateResult(
            triggered=False,
            reason="Decision Gate not required",
            missing_sections=(),
            questions=(),
            recommendation="Proceed with execution.",
            tags=(),
        ),
        gtd=GoodToDoValidationResult(
            valid=True,
            missing_criteria=(),
            clarification_questions=(),
        ),
    )


def _coerce_clear_decision(*, decision: IngressDecision, case: DecisionCase) -> IngressDecision:
    normalized_pre_check = (
        decision.pre_check
        if isinstance(decision.pre_check, PreRunCheckResult)
        else _synthetic_clear_pre_check(case=case)
    )
    return _decision_with_planner_result(
        decision=IngressDecision(
            source=decision.source,
            pre_check=normalized_pre_check,
            block_reason=blocking_reason_for_precheck(normalized_pre_check),
            guidance=None,
            policy_error=None,
            label_actions=decision.label_actions,
        ),
        planner_result=_clear_planner_result(),
    )


def evaluate_ingress_precheck(
    *,
    source: DecisionSource,
    tenant_id: str | None,
    project_id: str | None,
    issue_key: str | None,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    ready_label: str | None,
    recorded_answers: list[dict[str, str]] | None = None,
    evaluate_pre_run_check_fn: Callable[..., PreRunCheckResult],
) -> IngressDecision:
    try:
        pre_check = evaluate_pre_run_check_fn(
            tenant_id=tenant_id,
            project_id=project_id,
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            recorded_answers=recorded_answers,
            issue_labels=issue_labels,
            ready_label=ready_label,
        )
    except Exception as exc:  # noqa: BLE001
        return IngressDecision(
            source=source,
            pre_check=None,
            block_reason="policy_eval_failed",
            guidance=enqueue_reason_guidance("policy_eval_failed"),
            policy_error=str(exc),
            label_actions=(),
        )

    actions = derive_label_actions(pre_check)
    block_reason = blocking_reason_for_precheck(pre_check)
    return IngressDecision(
        source=source,
        pre_check=pre_check,
        block_reason=block_reason,
        guidance=enqueue_reason_guidance(block_reason) if block_reason else None,
        policy_error=None,
        label_actions=actions,
    )
def resolve_enqueue_precheck_outcome(
    *,
    source: DecisionSource,
    precheck_outcome: str | None = None,
    precheck_source_plan: object | None = None,
    issue_summary: str | None = None,
    issue_description: str | None = None,
) -> str | None:
    if is_pr_remediation_run(
        run_plan=precheck_source_plan,
        issue_summary=issue_summary,
        issue_description=issue_description,
    ):
        return "ready_for_agent"

    normalized_outcome = resolve_precheck_outcome_for_enqueue(
        precheck_outcome=precheck_outcome,
        precheck_source_plan=precheck_source_plan,
    )
    if normalized_outcome is not None:
        return normalized_outcome
    if source in _READY_FOR_AGENT_OVERRIDE_SOURCES:
        return "ready_for_agent"
    return None


def evaluate_worker_decision(
    *,
    run_plan: object | None,
    tenant_id: str | None,
    project_id: str | None,
    issue_key: str | None,
    run_id: str | None,
    issue_summary: str | None,
    issue_description: str | None,
    session: Session | None = None,
    tenant: Tenant | None = None,
    issue_labels: list[str] | None = None,
    evaluate_pre_run_check_fn: Callable[..., PreRunCheckResult] = evaluate_pre_run_check,
    evaluate_decision_gate_fn: Callable[..., DecisionGateResult] | None = None,
) -> WorkerDecision:
    if is_ready_for_agent_precheck(run_plan):
        return WorkerDecision(allowed=True, decision_gate=None, configuration_error=None)

    if is_pr_remediation_run(
        run_plan=run_plan,
        issue_summary=issue_summary,
        issue_description=issue_description,
    ):
        return WorkerDecision(allowed=True, decision_gate=None, configuration_error=None)

    if session is not None and issue_key:
        existing_case = existing_case_for_issue_state(
            session=session,
            tenant_id=str(tenant_id or ""),
            issue_key=issue_key,
        )
        if existing_case is not None:
            classification = str(existing_case.classification or "").strip() or "clear"
            cycle = active_cycle_state(session=session, case=existing_case)
            snapshot = (
                existing_case.metadata_json.get("result_snapshot")
                if isinstance(existing_case.metadata_json, dict)
                and isinstance(existing_case.metadata_json.get("result_snapshot"), dict)
                else {}
            )
            decision = decision_from_snapshot_state(
                snapshot=snapshot,
                source=str(existing_case.last_source or "worker_execution"),
                classification=classification,
                cycle=cycle,
                case=existing_case,
            )
            if decision.policy_error:
                return WorkerDecision(
                    allowed=False,
                    decision_gate=None,
                    configuration_error=str(decision.policy_error),
                    block_reason=decision.block_reason,
                    classification=classification,
                    pre_check=decision.pre_check if isinstance(decision.pre_check, PreRunCheckResult) else None,
                )
            blocking_gate = worker_blocking_gate_state(
                pre_check=decision.pre_check,
                classification=classification,
                block_reason=decision.block_reason,
            )
            if blocking_gate is not None:
                return WorkerDecision(
                    allowed=False,
                    decision_gate=blocking_gate,
                    configuration_error=None,
                    block_reason=decision.block_reason,
                    classification=classification,
                    pre_check=decision.pre_check if isinstance(decision.pre_check, PreRunCheckResult) else None,
                )
            return WorkerDecision(
                allowed=True,
                decision_gate=None,
                configuration_error=None,
                block_reason=decision.block_reason,
                classification=classification,
                pre_check=decision.pre_check if isinstance(decision.pre_check, PreRunCheckResult) else None,
            )

    if tenant is not None:
        decision = evaluate_ingress_precheck(
            source="worker_execution",
            tenant_id=tenant_id,
            project_id=project_id,
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            recorded_answers=None,
            issue_labels=issue_labels,
            ready_label=(tenant.jira_config or {}).get("ready_label"),
            evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
        )
        classification = precheck_classification(decision.pre_check) if decision.pre_check is not None else "clear"
        if decision.policy_error:
            return WorkerDecision(
                allowed=False,
                decision_gate=None,
                configuration_error=str(decision.policy_error),
                block_reason=decision.block_reason,
                classification=classification,
                pre_check=decision.pre_check if isinstance(decision.pre_check, PreRunCheckResult) else None,
            )
        blocking_gate = worker_blocking_gate_state(
            pre_check=decision.pre_check,
            classification=classification,
            block_reason=decision.block_reason,
        )
        if blocking_gate is not None:
            return WorkerDecision(
                allowed=False,
                decision_gate=blocking_gate,
                configuration_error=None,
                block_reason=decision.block_reason,
                classification=classification,
                pre_check=decision.pre_check if isinstance(decision.pre_check, PreRunCheckResult) else None,
            )
        return WorkerDecision(
            allowed=True,
            decision_gate=None,
            configuration_error=None,
            block_reason=decision.block_reason,
            classification=classification,
            pre_check=decision.pre_check if isinstance(decision.pre_check, PreRunCheckResult) else None,
        )

    if evaluate_decision_gate_fn is None:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Decision Gate configuration error: missing worker decision evaluator",
        )
    try:
        decision_gate = evaluate_decision_gate_fn(
            tenant_id=tenant_id,
            project_id=project_id,
            issue_key=issue_key,
            run_id=run_id,
            issue_summary=issue_summary,
            issue_description=issue_description,
        )
    except (FileNotFoundError, ValueError) as exc:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error=f"Decision Gate configuration error: {exc}",
        )

    if decision_gate.triggered:
        return WorkerDecision(
            allowed=False,
            decision_gate=decision_gate,
            configuration_error=None,
            block_reason="decision_gate_required",
            classification="decision_gate",
        )
    return WorkerDecision(
        allowed=True,
        decision_gate=decision_gate,
        configuration_error=None,
        classification="clear",
    )

logger = logging.getLogger(__name__)

__all__ = [
    "DecisionEngineResult",
    "DecisionEventInput",
    "DecisionLabelAction",
    "DecisionSource",
    "IngressDecision",
    "WorkerDecision",
    "evaluate_decision_event",
    "evaluate_ingress_precheck",
    "evaluate_worker_decision",
    "resolve_enqueue_precheck_outcome",
]


def evaluate_decision_event(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    event: DecisionEventInput,
    settings,  # noqa: ANN001
    tenant_jira_oauth_context_fn: Callable[..., Any],
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None = None,
    oauth_context: Any | None = None,
    evaluate_pre_run_check_fn: Callable[..., object] = evaluate_pre_run_check,
) -> DecisionEngineResult:
    if project is None:
        raise ValueError("Decision evaluation requires a resolved project")
    occurred_at = normalize_occurred_at_event(event.occurred_at)
    idempotency_key = resolve_idempotency_key_event(
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        event=event,
    )
    existing_event = session.execute(
        select(DecisionEvent).where(
            DecisionEvent.tenant_id == tenant.tenant_id,
            DecisionEvent.idempotency_key == idempotency_key,
        )
    ).scalar_one_or_none()
    if existing_event is not None:
        return decision_result_for_duplicate_event_state(
            session=session,
            existing_event=existing_event,
            decision_result_type=DecisionEngineResult,
            decision_case_type=DecisionCase,
            decision_cycle_type=DecisionCycle,
            decision_effect_outbox_type=DecisionEffectOutbox,
        )

    existing_case = existing_case_for_issue_state(
        session=session,
        tenant_id=tenant.tenant_id,
        issue_key=event.issue_key,
    )
    _, existing_cycle = active_case_and_cycle_for_issue(
        session=session,
        tenant_id=tenant.tenant_id,
        issue_key=event.issue_key,
    )
    recorded_answers = (
        recorded_cycle_answers(session=session, cycle_id=existing_cycle.cycle_id)
        if existing_cycle is not None
        else latest_recorded_answers_for_issue(
            session=session,
            tenant_id=tenant.tenant_id,
            issue_key=event.issue_key,
        )
    )
    structured_recorded_answers = serialize_recorded_answers_for_policy(recorded_answers)
    current_issue_fingerprint = issue_fingerprint_state(
        issue_summary=event.issue_summary,
        issue_description=event.issue_description,
        issue_labels=event.issue_labels or [],
    )
    if existing_case is not None and existing_cycle is not None:
        unresolved_question_ids = unresolved_question_ids_for_cycle(
            session=session,
            cycle=existing_cycle,
        )
        existing_cycle.unresolved_question_ids_json = list(unresolved_question_ids)
        existing_cycle.updated_at = occurred_at
        if unresolved_question_ids:
            snapshot_classification = classification_for_cycle_questions(
                cycle=existing_cycle,
                unresolved_question_ids=unresolved_question_ids,
            )
            snapshot = (
                existing_case.metadata_json.get("result_snapshot")
                if isinstance(existing_case.metadata_json, dict)
                and isinstance(existing_case.metadata_json.get("result_snapshot"), dict)
                else {}
            )
            decision = decision_from_snapshot_state(
                snapshot=snapshot,
                source=event.source,
                classification=snapshot_classification,
                cycle=existing_cycle,
                case=existing_case,
            )
            planner_result = plan_decision_questions(
                session=session,
                settings=settings,
                tenant=tenant,
                project=project,
                issue_key=event.issue_key,
                source=event.source,
                classification=snapshot_classification,
                block_reason=decision.block_reason,
                case=existing_case,
                cycle=existing_cycle,
            )
            question_set_override = None
            question_reason_override = None
            classification = snapshot_classification
            accepted_question_ids: set[str] = accepted_question_ids_for_cycle(
                session=session,
                cycle_id=existing_cycle.cycle_id,
            )
            if planner_result is not None:
                decision = _decision_with_planner_result(
                    decision=decision,
                    planner_result=planner_result,
                )
                classification = _planner_classification(planner_result.gate_status)
                question_set_override = _planner_question_set(planner_result)
                question_reason_override = planner_result.reason
                accepted_ids, _, effect_ids = sync_cycle_answers_from_planner(
                    session=session,
                    tenant=tenant,
                    project=project,
                    case=existing_case,
                    cycle=existing_cycle,
                    planner_question_states=_planner_question_state_payload(planner_result),
                    now=occurred_at,
                )
                accepted_question_ids = set(accepted_ids)
                outbox_effect_ids = tuple(effect_ids)
            else:
                outbox_effect_ids = ()
            issue_labels = [
                str(label).strip()
                for label in event.issue_labels or []
                if str(label).strip()
            ]
            issue_description = event.issue_description
            case, cycle, persist_effect_ids = persist_decision_state_repo(
                session=session,
                tenant=tenant,
                project=project,
                event=event,
                occurred_at=occurred_at,
                idempotency_key=idempotency_key,
                issue_labels=issue_labels,
                issue_description=issue_description,
                decision=decision,
                classification=classification,
                question_set_override=question_set_override,
                question_reason_override=question_reason_override,
                auto_resolved_answers={},
                accepted_question_ids=accepted_question_ids,
                issue_fingerprint_fn=issue_fingerprint_state,
            )
            outbox_effect_ids = tuple({*outbox_effect_ids, *persist_effect_ids})
            if publish_jira_comment_fn is not None and outbox_effect_ids:
                publish_decision_effects_repo(
                    session=session,
                    effect_ids=outbox_effect_ids,
                    publish_jira_comment_fn=publish_jira_comment_fn,
                    occurred_at=occurred_at,
                )
            return DecisionEngineResult(
                decision=decision,
                issue_labels=issue_labels,
                classification=classification,
                missing_slots=[],
                auto_resolved_slots=[],
                case_id=case.case_id,
                case_state=case.state,
                cycle_id=cycle.cycle_id if cycle is not None else None,
                outbox_effect_ids=outbox_effect_ids,
                duplicate_event=False,
            )
        existing_cycle.status = "resolved"
        existing_cycle.closed_at = occurred_at
        existing_cycle.updated_at = occurred_at
        existing_case.active_cycle_id = None
        existing_case.updated_at = occurred_at
        snapshot = (
            existing_case.metadata_json.get("result_snapshot")
            if isinstance(existing_case.metadata_json, dict)
            and isinstance(existing_case.metadata_json.get("result_snapshot"), dict)
            else {}
        )
        resolved_decision = _coerce_clear_decision(
            decision=decision_from_snapshot_state(
                snapshot=snapshot,
                source=event.source,
                classification="clear",
                cycle=None,
                case=existing_case,
            ),
            case=existing_case,
        )
        issue_labels = [str(label).strip() for label in event.issue_labels or [] if str(label).strip()]
        issue_description = event.issue_description
        case, cycle, outbox_effect_ids = persist_decision_state_repo(
            session=session,
            tenant=tenant,
            project=project,
            event=event,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
            issue_labels=issue_labels,
            issue_description=issue_description,
            decision=resolved_decision,
            classification="clear",
            question_set_override=None,
            question_reason_override=None,
            auto_resolved_answers={},
            accepted_question_ids=accepted_question_ids_for_cycle(
                session=session,
                cycle_id=existing_cycle.cycle_id,
            ),
            issue_fingerprint_fn=issue_fingerprint_state,
        )
        if publish_jira_comment_fn is not None and outbox_effect_ids:
            publish_decision_effects_repo(
                session=session,
                effect_ids=outbox_effect_ids,
                publish_jira_comment_fn=publish_jira_comment_fn,
                occurred_at=occurred_at,
            )
        return DecisionEngineResult(
            decision=resolved_decision,
            issue_labels=issue_labels,
            classification="clear",
            missing_slots=[],
            auto_resolved_slots=[],
            case_id=case.case_id,
            case_state=case.state,
            cycle_id=None,
            outbox_effect_ids=outbox_effect_ids,
            duplicate_event=False,
        )

    if (
        existing_case is not None
        and existing_cycle is None
        and str(existing_case.classification or "").strip() == "clear"
        and str(existing_case.issue_fingerprint or "").strip() == current_issue_fingerprint
    ):
        snapshot = (
            existing_case.metadata_json.get("result_snapshot")
            if isinstance(existing_case.metadata_json, dict)
            and isinstance(existing_case.metadata_json.get("result_snapshot"), dict)
            else {}
        )
        decision = _coerce_clear_decision(
            decision=decision_from_snapshot_state(
                snapshot=snapshot,
                source=event.source,
                classification="clear",
                cycle=None,
                case=existing_case,
            ),
            case=existing_case,
        )
        issue_labels = [str(label).strip() for label in event.issue_labels or [] if str(label).strip()]
        issue_description = event.issue_description
        case, cycle, outbox_effect_ids = persist_decision_state_repo(
            session=session,
            tenant=tenant,
            project=project,
            event=event,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
            issue_labels=issue_labels,
            issue_description=issue_description,
            decision=decision,
            classification="clear",
            question_set_override=None,
            question_reason_override=None,
            auto_resolved_answers={},
            accepted_question_ids=set(),
            issue_fingerprint_fn=issue_fingerprint_state,
        )
        if publish_jira_comment_fn is not None and outbox_effect_ids:
            publish_decision_effects_repo(
                session=session,
                effect_ids=outbox_effect_ids,
                publish_jira_comment_fn=publish_jira_comment_fn,
                occurred_at=occurred_at,
            )
        return DecisionEngineResult(
            decision=decision,
            issue_labels=issue_labels,
            classification="clear",
            missing_slots=[],
            auto_resolved_slots=[],
            case_id=case.case_id,
            case_state=case.state,
            cycle_id=None,
            outbox_effect_ids=outbox_effect_ids,
            duplicate_event=False,
        )

    initial = evaluate_with_labels_state(
        session=session,
        tenant=tenant,
        project=project,
        source=event.source,
        issue_key=event.issue_key,
        issue_summary=event.issue_summary,
        issue_description=event.issue_description,
        recorded_answers=structured_recorded_answers,
        issue_labels=event.issue_labels,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
        evaluate_ingress_precheck_fn=evaluate_ingress_precheck,
        oauth_context=oauth_context,
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
    )
    decision = initial.decision
    issue_labels = initial.issue_labels
    issue_description = event.issue_description
    missing_slots = precheck_missing_slots(decision.pre_check) if decision.pre_check is not None else []
    persisted_slot_answers = slot_resolutions_from_case_resolution(case=existing_case)
    auto_resolved_answers: dict[str, SlotResolution] = {}
    accepted_question_ids = (
        accepted_question_ids_for_cycle(session=session, cycle_id=existing_cycle.cycle_id)
        if existing_cycle is not None
        else set()
    )

    if decision.pre_check is not None and decision.block_reason in {"decision_gate_required", "gtd_required"}:
        slot_answers = resolve_slots_before_block_resolution(
            session=session,
            tenant=tenant,
            project=project,
            issue_key=event.issue_key,
            issue_summary=event.issue_summary,
            issue_description=issue_description,
            missing_slots=missing_slots,
            settings=settings,
            persisted_slot_answers=persisted_slot_answers,
            resolve_missing_slots_from_knowledge_fn=resolve_missing_slots_from_knowledge,
            resolve_slots_with_codex_fn=lambda **kwargs: resolve_slots_with_codex_resolution(
                **kwargs,
                build_codex_runtime_fn=build_codex_runtime,
                invoke_codex_json_fn=invoke_codex_json,
                project_repo_dir_fn=project_repo_dir,
                codex_runtime_error_type=CodexRuntimeError,
            ),
        )
        if slot_answers:
            auto_resolved_answers = dict(slot_answers)
            issue_description = append_auto_resolved_block_resolution(
                issue_description=issue_description,
                slot_answers=slot_answers,
            )
            reevaluated = evaluate_with_labels_state(
                session=session,
                tenant=tenant,
                project=project,
                source=event.source,
                issue_key=event.issue_key,
                issue_summary=event.issue_summary,
                issue_description=issue_description,
                recorded_answers=structured_recorded_answers,
                issue_labels=issue_labels,
                settings=settings,
                tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
                evaluate_ingress_precheck_fn=evaluate_ingress_precheck,
                oauth_context=oauth_context,
                evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
            )
            decision = reevaluated.decision
            issue_labels = reevaluated.issue_labels
            missing_slots = precheck_missing_slots(decision.pre_check) if decision.pre_check is not None else []

    classification = precheck_classification(decision.pre_check) if decision.pre_check is not None else "clear"
    question_set_override = None
    question_reason_override = None
    extra_effect_ids: tuple[str, ...] = ()
    if classification in {"decision_gate", "gtd", "both"}:
        planner_result = plan_decision_questions(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            issue_key=event.issue_key,
            source=event.source,
            classification=classification,
            block_reason=decision.block_reason,
            case=existing_case,
            cycle=existing_cycle,
        )
        if planner_result is not None:
            decision = _decision_with_planner_result(
                decision=decision,
                planner_result=planner_result,
            )
            classification = _planner_classification(planner_result.gate_status)
            question_set_override = _planner_question_set(planner_result)
            question_reason_override = planner_result.reason
            if existing_case is not None and existing_cycle is not None:
                accepted_ids, _, extra_effect_ids = sync_cycle_answers_from_planner(
                    session=session,
                    tenant=tenant,
                    project=project,
                    case=existing_case,
                    cycle=existing_cycle,
                    planner_question_states=_planner_question_state_payload(planner_result),
                    now=occurred_at,
                )
                accepted_question_ids = set(accepted_ids)

    case, cycle, outbox_effect_ids = persist_decision_state_repo(
        session=session,
        tenant=tenant,
        project=project,
        event=event,
        occurred_at=occurred_at,
        idempotency_key=idempotency_key,
        issue_labels=issue_labels,
        issue_description=issue_description,
        decision=decision,
        classification=classification,
        question_set_override=question_set_override,
        question_reason_override=question_reason_override,
        auto_resolved_answers=auto_resolved_answers,
        accepted_question_ids=accepted_question_ids,
        issue_fingerprint_fn=issue_fingerprint_state,
    )
    outbox_effect_ids = tuple({*extra_effect_ids, *outbox_effect_ids})
    if publish_jira_comment_fn is not None and outbox_effect_ids:
        publish_decision_effects_repo(
            session=session,
            effect_ids=outbox_effect_ids,
            publish_jira_comment_fn=publish_jira_comment_fn,
            occurred_at=occurred_at,
        )

    if decision.pre_check is not None and cycle is not None and cycle.status == "open":
        pre_check_with_cycle = apply_frozen_cycle_to_precheck_state(
            pre_check=decision.pre_check,
            cycle=cycle,
            classification=classification,
        )
        decision = IngressDecision(
            source=decision.source,
            pre_check=pre_check_with_cycle,
            block_reason=decision.block_reason,
            guidance=decision.guidance,
            policy_error=decision.policy_error,
            label_actions=decision.label_actions,
        )

    return DecisionEngineResult(
        decision=decision,
        issue_labels=issue_labels,
        classification=classification,
        missing_slots=missing_slots,
        auto_resolved_slots=sorted(auto_resolved_answers.keys()),
        case_id=case.case_id,
        case_state=case.state,
        cycle_id=cycle.cycle_id if cycle is not None else None,
        outbox_effect_ids=outbox_effect_ids,
        duplicate_event=False,
    )
