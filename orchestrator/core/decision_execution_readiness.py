from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_types import WorkerDecision
from orchestrator.core.pre_run_check import PreRunCheckResult, evaluate_pre_run_check
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


def resolve_enqueue_precheck_outcome(
    *,
    source: str,
    precheck_outcome: str | None = None,
    precheck_source_plan: object | None = None,
) -> str | None:
    _ = source
    if is_pr_remediation_run(
        run_plan=precheck_source_plan,
    ):
        return "ready_for_agent"

    normalized_outcome = resolve_precheck_outcome_for_enqueue(
        precheck_outcome=precheck_outcome,
        precheck_source_plan=precheck_source_plan,
    )
    return normalized_outcome


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
            block_reason="policy_eval_failed",
        )

    persisted_outcome = resolve_precheck_outcome_for_enqueue(
        precheck_outcome=None,
        precheck_source_plan=run_plan,
    )
    if persisted_outcome is None:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Execution readiness check failed: missing persisted pre_check_outcome",
            block_reason="policy_eval_failed",
        )

    if persisted_outcome == "ready_for_agent":
        return WorkerDecision(
            allowed=True,
            decision_gate=None,
            configuration_error=None,
            classification="clear",
        )

    execution_context = snapshot.context.execution_context
    run_not_ready = execution_context.get("run_not_ready")
    run_not_ready_payload = run_not_ready if isinstance(run_not_ready, dict) else {}
    configured_ready_label = (
        str((tenant.jira_config or {}).get("ready_label") or "").strip()
        if tenant is not None
        else ""
    )

    if persisted_outcome == "missing_ready_label":
        ready_label = (
            str(run_not_ready_payload.get("ready_label") or "").strip()
            or configured_ready_label
            or None
        )
        guidance = (
            f"{enqueue_reason_guidance('missing_ready_label')} ({ready_label})"
            if ready_label
            else enqueue_reason_guidance("missing_ready_label")
        )
        return WorkerDecision(
            allowed=False,
            decision_gate=DecisionGateResult(
                triggered=True,
                reason=guidance,
                missing_sections=(),
                questions=(),
                recommendation="Apply the configured ready label before execution.",
                tags=(),
            ),
            configuration_error=None,
            block_reason="missing_ready_label",
            classification="clear",
        )

    if persisted_outcome in {"decision_gate_required", "gtd_required", "execution_blocked"}:
        guidance = enqueue_reason_guidance(persisted_outcome)
        reason = str(run_not_ready_payload.get("reason") or "").strip() or guidance
        return WorkerDecision(
            allowed=False,
            decision_gate=DecisionGateResult(
                triggered=True,
                reason=reason,
                missing_sections=(),
                questions=(),
                recommendation="Resolve the open clarification topics before execution.",
                tags=(),
            ),
            configuration_error=None,
            block_reason=persisted_outcome,
            classification="decision_gate" if persisted_outcome == "decision_gate_required" else "gtd",
        )

    if persisted_outcome == "policy_eval_failed":
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Execution readiness check failed: persisted policy evaluation failure",
            block_reason="policy_eval_failed",
        )

    return WorkerDecision(
        allowed=False,
        decision_gate=None,
        configuration_error=f"Execution readiness check failed: unsupported persisted outcome '{persisted_outcome}'",
        block_reason="policy_eval_failed",
        classification="clear",
    )
