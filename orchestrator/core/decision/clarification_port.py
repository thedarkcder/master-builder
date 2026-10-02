from __future__ import annotations

from typing import Any, Callable, Protocol

from sqlalchemy.orm import Session

from orchestrator.core.decision import (
    clarification_service as decision_clarification_service,
)
from orchestrator.core.decision.engine import DecisionEngineResult, DecisionEventInput
from orchestrator.core.decision.reply_service import DecisionReplyCaptureResult
from orchestrator.core.runtime.payload_models import InteractionResponse
from orchestrator.storage.models import Project, Tenant


class DecisionClarificationPort(Protocol):
    def evaluate_issue_clarification_state(
        self,
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
    ) -> DecisionEngineResult: ...

    def capture_decision_reply_and_recheck(
        self,
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
        decision_event_factory: Callable[
            [DecisionReplyCaptureResult], DecisionEventInput
        ],
        tenant_atlassian_oauth_context_fn: Callable[..., Any],
        evaluate_pre_run_check_fn: Callable[..., object],
        oauth_context: Any | None = None,
        publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None = None,
        interpreted_reply: InteractionResponse | None = None,
    ) -> decision_clarification_service.DecisionReplyRecheckResult: ...


class RuntimeDecisionClarificationPort:
    def evaluate_issue_clarification_state(
        self,
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
        return decision_clarification_service.evaluate_issue_clarification_state(
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

    def capture_decision_reply_and_recheck(
        self,
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
        decision_event_factory: Callable[
            [DecisionReplyCaptureResult], DecisionEventInput
        ],
        tenant_atlassian_oauth_context_fn: Callable[..., Any],
        evaluate_pre_run_check_fn: Callable[..., object],
        oauth_context: Any | None = None,
        publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None = None,
        interpreted_reply: InteractionResponse | None = None,
    ) -> decision_clarification_service.DecisionReplyRecheckResult:
        return decision_clarification_service.capture_decision_reply_and_recheck(
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
            decision_event_factory=decision_event_factory,
            tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context_fn,
            evaluate_pre_run_check_fn=evaluate_pre_run_check_fn,
            oauth_context=oauth_context,
            publish_jira_comment_fn=publish_jira_comment_fn,
            interpreted_reply=interpreted_reply,
        )
