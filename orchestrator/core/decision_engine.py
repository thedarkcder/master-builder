from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
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
from orchestrator.core.decision_resolution_service import (
    append_auto_resolved_block as append_auto_resolved_block_resolution,
    resolve_slots_before_block as resolve_slots_before_block_resolution,
    resolve_slots_with_codex as resolve_slots_with_codex_resolution,
    slot_resolutions_from_case as slot_resolutions_from_case_resolution,
)
from orchestrator.core.decision_state_repository import (
    active_cycle as active_cycle_state,
    existing_case_for_issue as existing_case_for_issue_state,
    persist_decision_state as persist_decision_state_repo,
    publish_decision_effects as publish_decision_effects_repo,
)
from orchestrator.core.decision_types import (
    DecisionEngineResult,
    DecisionEventInput,
    DecisionLabelAction,
    DecisionSource,
    IngressDecision,
    WorkerDecision,
    blocking_reason_for_precheck,
)
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
    evaluate_pre_run_check_fn: Callable[..., PreRunCheckResult],
) -> IngressDecision:
    try:
        pre_check = evaluate_pre_run_check_fn(
            tenant_id=tenant_id,
            project_id=project_id,
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
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
) -> str | None:
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
    project: Project | None,
    event: DecisionEventInput,
    settings,  # noqa: ANN001
    tenant_jira_oauth_context_fn: Callable[..., Any],
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None = None,
    oauth_context: Any | None = None,
    evaluate_pre_run_check_fn: Callable[..., object] = evaluate_pre_run_check,
) -> DecisionEngineResult:
    occurred_at = normalize_occurred_at_event(event.occurred_at)
    idempotency_key = resolve_idempotency_key_event(
        tenant_id=tenant.tenant_id,
        project_id=project.project_id if project is not None else None,
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

    initial = _evaluate_with_labels(
        session=session,
        tenant=tenant,
        project=project,
        source=event.source,
        issue_key=event.issue_key,
        issue_summary=event.issue_summary,
        issue_description=event.issue_description,
        issue_labels=event.issue_labels,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
        oauth_context=oauth_context,
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
    )
    decision = initial.decision
    issue_labels = initial.issue_labels
    issue_description = event.issue_description
    missing_slots = precheck_missing_slots(decision.pre_check) if decision.pre_check is not None else []
    existing_case = existing_case_for_issue_state(
        session=session,
        tenant_id=tenant.tenant_id,
        issue_key=event.issue_key,
    )
    persisted_slot_answers = slot_resolutions_from_case_resolution(case=existing_case)
    auto_resolved_answers: dict[str, SlotResolution] = {}

    if (
        decision.pre_check is not None
        and decision.block_reason in {"decision_gate_required", "gtd_required"}
        and project is not None
    ):
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
            reevaluated = _evaluate_with_labels(
                session=session,
                tenant=tenant,
                project=project,
                source=event.source,
                issue_key=event.issue_key,
                issue_summary=event.issue_summary,
                issue_description=issue_description,
                issue_labels=issue_labels,
                settings=settings,
                tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
                oauth_context=oauth_context,
                evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
            )
            decision = reevaluated.decision
            issue_labels = reevaluated.issue_labels
            missing_slots = precheck_missing_slots(decision.pre_check) if decision.pre_check is not None else []

    classification = precheck_classification(decision.pre_check) if decision.pre_check is not None else "clear"
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
        auto_resolved_answers=auto_resolved_answers,
        issue_fingerprint_fn=_issue_fingerprint,
    )
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


@dataclass(frozen=True)
class _EvaluationResult:
    decision: IngressDecision
    issue_labels: list[str]



def _evaluate_with_labels(
    *,
    session: Session,
    tenant: Tenant,
    project: Project | None,
    source: DecisionSource,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    settings,  # noqa: ANN001
    tenant_jira_oauth_context_fn: Callable[..., Any],
    oauth_context: Any | None = None,
    evaluate_pre_run_check_fn: Callable[..., object] = evaluate_pre_run_check,
) -> _EvaluationResult:
    from orchestrator.core.label_action_service import apply_issue_label_actions

    decision = evaluate_ingress_precheck(
        source=source,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id if project is not None else None,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        ready_label=(tenant.jira_config or {}).get("ready_label"),
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
    )
    normalized_labels = [str(label).strip() for label in (issue_labels or []) if str(label).strip()]
    if decision.pre_check is None:
        return _EvaluationResult(decision=decision, issue_labels=normalized_labels)

    apply_result = apply_issue_label_actions(
        session=session,
        tenant=tenant,
        project_policy_overrides=project.policy_overrides if project is not None else {},
        issue_key=issue_key,
        existing_labels=normalized_labels,
        actions=decision.label_actions,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
        oauth_context=oauth_context,
        logger=logger,
    )
    label_set = {label.casefold() for label in normalized_labels}
    for label in apply_result.applied_labels:
        if label.casefold() in label_set:
            continue
        normalized_labels.append(label)
        label_set.add(label.casefold())
    resolved = decision.with_applied_labels(list(apply_result.applied_labels))
    return _EvaluationResult(decision=resolved, issue_labels=normalized_labels)


def _issue_fingerprint(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str],
) -> str:
    payload = json.dumps(
        {
            "summary": str(issue_summary or "").strip(),
            "description": str(issue_description or "").strip(),
            "labels": sorted({label.casefold() for label in issue_labels}),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
