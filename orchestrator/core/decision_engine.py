from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Callable, Literal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.knowledge_base import SlotResolution, resolve_missing_slots_from_knowledge
from orchestrator.core.pre_run_check import PreRunCheckResult, evaluate_pre_run_check
from orchestrator.core.precheck_decision import precheck_classification, precheck_missing_slots
from orchestrator.core.project_policy import resolve_effective_policy
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

DecisionSource = Literal[
    "jira_webhook",
    "discord_run",
    "discord_retry",
    "discord_reply",
    "admin_rerun",
    "cli_run",
    "github_pr_remediation",
    "worker_execution",
]

_BLOCKING_PRECHECK_OUTCOMES = {
    "decision_gate_required",
    "gtd_required",
    "missing_ready_label",
}

_READY_FOR_AGENT_OVERRIDE_SOURCES = {
    "admin_rerun",
    "cli_run",
    "github_pr_remediation",
}


@dataclass(frozen=True)
class DecisionLabelAction:
    label: str
    action: Literal["add"]
    reason: Literal["ready_label_missing", "required_worker_label_missing"]


@dataclass(frozen=True)
class IngressDecision:
    source: DecisionSource
    pre_check: object | None
    block_reason: str | None
    guidance: str | None
    policy_error: str | None
    label_actions: tuple[DecisionLabelAction, ...]

    def with_applied_labels(self, applied_labels: list[str]) -> IngressDecision:
        if self.pre_check is None or not applied_labels or not isinstance(self.pre_check, PreRunCheckResult):
            return self
        normalized_applied = {str(label).strip().casefold() for label in applied_labels if str(label).strip()}
        if not normalized_applied:
            return self

        updated_pre_check = self.pre_check
        if (
            updated_pre_check.ready_label
            and updated_pre_check.ready_label.casefold() in normalized_applied
            and updated_pre_check.ready_label_missing
        ):
            updated_pre_check = updated_pre_check.with_ready_label_present()
        if (
            updated_pre_check.required_worker_label
            and updated_pre_check.required_worker_label.casefold() in normalized_applied
            and not updated_pre_check.required_worker_label_present
        ):
            updated_pre_check = replace(updated_pre_check, required_worker_label_present=True)

        updated_block_reason = _blocking_reason_for_precheck(updated_pre_check)
        return IngressDecision(
            source=self.source,
            pre_check=updated_pre_check,
            block_reason=updated_block_reason,
            guidance=enqueue_reason_guidance(updated_block_reason) if updated_block_reason else None,
            policy_error=self.policy_error,
            label_actions=self.label_actions,
        )


@dataclass(frozen=True)
class WorkerDecision:
    allowed: bool
    decision_gate: DecisionGateResult | None
    configuration_error: str | None


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
    block_reason = _blocking_reason_for_precheck(pre_check)
    return IngressDecision(
        source=source,
        pre_check=pre_check,
        block_reason=block_reason,
        guidance=enqueue_reason_guidance(block_reason) if block_reason else None,
        policy_error=None,
        label_actions=actions,
    )


def derive_label_actions(pre_check: object) -> tuple[DecisionLabelAction, ...]:
    actions: list[DecisionLabelAction] = []
    ready_label_missing = bool(getattr(pre_check, "ready_label_missing", False))
    ready_label = str(getattr(pre_check, "ready_label", "") or "").strip()
    if ready_label_missing and ready_label:
        actions.append(
            DecisionLabelAction(
                label=ready_label,
                action="add",
                reason="ready_label_missing",
            )
        )
    required_worker_label = str(getattr(pre_check, "required_worker_label", "") or "").strip()
    required_worker_label_present = bool(getattr(pre_check, "required_worker_label_present", True))
    if not required_worker_label_present and required_worker_label:
        actions.append(
            DecisionLabelAction(
                label=required_worker_label,
                action="add",
                reason="required_worker_label_missing",
            )
        )
    deduped: list[DecisionLabelAction] = []
    seen: set[str] = set()
    for action in actions:
        key = action.label.casefold()
        if key in seen:
            continue
        deduped.append(action)
        seen.add(key)
    return tuple(deduped)


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
    evaluate_decision_gate_fn: Callable[..., DecisionGateResult],
) -> WorkerDecision:
    if is_ready_for_agent_precheck(run_plan):
        return WorkerDecision(allowed=True, decision_gate=None, configuration_error=None)
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
        return WorkerDecision(allowed=False, decision_gate=decision_gate, configuration_error=None)
    return WorkerDecision(allowed=True, decision_gate=decision_gate, configuration_error=None)


def _blocking_reason_for_precheck(pre_check: object) -> str | None:
    outcome = str(getattr(pre_check, "outcome", "") or "").strip()
    if outcome in _BLOCKING_PRECHECK_OUTCOMES:
        return outcome
    return None
logger = logging.getLogger(__name__)

_QUESTION_KIND_DG = "decision_gate"
_QUESTION_KIND_GTD = "gtd"
_BLOCKED_CLASSIFICATIONS = {"decision_gate", "gtd", "both"}


@dataclass(frozen=True)
class DecisionEventInput:
    source: DecisionSource
    event_type: str
    idempotency_key: str | None
    issue_key: str
    issue_summary: str | None
    issue_description: str | None
    issue_labels: list[str] | None
    occurred_at: datetime | None = None


@dataclass(frozen=True)
class DecisionEngineResult:
    decision: IngressDecision
    issue_labels: list[str]
    classification: str
    missing_slots: list[str]
    auto_resolved_slots: list[str]
    case_id: str
    case_state: str
    cycle_id: str | None
    outbox_effect_ids: tuple[str, ...]
    duplicate_event: bool


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
    occurred_at = _normalize_occurred_at(event.occurred_at)
    idempotency_key = _resolve_idempotency_key(
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
    duplicate_event = existing_event is not None

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
    auto_resolved_slots: list[str] = []

    if (
        decision.pre_check is not None
        and decision.block_reason in {"decision_gate_required", "gtd_required"}
        and project is not None
        and not duplicate_event
    ):
        slot_answers = _resolve_slots_before_block(
            session=session,
            tenant=tenant,
            project=project,
            issue_key=event.issue_key,
            issue_summary=event.issue_summary,
            issue_description=issue_description,
            missing_slots=missing_slots,
            settings=settings,
        )
        if slot_answers:
            auto_resolved_slots = sorted(slot_answers.keys())
            issue_description = _append_auto_resolved_block(
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
    case, cycle, outbox_effect_ids = _persist_decision_state(
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
        auto_resolved_slots=auto_resolved_slots,
        duplicate_event=duplicate_event,
        publish_jira_comment_fn=publish_jira_comment_fn,
    )

    if decision.pre_check is not None and cycle is not None and cycle.status == "open":
        pre_check_with_cycle = _apply_frozen_cycle_to_precheck(
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
        auto_resolved_slots=auto_resolved_slots,
        case_id=case.case_id,
        case_state=case.state,
        cycle_id=cycle.cycle_id if cycle is not None else None,
        outbox_effect_ids=outbox_effect_ids,
        duplicate_event=duplicate_event,
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


def _resolve_slots_before_block(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    missing_slots: list[str],
    settings,  # noqa: ANN001
) -> dict[str, SlotResolution]:
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant.policy_config or {},
        project_overrides=project.policy_overrides or {},
    )
    knowledge_enabled = bool(effective_policy.get("knowledge_base_enabled", True))
    mode = str(effective_policy.get("knowledge_auto_answer_mode") or "").strip().lower()
    if mode not in {"safe", "balanced", "aggressive"}:
        mode = "aggressive"

    resolved: dict[str, SlotResolution] = {}
    if knowledge_enabled and missing_slots:
        resolved = resolve_missing_slots_from_knowledge(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            missing_slots=missing_slots,
            mode=mode,
        )

    remaining = [slot for slot in missing_slots if slot not in resolved]
    if not remaining:
        return resolved

    codex_answers = _resolve_slots_with_codex(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        missing_slots=remaining,
    )
    merged = dict(resolved)
    for slot_name, answer in codex_answers.items():
        if slot_name in merged:
            continue
        merged[slot_name] = answer
    return merged


def _resolve_slots_with_codex(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    missing_slots: list[str],
) -> dict[str, SlotResolution]:
    runtime = build_codex_runtime(session=session, settings=settings)
    repo_evidence = _collect_repo_evidence(
        base_dir=str(getattr(settings, "project_repo_checkout_base_dir", "") or ""),
        tenant_id=tenant.tenant_id,
        project=project,
        missing_slots=missing_slots,
    )
    try:
        payload = invoke_codex_json(
            runtime=runtime,
            context=CodexInvocationContext(
                channel="system",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                command="policy",
                stage="decision_resolution",
                working_dir=".",
                issue_key=issue_key,
                reasoning_effort="low",
                issue_description_chars=len(str(issue_description or "")),
            ),
            system_prompt=(
                "You resolve missing decision slots from issue/Jira context and repository evidence. "
                "Return strict JSON object only: {\"answers\": {\"<slot_name>\": \"<value>\"}}. "
                "Only include slots with explicit evidence."
            ),
            user_prompt="\n".join(
                [
                    f"Issue key: {issue_key}",
                    f"Issue summary: {str(issue_summary or '').strip()}",
                    "Issue description:",
                    str(issue_description or "").strip(),
                    "",
                    f"Missing slots: {json.dumps(missing_slots)}",
                    "",
                    "Repository evidence:",
                    repo_evidence or "(none)",
                ]
            ),
        )
    except CodexRuntimeError as exc:
        logger.warning(
            "decision_resolution_codex_failed tenant_id=%s project_id=%s issue_key=%s error=%s",
            tenant.tenant_id,
            project.project_id,
            issue_key,
            exc,
        )
        return {}

    answers_raw = payload.get("answers") if isinstance(payload, dict) else None
    if not isinstance(answers_raw, dict):
        return {}

    resolved: dict[str, SlotResolution] = {}
    for slot_name in missing_slots:
        value = str(answers_raw.get(slot_name) or "").strip()
        if not value:
            continue
        resolved[slot_name] = SlotResolution(
            slot_name=slot_name,
            slot_value=value,
            source_timestamp=None,
            confidence=0.66,
            citation={
                "source_type": "codex_inference",
                "title": "Decision resolution inference",
                "source_timestamp": None,
            },
            inferred=True,
        )
    return resolved


def _collect_repo_evidence(
    *,
    base_dir: str,
    tenant_id: str,
    project: Project,
    missing_slots: list[str],
) -> str:
    if not base_dir.strip():
        return ""
    repo_dir = project_repo_dir(base_dir=base_dir, tenant_id=tenant_id, project_id=project.project_id)
    if not (repo_dir / ".git").exists():
        return ""
    slot_tokens: dict[str, tuple[str, ...]] = {
        "objective": ("objective", "goal"),
        "scope": ("scope", "in scope"),
        "acceptance_criteria": ("acceptance", "done when"),
        "how_to_test": ("how to test", "test"),
        "nfr_intent": ("mvp", "scale-ready", "nfr"),
        "reliability_security_constraints": ("security", "reliability", "token", "session"),
        "out_of_scope": ("out of scope", "non-goal"),
        "rollout_constraints": ("rollout", "migration"),
        "decision_owner": ("owner", "approver"),
        "dependencies_and_risks": ("risk", "dependency"),
    }
    candidate_files: list[Path] = []
    for pattern in ("README*", "docs/**/*.md", "**/*architecture*.md", "**/*decision*.md"):
        candidate_files.extend(repo_dir.glob(pattern))
    lines: list[str] = []
    seen_paths: set[str] = set()
    for path in sorted(candidate_files):
        if len(lines) >= 20:
            break
        if not path.is_file():
            continue
        path_key = str(path)
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        content_lower = content.lower()
        for slot_name in missing_slots:
            terms = slot_tokens.get(slot_name, ())
            if not terms:
                continue
            if not any(term in content_lower for term in terms):
                continue
            snippet = _extract_first_matching_line(content=content, terms=terms)
            if snippet:
                rel_path = str(path.relative_to(repo_dir))
                lines.append(f"- {slot_name} [{rel_path}]: {snippet}")
                break
    return "\n".join(lines)


def _extract_first_matching_line(*, content: str, terms: tuple[str, ...]) -> str | None:
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        lower = line.lower()
        if not any(term in lower for term in terms):
            continue
        if len(line) > 240:
            return f"{line[:237].rstrip()}..."
        return line
    return None


def _append_auto_resolved_block(*, issue_description: str | None, slot_answers: dict[str, SlotResolution]) -> str:
    lines = ["", "Auto-resolved context for precheck:"]
    for slot_name in sorted(slot_answers.keys()):
        answer = slot_answers[slot_name]
        citation_title = str(answer.citation.get("title") or answer.citation.get("source_type") or "").strip()
        if citation_title:
            lines.append(f"- {slot_name}: {answer.slot_value} (source: {citation_title})")
        else:
            lines.append(f"- {slot_name}: {answer.slot_value}")
    base = str(issue_description or "").strip()
    extra = "\n".join(lines).strip()
    if not base:
        return extra
    return f"{base}\n\n{extra}"


def _persist_decision_state(
    *,
    session: Session,
    tenant: Tenant,
    project: Project | None,
    event: DecisionEventInput,
    occurred_at: datetime,
    idempotency_key: str,
    issue_labels: list[str],
    issue_description: str | None,
    decision: IngressDecision,
    classification: str,
    auto_resolved_slots: list[str],
    duplicate_event: bool,
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None,
) -> tuple[DecisionCase, DecisionCycle | None, tuple[str, ...]]:
    case = _load_or_create_case(
        session=session,
        tenant=tenant,
        project=project,
        issue_key=event.issue_key,
        now=occurred_at,
    )
    pre_check = decision.pre_check
    case_state = _case_state_for_decision(decision=decision)
    question_driven = classification in _BLOCKED_CLASSIFICATIONS and decision.block_reason in {
        "decision_gate_required",
        "gtd_required",
    }
    question_set = _build_question_set(pre_check=pre_check, classification=classification) if question_driven else []
    reason = _decision_reason(pre_check=pre_check, classification=classification) if question_driven else None

    active_cycle = _active_cycle(session=session, case=case)
    cycle: DecisionCycle | None = None
    if question_driven:
        if (
            active_cycle is not None
            and active_cycle.status == "open"
            and str(active_cycle.classification or "") == classification
        ):
            cycle = active_cycle
        else:
            if active_cycle is not None and active_cycle.status == "open":
                active_cycle.status = "closed"
                active_cycle.closed_at = occurred_at
                active_cycle.updated_at = occurred_at
            cycle = DecisionCycle(
                cycle_id=uuid4().hex,
                case_id=case.case_id,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id if project is not None else None,
                issue_key=event.issue_key,
                status="open",
                reason=reason,
                classification=classification,
                question_set_json=question_set,
                unresolved_question_ids_json=[str(item.get("id") or "") for item in question_set if str(item.get("id") or "").strip()],
                metadata_json={},
                opened_at=occurred_at,
                closed_at=None,
                created_at=occurred_at,
                updated_at=occurred_at,
            )
            session.add(cycle)
            case.active_cycle_id = cycle.cycle_id
    else:
        if active_cycle is not None and active_cycle.status == "open":
            active_cycle.status = "closed"
            active_cycle.closed_at = occurred_at
            active_cycle.updated_at = occurred_at
        case.active_cycle_id = None

    case.project_id = project.project_id if project is not None else case.project_id
    case.state = case_state
    case.blocked_reason = decision.block_reason
    case.classification = classification
    case.issue_fingerprint = _issue_fingerprint(
        issue_summary=event.issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
    )
    case.last_source = event.source
    case.last_event_type = str(event.event_type or "").strip() or None
    case.last_event_at = occurred_at
    case.required_worker_capability = (
        str(getattr(pre_check, "required_worker_capability", "") or "").strip() or None
        if pre_check is not None
        else None
    )
    case.required_worker_label = (
        str(getattr(pre_check, "required_worker_label", "") or "").strip() or None
        if pre_check is not None
        else None
    )
    case.ready_label = (
        str(getattr(pre_check, "ready_label", "") or "").strip() or None
        if pre_check is not None
        else None
    )
    case.ready_label_present = bool(getattr(pre_check, "ready_label_present", False)) if pre_check is not None else False
    case.metadata_json = {
        "auto_resolved_slots": auto_resolved_slots,
        "duplicate_event": duplicate_event,
    }
    case.updated_at = occurred_at

    outbox_effect_ids: list[str] = []
    if not duplicate_event:
        event_row = DecisionEvent(
            event_id=uuid4().hex,
            idempotency_key=idempotency_key,
            case_id=case.case_id,
            cycle_id=cycle.cycle_id if cycle is not None else None,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id if project is not None else None,
            issue_key=event.issue_key,
            source=event.source,
            event_type=str(event.event_type or "").strip() or "unspecified",
            payload_json={
                "issue_summary": str(event.issue_summary or "").strip(),
                "issue_labels": issue_labels,
            },
            outcome_state=case_state,
            created_at=occurred_at,
        )
        session.add(event_row)

        if cycle is not None and cycle.status == "open":
            effect = _enqueue_or_get_effect(
                session=session,
                case=case,
                cycle=cycle,
                effect_type="jira_comment",
                dedupe_key=f"jira-comment:{tenant.tenant_id}:{event.issue_key}:{cycle.cycle_id}",
                payload={
                    "comment": _build_cycle_comment(case=case, cycle=cycle),
                },
                now=occurred_at,
            )
            outbox_effect_ids.append(effect.effect_id)
            if publish_jira_comment_fn is not None and effect.status == "pending":
                posted, error = publish_jira_comment_fn(str(effect.payload_json.get("comment") or ""))
                if posted:
                    effect.status = "sent"
                    effect.sent_at = occurred_at
                    effect.updated_at = occurred_at
                else:
                    effect.status = "failed"
                    effect.attempt_count += 1
                    effect.last_error = error
                    effect.updated_at = occurred_at

    session.commit()
    session.refresh(case)
    if cycle is not None:
        session.refresh(cycle)
    return case, cycle, tuple(outbox_effect_ids)


def _enqueue_or_get_effect(
    *,
    session: Session,
    case: DecisionCase,
    cycle: DecisionCycle,
    effect_type: str,
    dedupe_key: str,
    payload: dict[str, object],
    now: datetime,
) -> DecisionEffectOutbox:
    existing = session.execute(
        select(DecisionEffectOutbox).where(
            DecisionEffectOutbox.tenant_id == case.tenant_id,
            DecisionEffectOutbox.dedupe_key == dedupe_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    effect = DecisionEffectOutbox(
        effect_id=uuid4().hex,
        dedupe_key=dedupe_key,
        case_id=case.case_id,
        cycle_id=cycle.cycle_id,
        tenant_id=case.tenant_id,
        project_id=case.project_id,
        issue_key=case.issue_key,
        effect_type=effect_type,
        payload_json=payload,
        status="pending",
        attempt_count=0,
        next_attempt_at=None,
        sent_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(effect)
    return effect


def _active_cycle(*, session: Session, case: DecisionCase) -> DecisionCycle | None:
    if not case.active_cycle_id:
        return None
    cycle = session.get(DecisionCycle, case.active_cycle_id)
    if cycle is None:
        return None
    if cycle.status != "open":
        return None
    return cycle


def _load_or_create_case(
    *,
    session: Session,
    tenant: Tenant,
    project: Project | None,
    issue_key: str,
    now: datetime,
) -> DecisionCase:
    case = session.execute(
        select(DecisionCase).where(
            DecisionCase.tenant_id == tenant.tenant_id,
            DecisionCase.issue_key == issue_key,
        )
    ).scalar_one_or_none()
    if case is not None:
        return case
    case = DecisionCase(
        case_id=uuid4().hex,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id if project is not None else None,
        issue_key=issue_key,
        state="clear",
        blocked_reason=None,
        classification=None,
        issue_fingerprint=None,
        active_cycle_id=None,
        last_source=None,
        last_event_type=None,
        last_event_at=None,
        required_worker_capability=None,
        required_worker_label=None,
        ready_label=None,
        ready_label_present=False,
        metadata_json={},
        created_at=now,
        updated_at=now,
    )
    session.add(case)
    session.flush()
    return case


def _build_cycle_comment(*, case: DecisionCase, cycle: DecisionCycle) -> str:
    lines = [
        f"<!-- decision-cycle:{cycle.cycle_id} -->",
        f"Decision state: `{case.state}`",
    ]
    if cycle.reason:
        lines.append(f"Reason: {cycle.reason}")
    if cycle.question_set_json:
        lines.append("Outstanding questions:")
        for item in cycle.question_set_json:
            question_id = str(item.get("id") or "").strip()
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            if question_id:
                lines.append(f"- [{question_id}] {text}")
            else:
                lines.append(f"- {text}")
    return "\n".join(lines)


def _apply_frozen_cycle_to_precheck(*, pre_check: object, cycle: DecisionCycle, classification: str) -> object:
    decision_gate_questions = [
        str(item.get("text") or "").strip()
        for item in cycle.question_set_json
        if str(item.get("kind") or "").strip() == _QUESTION_KIND_DG and str(item.get("text") or "").strip()
    ]
    gtd_questions = [
        str(item.get("text") or "").strip()
        for item in cycle.question_set_json
        if str(item.get("kind") or "").strip() == _QUESTION_KIND_GTD and str(item.get("text") or "").strip()
    ]
    resolved = pre_check
    decision_gate = getattr(resolved, "decision_gate", None)
    gtd = getattr(resolved, "gtd", None)
    if classification in {"decision_gate", "both"} and decision_gate is not None:
        next_decision_gate = decision_gate
        if cycle.reason:
            next_decision_gate = replace(next_decision_gate, reason=cycle.reason)
        if decision_gate_questions:
            next_decision_gate = replace(next_decision_gate, questions=tuple(decision_gate_questions))
        resolved = replace(resolved, decision_gate=next_decision_gate)
    if classification in {"gtd", "both"} and gtd is not None and gtd_questions:
        resolved = replace(resolved, gtd=replace(gtd, clarification_questions=tuple(gtd_questions)))
    return resolved


def _build_question_set(*, pre_check: object, classification: str) -> list[dict]:
    if pre_check is None:
        return []
    items: list[dict] = []
    if classification in {"decision_gate", "both"}:
        decision_gate = getattr(pre_check, "decision_gate", None)
        questions = getattr(decision_gate, "questions", ()) if decision_gate is not None else ()
        for question in questions:
            text = str(question or "").strip()
            if not text:
                continue
            items.append(
                {
                    "id": f"dg_{_stable_short_hash(text)}",
                    "kind": _QUESTION_KIND_DG,
                    "text": text,
                }
            )
    if classification in {"gtd", "both"}:
        for question in getattr(pre_check, "gtd_clarification_questions", ()):
            text = str(question or "").strip()
            if not text:
                continue
            items.append(
                {
                    "id": f"gtd_{_stable_short_hash(text)}",
                    "kind": _QUESTION_KIND_GTD,
                    "text": text,
                }
            )
    deduped: list[dict] = []
    seen: set[str] = set()
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if not item_id or item_id in seen:
            continue
        deduped.append(item)
        seen.add(item_id)
    return deduped


def _question_signature(question_set: list[dict]) -> str:
    payload = json.dumps(question_set, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stable_short_hash(value: str) -> str:
    return hashlib.sha1(str(value).encode("utf-8")).hexdigest()[:10]


def _case_state_for_decision(*, decision: IngressDecision) -> str:
    pre_check = decision.pre_check
    if decision.block_reason == "decision_gate_required":
        return "blocked_decision_gate"
    if decision.block_reason == "gtd_required":
        return "blocked_gtd"
    if pre_check is not None and str(getattr(pre_check, "outcome", "") or "").strip() == "ready_for_agent":
        return "ready_for_execution"
    return "clear"


def _decision_reason(*, pre_check: object, classification: str) -> str | None:
    if pre_check is None:
        return None
    if classification in {"decision_gate", "both"}:
        decision_gate = getattr(pre_check, "decision_gate", None)
        reason = str(getattr(decision_gate, "reason", "") or "").strip() if decision_gate is not None else ""
        if reason:
            return reason
    if classification in {"gtd", "both"}:
        missing = [
            str(item).strip()
            for item in getattr(pre_check, "gtd_missing_criteria", ())
            if str(item).strip()
        ]
        if missing:
            return "Missing GTD criteria: " + ", ".join(missing)
    return None


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


def _resolve_idempotency_key(*, tenant_id: str, project_id: str | None, event: DecisionEventInput) -> str:
    if isinstance(event.idempotency_key, str) and event.idempotency_key.strip():
        return event.idempotency_key.strip()
    payload = json.dumps(
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "source": event.source,
            "event_type": event.event_type,
            "issue_key": event.issue_key,
            "issue_summary": str(event.issue_summary or "").strip(),
            "issue_description": str(event.issue_description or "").strip(),
            "labels": sorted(str(label).strip().casefold() for label in (event.issue_labels or []) if str(label).strip()),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalize_occurred_at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
