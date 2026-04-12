from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_types import (
    DecisionClassification,
    PrecheckOutcome,
    WorkerDecision,
    guidance_for_precheck_block_reason,
)
from orchestrator.core.pre_run_check import (
    PreRunCheckResult,
    evaluate_pre_run_check,
)
from orchestrator.core.runs import is_ready_for_agent_precheck, resolve_precheck_outcome_for_enqueue
from orchestrator.core.workflow.execution_snapshot import (
    ExecutionSnapshot,
    load_parsed_trigger_context_from_plan,
)
from orchestrator.core.workflow.trigger_context import GithubPrRemediationTriggerContext
from orchestrator.storage.models import Project, Tenant


def is_pr_remediation_run(
    *,
    run_plan: object | None,
) -> bool:
    trigger_context = load_parsed_trigger_context_from_plan(run_plan)
    return isinstance(trigger_context, GithubPrRemediationTriggerContext)


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
    project: Project | None = None,
    issue_labels: list[str] | None = None,
    settings=None,  # noqa: ANN001
    tenant_jira_oauth_context_fn: Callable[..., Any] | None = None,
    evaluate_pre_run_check_fn: Callable[..., PreRunCheckResult] = evaluate_pre_run_check,
    evaluate_decision_gate_fn: Callable[..., DecisionGateResult] | None = None,
) -> WorkerDecision:
    _ = (
        tenant_id,
        project_id,
        issue_key,
        run_id,
        issue_summary,
        issue_description,
        session,
        project,
        issue_labels,
        settings,
        tenant_jira_oauth_context_fn,
        evaluate_pre_run_check_fn,
        evaluate_decision_gate_fn,
    )
    if is_ready_for_agent_precheck(run_plan):
        return WorkerDecision(allowed=True, decision_gate=None, configuration_error=None)

    if is_pr_remediation_run(
        run_plan=run_plan,
    ):
        return WorkerDecision(allowed=True, decision_gate=None, configuration_error=None)

    snapshot = ExecutionSnapshot.load(run_plan)
    if snapshot is None:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Execution readiness check failed: unsupported execution snapshot version/shape",
            block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
        )

    persisted_outcome_raw = resolve_precheck_outcome_for_enqueue(
        precheck_outcome=None,
        precheck_source_plan=run_plan,
    )
    persisted_outcome = PrecheckOutcome.parse(persisted_outcome_raw)
    if persisted_outcome is None:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Execution readiness check failed: missing persisted pre_check_outcome",
            block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
        )

    if persisted_outcome is PrecheckOutcome.READY_FOR_AGENT:
        return WorkerDecision(
            allowed=True,
            decision_gate=None,
            configuration_error=None,
            classification=DecisionClassification.CLEAR,
        )

    if persisted_outcome is PrecheckOutcome.POLICY_EVAL_FAILED:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Execution readiness check failed: persisted policy evaluation failure",
            block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
        )

    return WorkerDecision(
        allowed=False,
        decision_gate=None,
        configuration_error=(
            f"Execution readiness check failed: run was queued with non-ready pre_check_outcome '{persisted_outcome.value}'"
        ),
        block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
        classification=DecisionClassification.CLEAR,
    )

def _blocked_worker_decision_from_outcome(
    *,
    outcome: PrecheckOutcome,
    pre_check: PreRunCheckResult | None,
) -> WorkerDecision:
    if outcome is PrecheckOutcome.READY_FOR_AGENT:
        return WorkerDecision(
            allowed=True,
            decision_gate=None,
            configuration_error=None,
            classification=DecisionClassification.CLEAR,
            pre_check=pre_check,
        )
    if outcome is PrecheckOutcome.POLICY_EVAL_FAILED:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Execution readiness check failed: persisted policy evaluation failure",
            block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
            pre_check=pre_check,
        )

    ready_label = str(getattr(pre_check, "ready_label", "") or "").strip() or None
    guidance = guidance_for_precheck_block_reason(
        block_reason=outcome.value,
        ready_label=ready_label,
    )
    if pre_check is not None and outcome in {
        PrecheckOutcome.DECISION_GATE_REQUIRED,
        PrecheckOutcome.GTD_REQUIRED,
        PrecheckOutcome.EXECUTION_BLOCKED,
    }:
        precheck_gate = getattr(pre_check, "decision_gate", None)
        if isinstance(precheck_gate, DecisionGateResult) and str(precheck_gate.reason or "").strip():
            return WorkerDecision(
                allowed=False,
                decision_gate=precheck_gate,
                configuration_error=None,
                block_reason=outcome.value,
                classification=(
                    DecisionClassification.DECISION_GATE
                    if outcome is PrecheckOutcome.DECISION_GATE_REQUIRED
                    else DecisionClassification.GTD
                ),
                pre_check=pre_check,
            )

    synthetic_gate = DecisionGateResult(
        triggered=True,
        reason=guidance,
        missing_sections=tuple(getattr(getattr(pre_check, "gtd", None), "missing_criteria", ()) or ()),
        questions=tuple(getattr(getattr(pre_check, "gtd", None), "clarification_questions", ()) or ()),
        recommendation="Resolve execution-readiness blockers before execution.",
        tags=(),
    )
    return WorkerDecision(
        allowed=False,
        decision_gate=synthetic_gate,
        configuration_error=None,
        block_reason=outcome.value,
        classification=(
            DecisionClassification.CLEAR
            if outcome is PrecheckOutcome.MISSING_READY_LABEL
            else (
                DecisionClassification.DECISION_GATE
                if outcome is PrecheckOutcome.DECISION_GATE_REQUIRED
                else DecisionClassification.GTD
            )
        ),
        pre_check=pre_check,
    )


def _worker_decision_from_precheck(*, pre_check: PreRunCheckResult) -> WorkerDecision:
    parsed_outcome = PrecheckOutcome.parse(pre_check.outcome)
    if parsed_outcome is None:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Execution readiness check failed: persisted policy evaluation failure",
            block_reason=PrecheckOutcome.POLICY_EVAL_FAILED.value,
            pre_check=pre_check,
        )
    return _blocked_worker_decision_from_outcome(outcome=parsed_outcome, pre_check=pre_check)
