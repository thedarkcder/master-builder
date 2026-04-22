from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_state_machine import resolve_worker_decision_from_precheck
from orchestrator.core.decision_types import (
    DecisionClassification,
    PrecheckOutcome,
    WorkerDecision,
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
    tenant_atlassian_oauth_context_fn: Callable[..., Any] | None = None,
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
        tenant_atlassian_oauth_context_fn,
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

def _worker_decision_from_precheck(*, pre_check: PreRunCheckResult) -> WorkerDecision:
    return resolve_worker_decision_from_precheck(pre_check=pre_check)
