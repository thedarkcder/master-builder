from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_engine import DecisionEngineResult
from orchestrator.core.decision_types import ExecutionGateReason, ExecutionGateState, PrecheckOutcome
from orchestrator.core.runs import EnqueueRunResult, enqueue_run, resolve_precheck_outcome_for_enqueue


@dataclass(frozen=True)
class RunGateBlock:
    reason: str
    guidance: str
    detail: str | None
    ready_label: str | None


def resolve_run_gate_block(*, decision_result: DecisionEngineResult) -> RunGateBlock | None:
    gate_state = decision_result.execution_gate.state
    gate_reason = decision_result.execution_gate.reason
    if gate_state == ExecutionGateState.ALLOW_EXECUTION:
        return None
    resolved_reason = _resolve_gate_reason(
        gate_state=gate_state,
        gate_reason=gate_reason,
        fallback_reason=PrecheckOutcome.parse(decision_result.decision.block_reason),
    )
    return RunGateBlock(
        reason=resolved_reason.reason_code,
        guidance=resolved_reason.guidance,
        detail=resolved_reason.detail,
        ready_label=resolved_reason.ready_label,
    )


def _resolve_gate_reason(
    *,
    gate_state: ExecutionGateState,
    gate_reason: ExecutionGateReason | None,
    fallback_reason: PrecheckOutcome | None,
) -> ExecutionGateReason:
    if gate_reason is not None:
        return gate_reason
    if gate_state == ExecutionGateState.POLICY_ERROR:
        return ExecutionGateReason(
            reason_code=PrecheckOutcome.POLICY_EVAL_FAILED.value,
            guidance=enqueue_reason_guidance(PrecheckOutcome.POLICY_EVAL_FAILED.value),
        )
    reason_code = (fallback_reason or PrecheckOutcome.DECISION_GATE_REQUIRED).value
    if gate_state == ExecutionGateState.BLOCK_READY_LABEL:
        reason_code = PrecheckOutcome.MISSING_READY_LABEL.value
    return ExecutionGateReason(
        reason_code=reason_code,
        guidance=enqueue_reason_guidance(reason_code),
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
