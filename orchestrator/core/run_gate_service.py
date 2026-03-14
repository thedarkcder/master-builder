from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.runs import EnqueueRunResult, enqueue_run, resolve_precheck_outcome_for_enqueue


@dataclass(frozen=True)
class RunGateBlock:
    reason: str
    guidance: str
    decision_gate_reason: str | None
    decision_gate_questions: tuple[str, ...]
    gtd_missing_criteria: tuple[str, ...]
    gtd_questions: tuple[str, ...]
    ready_label: str | None


def resolve_run_gate_block(*, decision_result: DecisionEngineResult) -> RunGateBlock | None:
    precheck_decision = decision_result.decision
    pre_check = precheck_decision.pre_check
    if pre_check is None:
        return RunGateBlock(
            reason="policy_eval_failed",
            guidance=enqueue_reason_guidance("policy_eval_failed"),
            decision_gate_reason=None,
            decision_gate_questions=(),
            gtd_missing_criteria=(),
            gtd_questions=(),
            ready_label=None,
        )

    block_reason = str(precheck_decision.block_reason or "").strip()
    if block_reason not in {"decision_gate_required", "gtd_required", "missing_ready_label"}:
        return None

    decision_gate_questions = tuple(
        str(question).strip()
        for question in getattr(pre_check.decision_gate, "questions", ())
        if str(question).strip()
    )
    gtd_missing_criteria = tuple(
        str(item).strip()
        for item in getattr(pre_check, "gtd_missing_criteria", ())
        if str(item).strip()
    )
    gtd_questions = tuple(
        str(question).strip()
        for question in getattr(pre_check, "gtd_clarification_questions", ())
        if str(question).strip()
    )
    return RunGateBlock(
        reason=block_reason,
        guidance=enqueue_reason_guidance(block_reason),
        decision_gate_reason=(
            str(getattr(pre_check, "decision_gate_reason", "") or "").strip() or None
            if block_reason == "decision_gate_required"
            else None
        ),
        decision_gate_questions=decision_gate_questions if block_reason == "decision_gate_required" else (),
        gtd_missing_criteria=gtd_missing_criteria if block_reason == "gtd_required" else (),
        gtd_questions=gtd_questions if block_reason == "gtd_required" else (),
        ready_label=(str(pre_check.ready_label or "").strip() or None) if block_reason == "missing_ready_label" else None,
    )


def enqueue_issue_run_with_precheck(
    session: Session,
    *,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    repo_url: str | None,
    delivery_id: str | None,
    precheck_outcome: str | None,
    max_concurrent_runs: int | None,
) -> EnqueueRunResult:
    return enqueue_run(
        session,
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=repo_url,
        delivery_id=delivery_id,
        precheck_outcome=resolve_precheck_outcome_for_enqueue(precheck_outcome=precheck_outcome),
        max_concurrent_runs=max_concurrent_runs,
    )
