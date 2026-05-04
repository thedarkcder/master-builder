from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from orchestrator.core.decision.effect_service import publish_decision_effects
from orchestrator.core.decision.engine import DecisionEngineResult, DecisionEventInput, evaluate_decision_event
from orchestrator.core.decision.reply_service import DecisionReplyCaptureResult, capture_decision_reply
from orchestrator.storage.models import Project, Tenant


@dataclass(frozen=True)
class DecisionReplyRecheckResult:
    capture: DecisionReplyCaptureResult
    decision_result: DecisionEngineResult


def evaluate_issue_clarification_state(
    *,
    session: Session,
    tenant: Tenant,
    project: Project,
    event: DecisionEventInput,
    settings,  # noqa: ANN001
    tenant_atlassian_oauth_context_fn: Callable[..., Any],
    evaluate_pre_run_check_fn: Callable[..., object],
    oauth_context: Any | None = None,
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None = None,
) -> DecisionEngineResult:
    return evaluate_decision_event(
        session=session,
        tenant=tenant,
        project=project,
        event=event,
        settings=settings,
        tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context_fn,
        publish_jira_comment_fn=publish_jira_comment_fn,
        oauth_context=oauth_context,
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
    )


def capture_decision_reply_and_recheck(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    issue_key: str,
    reply_text: str,
    source_transport: str,
    source_ref: str | None,
    actor_ref: str | None,
    metadata: dict[str, Any],
    decision_event_factory: Callable[[DecisionReplyCaptureResult], DecisionEventInput],
    tenant_atlassian_oauth_context_fn: Callable[..., Any],
    evaluate_pre_run_check_fn: Callable[..., object],
    oauth_context: Any | None = None,
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None = None,
) -> DecisionReplyRecheckResult:
    capture = capture_decision_reply(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        issue_key=issue_key,
        reply_text=reply_text,
        source_transport=source_transport,
        source_ref=source_ref,
        actor_ref=actor_ref,
        metadata=metadata,
    )
    capture_effect_ids = tuple(getattr(capture, "effect_ids", ()) or ())
    if capture_effect_ids and publish_jira_comment_fn is not None:
        publish_decision_effects(
            session=session,
            effect_ids=capture_effect_ids,
            publish_jira_comment_fn=publish_jira_comment_fn,
            occurred_at=datetime.now(timezone.utc),
        )
    decision_result = evaluate_issue_clarification_state(
        session=session,
        tenant=tenant,
        project=project,
        event=decision_event_factory(capture),
        settings=settings,
        tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context_fn,
        evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
        oauth_context=oauth_context,
        publish_jira_comment_fn=publish_jira_comment_fn,
    )
    return DecisionReplyRecheckResult(capture=capture, decision_result=decision_result)
