from __future__ import annotations

from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.decision_precheck_mapping import worker_blocking_gate
from orchestrator.core.decision_types import WorkerDecision, blocking_reason_for_precheck
from orchestrator.core.pre_run_check import PreRunCheckResult, evaluate_execution_readiness_only
from orchestrator.core.precheck_decision import precheck_classification
from orchestrator.core.runs import is_ready_for_agent_precheck
from orchestrator.core.workflow.execution_snapshot import load_parsed_trigger_context_from_plan
from orchestrator.core.workflow.trigger_context import GithubPrRemediationTriggerContext

_LEGACY_REMEDIATION_DESCRIPTION_PREFIX = "automated remediation run triggered from github pr #"
_LEGACY_REMEDIATION_SUMMARY_MARKER = ": pr remediation for #"


def _is_pr_remediation_run(
    *,
    run_plan: object | None,
    issue_summary: str | None,
    issue_description: str | None,
) -> bool:
    trigger_context = load_parsed_trigger_context_from_plan(run_plan)
    if isinstance(trigger_context, GithubPrRemediationTriggerContext):
        return True
    normalized_summary = str(issue_summary or "").strip().lower()
    normalized_description = str(issue_description or "").strip().lower()
    return (
        _LEGACY_REMEDIATION_SUMMARY_MARKER in normalized_summary
        and normalized_description.startswith(_LEGACY_REMEDIATION_DESCRIPTION_PREFIX)
    )


def evaluate_worker_readiness(
    *,
    run_plan: object | None,
    tenant_id: str | None,
    project_id: str | None,
    issue_key: str | None,
    run_id: str | None,
    issue_summary: str | None,
    issue_description: str | None,
    session: Session | None = None,
    tenant=None,  # noqa: ANN001
    project=None,  # noqa: ANN001
    issue_labels: list[str] | None = None,
    settings: Any = None,
    tenant_jira_oauth_context_fn: Callable[..., Any] | None = None,
    evaluate_pre_run_check_fn: Callable[..., PreRunCheckResult] = evaluate_execution_readiness_only,
    evaluate_decision_gate_fn: Callable[..., DecisionGateResult] | None = None,
) -> WorkerDecision:
    _ = run_id, session, project, settings, tenant_jira_oauth_context_fn, evaluate_decision_gate_fn

    if is_ready_for_agent_precheck(run_plan):
        return WorkerDecision(allowed=True, decision_gate=None, configuration_error=None)

    if _is_pr_remediation_run(
        run_plan=run_plan,
        issue_summary=issue_summary,
        issue_description=issue_description,
    ):
        return WorkerDecision(allowed=True, decision_gate=None, configuration_error=None)

    if tenant is None:
        return WorkerDecision(
            allowed=False,
            decision_gate=None,
            configuration_error="Decision Gate configuration error: missing tenant for worker readiness check",
        )

    ready_label = (tenant.jira_config or {}).get("ready_label")
    pre_check = evaluate_pre_run_check_fn(
        tenant_id=tenant_id,
        project_id=project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        ready_label=ready_label,
    )
    block_reason = blocking_reason_for_precheck(pre_check)
    classification = precheck_classification(pre_check)
    blocking_gate = worker_blocking_gate(
        pre_check=pre_check,
        classification=classification,
        block_reason=block_reason,
    )
    if blocking_gate is not None:
        return WorkerDecision(
            allowed=False,
            decision_gate=blocking_gate,
            configuration_error=None,
            block_reason=block_reason,
            classification=classification,
            pre_check=pre_check,
        )
    if str(block_reason or "").strip() == "missing_ready_label":
        return WorkerDecision(
            allowed=False,
            decision_gate=DecisionGateResult(
                triggered=True,
                reason=enqueue_reason_guidance("missing_ready_label"),
                missing_sections=(),
                questions=(),
                recommendation="Apply the configured ready label before execution.",
                tags=(),
            ),
            configuration_error=None,
            block_reason="missing_ready_label",
            classification=classification,
            pre_check=pre_check,
        )
    return WorkerDecision(
        allowed=True,
        decision_gate=None,
        configuration_error=None,
        block_reason=block_reason,
        classification=classification,
        pre_check=pre_check,
    )
