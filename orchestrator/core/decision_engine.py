from __future__ import annotations

import logging
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.decision_precheck_mapping import (
    apply_frozen_cycle_to_precheck as apply_frozen_cycle_to_precheck_state,
    decision_result_for_duplicate_event as decision_result_for_duplicate_event_state,
    derive_label_actions,
    evaluate_with_labels as evaluate_with_labels_state,
    issue_fingerprint as issue_fingerprint_state,
    normalize_occurred_at as normalize_occurred_at_event,
    resolve_idempotency_key as resolve_idempotency_key_event,
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
from orchestrator.core.decision_planner import plan_decision_questions
from orchestrator.core.decision_state_repository import (
    decision_gate_closed_cycle_id as decision_gate_closed_cycle_id_state,
    decision_gate_closed_permanently as decision_gate_closed_permanently_state,
    existing_case_for_issue as existing_case_for_issue_state,
    persist_decision_state as persist_decision_state_repo,
)
from orchestrator.core.decision_state_machine import (
    DecisionEvent as DecisionLifecycleEvent,
    DecisionState,
    DecisionStateTransition,
    coerce_clear_decision_from_case,
    decision_classification_for_precheck,
    decision_missing_slots_for_precheck,
    decision_from_snapshot as decision_from_snapshot_state,
    ingress_decision_from_precheck,
    ingress_policy_error_decision,
    is_question_driven_state,
    reduce_decision_planner_result,
    resolve_execution_gate_state,
    resolve_decision_state_transition,
)
from orchestrator.core.decision_effect_service import publish_decision_effects as publish_decision_effects_repo
from orchestrator.core.decision_types import (
    DecisionClassification,
    DecisionEngineResult,
    DecisionEventInput,
    DecisionLabelAction,
    DecisionSource,
    IngressDecision,
    PrecheckOutcome,
    WorkerDecision,
    tenant_ready_label,
)
from orchestrator.core.knowledge_base import SlotResolution, resolve_missing_slots_from_knowledge
from orchestrator.core.pre_run_check import (
    evaluate_execution_readiness_only,
    PreRunCheckResult,
    evaluate_pre_run_check,
)
from orchestrator.core.runtime_invocation import invoke_runtime_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.storage.models import (
    DecisionCase,
    DecisionCycle,
    DecisionEffectOutbox,
    DecisionEvent,
    Project,
    Tenant,
)
from orchestrator.tools.project_repo_checkout import project_repo_dir

def _terminally_closed_gate_decision(
    *,
    source: DecisionSource,
    tenant_id: str,
    project_id: str,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    ready_label: str | None,
) -> IngressDecision:
    pre_check = evaluate_execution_readiness_only(
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        ready_label=ready_label,
    )
    return ingress_decision_from_precheck(
        source=source,
        pre_check=pre_check,
        label_actions=derive_label_actions(pre_check),
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
        return ingress_policy_error_decision(
            source=source,
            policy_error=str(exc),
        )

    actions = derive_label_actions(pre_check)
    return ingress_decision_from_precheck(
        source=source,
        pre_check=pre_check,
        label_actions=actions,
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
]


def _build_decision_engine_result(
    *,
    decision: IngressDecision,
    issue_labels: list[str],
    classification: DecisionClassification,
    missing_slots: list[str],
    auto_resolved_slots: list[str],
    case_id: str,
    case_state: str,
    cycle_id: str | None,
    outbox_effect_ids: tuple[str, ...],
    duplicate_event: bool,
) -> DecisionEngineResult:
    execution_gate = resolve_execution_gate_state(
        decision=decision,
        classification=classification,
    )
    return DecisionEngineResult(
        decision=decision,
        issue_labels=issue_labels,
        classification=classification,
        missing_slots=missing_slots,
        auto_resolved_slots=auto_resolved_slots,
        case_id=case_id,
        case_state=case_state,
        cycle_id=cycle_id,
        outbox_effect_ids=outbox_effect_ids,
        duplicate_event=duplicate_event,
        execution_gate=execution_gate,
    )


def _handle_open_cycle_blocked_transition(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    event: DecisionEventInput,
    occurred_at,
    idempotency_key: str,
    existing_case: DecisionCase,
    existing_cycle: DecisionCycle,
    unresolved_question_ids: set[str],
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None,
) -> DecisionEngineResult:
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
        reduced = reduce_decision_planner_result(
            decision=decision,
            planner_result=planner_result,
        )
        decision = reduced.decision
        classification = reduced.classification
        question_set_override = reduced.question_set
        question_reason_override = planner_result.reason
        accepted_ids, _, effect_ids = sync_cycle_answers_from_planner(
            session=session,
            tenant=tenant,
            project=project,
            case=existing_case,
            cycle=existing_cycle,
            planner_question_states=reduced.question_states,
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
        classification=classification.value,
        question_driven=True,
        question_set_override=question_set_override,
        question_reason=question_reason_override,
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
    return _build_decision_engine_result(
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


def _persist_terminal_clear_result(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    event: DecisionEventInput,
    occurred_at,
    idempotency_key: str,
    decision: IngressDecision,
    accepted_question_ids: set[str],
    terminal_gate_closed_cycle_id: str | None = None,
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None,
) -> DecisionEngineResult:
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
        classification=DecisionClassification.CLEAR.value,
        question_driven=False,
        question_set_override=None,
        question_reason=None,
        auto_resolved_answers={},
        accepted_question_ids=accepted_question_ids,
        issue_fingerprint_fn=issue_fingerprint_state,
        terminal_gate_closed_cycle_id=terminal_gate_closed_cycle_id,
    )
    if publish_jira_comment_fn is not None and outbox_effect_ids:
        publish_decision_effects_repo(
            session=session,
            effect_ids=outbox_effect_ids,
            publish_jira_comment_fn=publish_jira_comment_fn,
            occurred_at=occurred_at,
        )
    return _build_decision_engine_result(
        decision=decision,
        issue_labels=issue_labels,
        classification=DecisionClassification.CLEAR,
        missing_slots=[],
        auto_resolved_slots=[],
        case_id=case.case_id,
        case_state=case.state,
        cycle_id=cycle.cycle_id if cycle is not None else None,
        outbox_effect_ids=outbox_effect_ids,
        duplicate_event=False,
    )


def _handle_transition_result_or_none(
    *,
    transition: DecisionStateTransition,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    event: DecisionEventInput,
    occurred_at,
    idempotency_key: str,
    existing_case: DecisionCase | None,
    existing_cycle: DecisionCycle | None,
    unresolved_question_ids: set[str],
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None,
) -> DecisionEngineResult | None:
    if transition == DecisionStateTransition.OPEN_CYCLE_BLOCKED and existing_case is not None and existing_cycle is not None:
        return _handle_open_cycle_blocked_transition(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            event=event,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
            existing_case=existing_case,
            existing_cycle=existing_cycle,
            unresolved_question_ids=unresolved_question_ids,
            publish_jira_comment_fn=publish_jira_comment_fn,
        )

    if transition == DecisionStateTransition.OPEN_CYCLE_CLEAR_AND_CLOSE and existing_case is not None and existing_cycle is not None:
        existing_cycle.status = "resolved"
        existing_cycle.closed_at = occurred_at
        existing_cycle.updated_at = occurred_at
        existing_case.active_cycle_id = None
        existing_case.updated_at = occurred_at
        decision = _terminally_closed_gate_decision(
            source=event.source,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=event.issue_key,
            issue_summary=event.issue_summary,
            issue_description=event.issue_description,
            issue_labels=event.issue_labels,
            ready_label=tenant_ready_label(tenant),
        )
        return _persist_terminal_clear_result(
            session=session,
            tenant=tenant,
            project=project,
            event=event,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
            decision=decision,
            accepted_question_ids=accepted_question_ids_for_cycle(
                session=session,
                cycle_id=existing_cycle.cycle_id,
            ),
            terminal_gate_closed_cycle_id=existing_cycle.cycle_id,
            publish_jira_comment_fn=publish_jira_comment_fn,
        )

    if transition == DecisionStateTransition.TERMINAL_GATE_CLOSED_CLEAR and existing_case is not None:
        decision = _terminally_closed_gate_decision(
            source=event.source,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=event.issue_key,
            issue_summary=event.issue_summary,
            issue_description=event.issue_description,
            issue_labels=event.issue_labels,
            ready_label=tenant_ready_label(tenant),
        )
        return _persist_terminal_clear_result(
            session=session,
            tenant=tenant,
            project=project,
            event=event,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
            decision=decision,
            accepted_question_ids=set(),
            terminal_gate_closed_cycle_id=decision_gate_closed_cycle_id_state(case=existing_case),
            publish_jira_comment_fn=publish_jira_comment_fn,
        )

    if transition == DecisionStateTransition.REUSE_CLEAR_FINGERPRINT and existing_case is not None:
        snapshot = (
            existing_case.metadata_json.get("result_snapshot")
            if isinstance(existing_case.metadata_json, dict)
            and isinstance(existing_case.metadata_json.get("result_snapshot"), dict)
            else {}
        )
        decision = coerce_clear_decision_from_case(
            decision=decision_from_snapshot_state(
                snapshot=snapshot,
                source=event.source,
                classification=DecisionClassification.CLEAR.value,
                cycle=None,
                case=existing_case,
            ),
            case=existing_case,
        )
        return _persist_terminal_clear_result(
            session=session,
            tenant=tenant,
            project=project,
            event=event,
            occurred_at=occurred_at,
            idempotency_key=idempotency_key,
            decision=decision,
            accepted_question_ids=set(),
            terminal_gate_closed_cycle_id=None,
            publish_jira_comment_fn=publish_jira_comment_fn,
        )

    return None


def evaluate_decision_event(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    event: DecisionEventInput,
    settings,  # noqa: ANN001
    tenant_atlassian_oauth_context_fn: Callable[..., Any],
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
    unresolved_question_ids: set[str] = set()
    if existing_cycle is not None:
        unresolved_question_ids = unresolved_question_ids_for_cycle(
            session=session,
            cycle=existing_cycle,
        )
        existing_cycle.unresolved_question_ids_json = list(unresolved_question_ids)
        existing_cycle.updated_at = occurred_at
    transition = resolve_decision_state_transition(
        state=DecisionState(
            has_case=existing_case is not None,
            has_open_cycle=existing_cycle is not None,
            unresolved_question_count=len(unresolved_question_ids),
            decision_gate_closed_permanently=decision_gate_closed_permanently_state(case=existing_case),
            case_classification=DecisionClassification.parse(
                str(getattr(existing_case, "classification", "") or "").strip()
            ),
            case_issue_fingerprint=str(getattr(existing_case, "issue_fingerprint", "") or "").strip(),
            current_issue_fingerprint=current_issue_fingerprint,
        ),
        event=DecisionLifecycleEvent.EVALUATE_INGRESS,
    )

    transition_result = _handle_transition_result_or_none(
        transition=transition,
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        event=event,
        occurred_at=occurred_at,
        idempotency_key=idempotency_key,
        existing_case=existing_case,
        existing_cycle=existing_cycle,
        unresolved_question_ids=unresolved_question_ids,
        publish_jira_comment_fn=publish_jira_comment_fn,
    )
    if transition_result is not None:
        return transition_result

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
        tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context_fn,
        evaluate_ingress_precheck_fn=evaluate_ingress_precheck,
        oauth_context=oauth_context,
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
    )
    decision = initial.decision
    issue_labels = initial.issue_labels
    issue_description = event.issue_description
    missing_slots = decision_missing_slots_for_precheck(decision.pre_check) if decision.pre_check is not None else []
    persisted_slot_answers = slot_resolutions_from_case_resolution(case=existing_case)
    auto_resolved_answers: dict[str, SlotResolution] = {}
    accepted_question_ids = (
        accepted_question_ids_for_cycle(session=session, cycle_id=existing_cycle.cycle_id)
        if existing_cycle is not None
        else set()
    )

    if decision.pre_check is not None and PrecheckOutcome.parse(decision.block_reason) in {
        PrecheckOutcome.DECISION_GATE_REQUIRED,
        PrecheckOutcome.GTD_REQUIRED,
    }:
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
                invoke_runtime_json_fn=invoke_runtime_json,
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
                tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context_fn,
                evaluate_ingress_precheck_fn=evaluate_ingress_precheck,
                oauth_context=oauth_context,
                evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
            )
            decision = reevaluated.decision
            issue_labels = reevaluated.issue_labels
            missing_slots = decision_missing_slots_for_precheck(decision.pre_check) if decision.pre_check is not None else []

    classification = (
        decision_classification_for_precheck(decision.pre_check)
        if decision.pre_check is not None
        else DecisionClassification.CLEAR
    )
    question_set_override = None
    question_reason_override = None
    extra_effect_ids: tuple[str, ...] = ()
    if classification.blocks_execution:
        planner_result = plan_decision_questions(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            issue_key=event.issue_key,
            source=event.source,
            classification=classification.value,
            block_reason=decision.block_reason,
            case=existing_case,
            cycle=existing_cycle,
        )
        if planner_result is not None:
            reduced = reduce_decision_planner_result(
                decision=decision,
                planner_result=planner_result,
            )
            decision = reduced.decision
            classification = reduced.classification
            question_set_override = reduced.question_set
            question_reason_override = planner_result.reason
            if existing_case is not None and existing_cycle is not None:
                accepted_ids, _, extra_effect_ids = sync_cycle_answers_from_planner(
                    session=session,
                    tenant=tenant,
                    project=project,
                    case=existing_case,
                    cycle=existing_cycle,
                    planner_question_states=reduced.question_states,
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
        classification=classification.value,
        question_driven=is_question_driven_state(
            classification=classification,
            block_reason=decision.block_reason,
        ),
        question_set_override=question_set_override,
        question_reason=question_reason_override,
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
            classification=classification.value,
        )
        decision = ingress_decision_from_precheck(
            source=decision.source,
            pre_check=pre_check_with_cycle,
            policy_error=decision.policy_error,
            label_actions=decision.label_actions,
        )

    return _build_decision_engine_result(
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
