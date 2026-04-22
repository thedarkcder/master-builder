from __future__ import annotations

from typing import Any, Callable

from orchestrator.core.decision_clarification_service import evaluate_issue_clarification_state
from orchestrator.core.decision_engine import DecisionEngineResult, DecisionEventInput


def evaluate_issue_precheck_state(
    *,
    session,
    tenant,
    project,
    event: DecisionEventInput,
    settings: Any,
    tenant_atlassian_oauth_context_fn: Callable[..., Any],
    evaluate_pre_run_check_fn: Callable[..., object],
    oauth_context: Any | None = None,
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None = None,
) -> DecisionEngineResult:
    return evaluate_issue_clarification_state(
        session=session,
        tenant=tenant,
        project=project,
        event=event,
        settings=settings,
        tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context_fn,
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
        oauth_context=oauth_context,
        publish_jira_comment_fn=publish_jira_comment_fn,
    )
