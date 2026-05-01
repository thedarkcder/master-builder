from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from orchestrator.core.clarification_projection_service import resolve_active_clarification_context
from orchestrator.core.clarification_questions import ClarificationQuestionSet
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.followup_context_service import (
    CLOSED_FOLLOWUP_CONTEXT_STATUS,
    FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION,
    FOLLOWUP_CONTEXT_PM_INTERVIEW,
    close_followup_contexts,
    resolve_issue_followup_context,
)
from orchestrator.core.jira_parent_child_sync_publishers import (
    JiraEngineeringClarificationPublisher,
    engineering_decision_note as _engineering_decision_note,
    mark_issues_sync_blocked as _mark_issues_sync_blocked,
    post_parent_brief_questions_to_discord as _post_parent_brief_questions_to_discord,
    post_sync_note as _post_sync_note,
    update_issue_sync_label as _update_issue_sync_label,
)
from orchestrator.core.parent_feature_workflow.adapters import (
    _JiraParentIssueGateway,
    _atlassian_oauth_context,
    _jira_adapter,
)
from orchestrator.core.parent_feature_workflow.operations import (
    PARENT_OP_BACKLOG_PLANNING,
    PARENT_OP_BRIEF_NORMALIZATION,
    PARENT_OP_JIRA_CHILD_FANOUT,
    PARENT_OP_JIRA_COMMENT_PROJECTION,
)
from orchestrator.core.jira_parent_child_sync_shared import (
    JiraParentChildSyncContext,
    JiraParentChildSyncResult,
    jira_sync_result_from_advance_result,
    build_clarification_followup_prompt as _build_clarification_followup_prompt,
    combined_child_updates as _combined_child_updates,
    extract_parent_issue_key as _extract_parent_issue_key,
    is_system_generated_comment,
    pm_interview_jira_reply_scope as _pm_interview_jira_reply_scope,
    pm_interview_jira_transport as _pm_interview_jira_transport,
    project_key_for_issue as _project_key_for_issue,
)
from orchestrator.core.parent_feature_brief_store import (
    persist_parent_feature_brief_snapshot,
)
from orchestrator.core.parent_planning_clarification_service import ParentPlanningClarificationService
from orchestrator.core.parent_planning_fanout_service import ParentPlanningFanoutService
from orchestrator.core.pm_interview_followup_service import continue_pm_interview_from_followup
from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    mark_pm_interview_case_completed,
    normalize_pm_interview_evidence,
    pm_interview_case_from_row,
)
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.webhook_job_errors import RetryableWebhookJobError
from orchestrator.core.workflow_runtime import WorkflowAdvanceRequest, WorkflowTrigger
from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionProjection,
    WorkflowExecutionReference,
    WorkflowSourceReference,
    classify_external_workflow_failure,
    ensure_workflow_execution,
    resolve_latest_workflow_execution_by_source,
)
from orchestrator.core.workflow_handler_composition import build_installed_workflow_handler_registry
from orchestrator.core.workflow_operation_retry_use_case import retry_workflow_operation_with_registered_handler
from orchestrator.core.workflow_operation_service import complete_workflow_operation
from orchestrator.core.workflow_step_runner import (
    WorkflowStepAttempt,
    complete_workflow_step_attempt,
    fail_workflow_step_attempt,
    start_workflow_step_attempt,
    wait_workflow_step_attempt,
)
from orchestrator.core.workflow_type_catalog import get_workflow_type, get_workflow_type_by_handler_key
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import FollowupContext, PMInterviewCase, WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt

logger = logging.getLogger(__name__)


def _close_answered_parent_planning_context_if_current(
    *,
    session: Session,
    context_id: str,
    answered_question_fingerprint: str | None,
) -> None:
    followup_context = session.get(FollowupContext, context_id)
    if followup_context is None or followup_context.status != "active":
        return
    metadata = dict(getattr(followup_context, "metadata_json", {}) or {})
    current_fingerprint = str(metadata.get("question_state_fingerprint") or "").strip()
    answered_fingerprint = str(answered_question_fingerprint or "").strip()
    if current_fingerprint and answered_fingerprint and current_fingerprint != answered_fingerprint:
        return
    followup_context.status = CLOSED_FOLLOWUP_CONTEXT_STATUS
    followup_context.closed_at = datetime.now(timezone.utc)
    followup_context.updated_at = followup_context.closed_at


def _complete_answered_backlog_planning_operation(
    *,
    session: Session,
    workflow: WorkflowExecution,
) -> None:
    backlog_operation = _parent_planning_blocked_operation(
        session=session,
        workflow=workflow,
        operation_type=PARENT_OP_BACKLOG_PLANNING,
    )
    latest_attempt = session.execute(
        select(WorkflowOperationAttempt)
        .where(WorkflowOperationAttempt.operation_id == backlog_operation.operation_id)
        .order_by(desc(WorkflowOperationAttempt.attempt_number))
    ).scalars().first()
    if latest_attempt is None:
        raise RuntimeError("Answered backlog planning operation has no persisted attempt to complete")
    complete_workflow_operation(
        session,
        operation=backlog_operation,
        attempt=latest_attempt,
        summary="Backlog planning resumed after stakeholder clarification.",
    )


def _jira_workflow_execution_reference(
    *,
    issue_key: str,
    issue_summary: str | None = None,
    issue_description: object | None = None,
    issue_labels: list[str] | tuple[str, ...] = (),
) -> WorkflowExecutionReference:
    normalized_issue_key = str(issue_key or "").strip().upper()
    if not normalized_issue_key:
        raise RuntimeError("Jira parent workflow execution requires an issue key")
    return WorkflowExecutionReference(
        key=normalized_issue_key,
        source=WorkflowSourceReference(
            source_system="jira",
            source_ref=normalized_issue_key,
            display_name=issue_summary,
            description=issue_description,
            attributes={"jira_issue_labels": list(issue_labels)},
        ),
    )


def _start_parent_workflow_step(
    *,
    session: Session,
    context: JiraParentChildSyncContext,
    parent_detail,
    operation_type: str,
) -> tuple[WorkflowExecutionProjection, WorkflowStepAttempt]:
    workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
    lifecycle = ensure_workflow_execution(
        session=session,
        workflow_type=workflow_type,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        execution=_jira_workflow_execution_reference(
            issue_key=parent_detail.key,
            issue_summary=parent_detail.summary,
            issue_description=parent_detail.description,
            issue_labels=tuple(parent_detail.labels or ()),
        ),
        display_name=parent_detail.summary,
        description=parent_detail.description,
    )
    return lifecycle, start_workflow_step_attempt(lifecycle=lifecycle, operation_type=operation_type)


def _context_with_attempt(
    *,
    context: JiraParentChildSyncContext,
    lifecycle: WorkflowExecutionProjection,
    step: WorkflowStepAttempt,
) -> JiraParentChildSyncContext:
    return JiraParentChildSyncContext(
        request_id=context.request_id,
        tenant_id=context.tenant_id,
        tenant=context.tenant,
        project_id=context.project_id,
        issue_key=context.issue_key,
        issue_labels=list(context.issue_labels or []),
        payload=dict(context.payload or {}),
        webhook_event=context.webhook_event,
        comment_command=context.comment_command,
        comment_command_argument=context.comment_command_argument,
        workflow_id=lifecycle.workflow.workflow_id,
        operation_id=step.operation_id,
        attempt=step.attempt_number,
        attempt_id=step.attempt_id,
    )


def _sync_parent_issue_reference_links(
    *,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    context: JiraParentChildSyncContext,
    issue_key: str,
    issue_summary: str,
    issue_labels: list[str] | tuple[str, ...],
) -> None:
    gateway = _JiraParentIssueGateway(
        session=session,
        settings=settings,
        context=context,
        integration_router=integration_router,
        post_jira_comment_fn=None,
        create_jira_comment_fn=None,
    )
    architecture_gate = gateway.resolve_architecture_gate(
        parent_issue_key=issue_key,
        issue_summary=issue_summary,
        issue_labels=list(issue_labels),
    )
    gateway.upsert_workflow_execution_link(issue_key=issue_key)
    architecture_document = architecture_gate.document
    if architecture_document is None:
        return
    title = str(getattr(architecture_document, "title", "") or "").strip()
    url = str(getattr(architecture_document, "canonical_url", "") or "").strip()
    if not title or not url:
        raise RuntimeError(f"Architecture document link is incomplete for {issue_key}")
    gateway.upsert_architecture_document_link(
        issue_key=issue_key,
        title=title,
        url=url,
    )


def handle_parent_feature_sync(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    extract_changed_fields_fn,
    extract_status_transition_fn,
    build_workflow_runtime_fn,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    handler_registry = build_installed_workflow_handler_registry(
        integration_router=integration_router,
        extract_changed_fields_fn=extract_changed_fields_fn,
        extract_status_transition_fn=extract_status_transition_fn,
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
        post_jira_comment_fn=post_jira_comment_fn,
        create_jira_comment_fn=create_jira_comment_fn,
    )
    runtime = build_workflow_runtime_fn(
        session=session,
        settings=settings,
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
        resolve_advance_handler_fn=handler_registry.resolve_advance_handler,
        workflow_handler_registry=handler_registry,
    )
    result = runtime.advance(
        request=WorkflowAdvanceRequest(
            workflow_handler_key="jira_parent_feature",
            tenant_id=context.tenant_id,
            tenant=context.tenant,
            project_id=context.project_id,
            execution=_jira_workflow_execution_reference(
                issue_key=context.issue_key,
                issue_labels=tuple(context.issue_labels or []),
            ),
            payload={**dict(context.payload or {}), "request_id": context.request_id},
            trigger=WorkflowTrigger(
                event=context.webhook_event,
                command=context.comment_command,
                argument=context.comment_command_argument,
            ),
        )
    )
    if result.handled:
        parent_detail = _jira_adapter(
            integration_router=integration_router,
            session=session,
            tenant=context.tenant,
            settings=settings,
        ).get_issue_detail(issue_id_or_key=context.issue_key)
        _sync_parent_issue_reference_links(
            session=session,
            settings=settings,
            integration_router=integration_router,
            context=context,
            issue_key=context.issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
        )
    return jira_sync_result_from_advance_result(
        session=session,
        workflow_type=get_workflow_type_by_handler_key(session, handler_key="jira_parent_feature"),
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        issue_key=context.issue_key,
        result=result,
    )


def handle_engineering_clarification_command(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    build_runtime_for_selector_fn,
    classify_engineering_clarification_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    if context.comment_command != "clarify" or not context.project_id:
        return JiraParentChildSyncResult(handled=False)
    question = str(context.comment_command_argument or "").strip()
    if not question:
        return JiraParentChildSyncResult(handled=True, reason="invalid_comment_command")
    jira = _jira_adapter(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    )
    oauth = _atlassian_oauth_context(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    )
    child_detail = jira.get_issue_detail(issue_id_or_key=context.issue_key)
    child_labels = {str(label).strip().casefold() for label in child_detail.labels}
    if "engineering-child" not in child_labels:
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarify_requires_engineering_child",
            extra={"webhook_event": context.webhook_event},
        )
    parent_issue_key = _extract_parent_issue_key(child_detail=child_detail)
    if not parent_issue_key:
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_child_parent_missing",
            extra={"webhook_event": context.webhook_event},
        )
    parent_detail = jira.get_issue_detail(issue_id_or_key=parent_issue_key)
    lifecycle, projection_step = _start_parent_workflow_step(
        session=session,
        context=context,
        parent_detail=parent_detail,
        operation_type=PARENT_OP_JIRA_COMMENT_PROJECTION,
    )
    attempt_context = _context_with_attempt(context=context, lifecycle=lifecycle, step=projection_step)
    runtime = build_runtime_for_selector_fn(
        session=session,
        settings=settings,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        selector="discord.pm_answer",
        agent_role="pm",
        agent_name="pm_primary",
    )
    try:
        translation = classify_engineering_clarification_with_runtime_fn(
            runtime=runtime,
            parent_issue_key=parent_issue_key,
            parent_summary=parent_detail.summary,
            parent_description=parent_detail.description,
            child_issue_key=child_detail.key,
            child_summary=child_detail.summary,
            child_description=child_detail.description,
            question=question,
            invocation_context=AgentInvocationContext(
                channel="jira",
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                command="clarify",
                stage="pm-translation",
                working_dir=".",
                workflow_id=attempt_context.workflow_id,
                operation_id=attempt_context.operation_id,
                attempt=attempt_context.attempt,
                attempt_id=attempt_context.attempt_id,
                issue_key=child_detail.key,
                db_session=session,
            ),
        )
    except CodexRuntimeError as exc:
        fail_workflow_step_attempt(
            lifecycle=lifecycle,
            step=projection_step,
            category=classify_external_workflow_failure(error=exc),
            message=str(exc),
        )
        session.commit()
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarification_translation_failed",
            extra={"error": str(exc), "webhook_event": context.webhook_event},
            failed=True,
        )
    except Exception as exc:  # noqa: BLE001
        fail_workflow_step_attempt(
            lifecycle=lifecycle,
            step=projection_step,
            category=classify_external_workflow_failure(error=exc),
            message=str(exc),
        )
        session.commit()
        raise
    classification = translation.classification
    stakeholder_question = translation.stakeholder_question or ""
    child_block_note = translation.child_block_note
    reason = translation.reason
    try:
        if classification != "product_behavior" or not stakeholder_question:
            comment_text = _engineering_decision_note(
                question=question,
                owner="Engineering child team",
                approval_path="Child PR review and architecture review when boundaries or platform risk change",
                rationale=reason or child_block_note,
                status_line="Engineering owns this implementation decision. The parent PM brief does not reopen.",
            )
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=child_detail.key,
                settings=settings,
                body=comment_text,
                post_jira_comment_fn=post_jira_comment_fn,
            )
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=projection_step,
                summary="Engineering clarification was classified and answered on the child issue.",
            )
            session.commit()
            return JiraParentChildSyncResult(
                handled=True,
                reason="clarification_not_product_behavior",
                extra={
                    "classification": classification,
                    "detail_reason": reason,
                    "webhook_event": context.webhook_event,
                },
            )

        _update_issue_sync_label(
            oauth=oauth,
            issue_detail=child_detail,
            target_label="sync-blocked",
        )
        existing_context = resolve_issue_followup_context(
            session=session,
            tenant_id=context.tenant_id,
            issue_key=parent_issue_key,
            context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
        )
        metadata = dict(getattr(existing_context, "metadata_json", {}) or {})
        existing_questions = metadata.get("questions")
        question_entries = list(existing_questions) if isinstance(existing_questions, list) else []
        question_entries.append(
            {
                "source_child_key": child_detail.key,
                "original_question": question,
                "stakeholder_question": stakeholder_question,
                "requested_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        clarification_question_set = ClarificationQuestionSet.from_values(
            [
                {
                    "question": item.get("stakeholder_question"),
                    "source_ref": item.get("source_child_key"),
                }
                for item in question_entries
            ]
        )
        affected_child_keys = {
            *[str(value).strip().upper() for value in metadata.get("affected_child_keys", []) if str(value).strip()],
            child_detail.key.upper(),
        }
        clarification_service = ParentPlanningClarificationService()
        publication = clarification_service.ensure_active_clarification(
            issue_key=parent_issue_key,
            questions=clarification_question_set.questions,
            publisher=JiraEngineeringClarificationPublisher(
                session=session,
                context=attempt_context,
                settings=settings,
                metadata={
                    **metadata,
                    "parent_issue_key": parent_issue_key,
                    "project_id": context.project_id,
                    "project_key": _project_key_for_issue(parent_issue_key),
                    "affected_child_keys": sorted(affected_child_keys),
                    "questions": question_entries,
                    "parent_updated": False,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                },
                create_jira_comment_fn=create_jira_comment_fn,
            ),
        )
        session.commit()
        posted_to_discord = True
        if not publication.already_active:
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=parent_issue_key,
                settings=settings,
                body=(
                    f"Engineering needs a product clarification for child {child_detail.key}. "
                    f"Reply on this parent issue with the decision: {stakeholder_question}"
                ),
                post_jira_comment_fn=post_jira_comment_fn,
            )
            posted_to_discord = _post_parent_brief_questions_to_discord(
                session=session,
                settings=settings,
                tenant=context.tenant,
                project_id=context.project_id,
                parent_issue_key=parent_issue_key,
                questions=clarification_question_set.questions,
            )
            if not posted_to_discord:
                _post_sync_note(
                    session=session,
                    tenant=context.tenant,
                    issue_key=parent_issue_key,
                    settings=settings,
                    body=(
                        "Discord PM follow-up could not be created for this clarification. "
                        "Continue the decision on the parent Jira issue for now."
                    ),
                    post_jira_comment_fn=post_jira_comment_fn,
                )
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=child_detail.key,
            settings=settings,
            body=_engineering_decision_note(
                question=question,
                owner="Product via parent PM interview",
                approval_path=f"Parent feature {parent_issue_key} PM clarification thread",
                rationale=reason or child_block_note or stakeholder_question,
                status_line="Escalated to the parent PM thread because the answer changes product behavior or non-functional requirements.",
            ),
            post_jira_comment_fn=post_jira_comment_fn,
        )
        complete_workflow_step_attempt(
            lifecycle=lifecycle,
            step=projection_step,
            summary="Engineering clarification was projected to the parent PM thread.",
        )
        session.commit()
        return JiraParentChildSyncResult(
            handled=True,
            reason="comment_command_clarify",
            extra={
                "classification": classification,
                "parent_issue_key": parent_issue_key,
                "affected_child_keys": sorted(affected_child_keys),
                "stakeholder_question": stakeholder_question,
                "webhook_event": context.webhook_event,
            },
        )
    except Exception as exc:  # noqa: BLE001
        fail_workflow_step_attempt(
            lifecycle=lifecycle,
            step=projection_step,
            category=classify_external_workflow_failure(error=exc),
            message=str(exc),
        )
        session.commit()
        raise


def _extract_comment_body(
    *,
    context: JiraParentChildSyncContext,
    extract_jira_comment_text_fn,
) -> str:
    comment = context.payload.get("comment")
    comment_body = ""
    if isinstance(comment, dict):
        body = comment.get("body")
        if isinstance(body, str):
            comment_body = body
    if not comment_body:
        comment_body = str(extract_jira_comment_text_fn(context.payload) or "")
    return comment_body.strip()


def _parent_planning_blocked_operation(
    *,
    session: Session,
    workflow: WorkflowExecution,
    operation_type: str,
) -> WorkflowOperation:
    operation = session.execute(
        select(WorkflowOperation)
        .where(
            WorkflowOperation.workflow_id == workflow.workflow_id,
            WorkflowOperation.operation_type == operation_type,
        )
        .order_by(desc(WorkflowOperation.updated_at))
    ).scalar_one_or_none()
    if operation is None:
        raise RuntimeError(f"Parent planning workflow has no operation '{operation_type}' to continue")
    return operation


def handle_parent_planning_clarification_reply(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
    extract_jira_comment_text_fn,
    extract_jira_comment_id_fn,
    build_runtime_for_selector_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    if context.comment_command is not None:
        return JiraParentChildSyncResult(handled=False)
    if not context.project_id or context.webhook_event not in {"comment_created", "comment_updated"}:
        return JiraParentChildSyncResult(handled=False)
    followup_context = resolve_issue_followup_context(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        context_type=FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION,
    )
    if followup_context is None:
        return JiraParentChildSyncResult(handled=False)
    answered_context_id = followup_context.context_id
    comment_body = _extract_comment_body(
        context=context,
        extract_jira_comment_text_fn=extract_jira_comment_text_fn,
    )
    if not comment_body or is_system_generated_comment(text=comment_body):
        return JiraParentChildSyncResult(handled=False)

    metadata = dict(getattr(followup_context, "metadata_json", {}) or {})
    answered_question_fingerprint = str(metadata.get("question_state_fingerprint") or "").strip() or None
    blocked_operation_type = str(metadata.get("blocked_operation_type") or "").strip()
    if blocked_operation_type not in {PARENT_OP_BACKLOG_PLANNING, PARENT_OP_JIRA_CHILD_FANOUT}:
        raise RuntimeError(
            f"Parent planning clarification for {context.issue_key} is missing a valid blocked operation"
        )
    continuation_operation_type = (
        PARENT_OP_JIRA_CHILD_FANOUT
        if blocked_operation_type == PARENT_OP_BACKLOG_PLANNING
        else blocked_operation_type
    )
    workflow = resolve_latest_workflow_execution_by_source(
        session=session,
        tenant_id=context.tenant_id,
        source_system="jira",
        source_ref=context.issue_key,
    )
    if workflow is None:
        raise RuntimeError(f"Parent planning clarification for {context.issue_key} has no workflow execution")
    operation = _parent_planning_blocked_operation(
        session=session,
        workflow=workflow,
        operation_type=continuation_operation_type,
    )
    comment_id = str(extract_jira_comment_id_fn(context.payload) or "").strip() or None
    metadata.update(
        {
            "answer_text": comment_body,
            "answer_comment_id": comment_id,
            "answered_at": datetime.now(timezone.utc).isoformat(),
            "answer_request_id": context.request_id,
        }
    )
    followup_context.metadata_json = metadata
    session.flush()
    session.commit()
    handler_registry = build_installed_workflow_handler_registry(
        integration_router=integration_router,
        extract_changed_fields_fn=lambda *args, **kwargs: [],
        extract_status_transition_fn=lambda *args, **kwargs: (None, None),
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
        post_jira_comment_fn=post_jira_comment_fn,
        create_jira_comment_fn=create_jira_comment_fn,
    )
    handle = retry_workflow_operation_with_registered_handler(
        session=session,
        settings=settings,
        session_factory=create_session_factory(getattr(settings, "database_url", None)),
        workflow=workflow,
        operation=operation,
        handler_registry=handler_registry,
    )
    if str(handle.status or "").strip().lower() == "running":
        raise RuntimeError(
            "Parent planning clarification continuation returned running; "
            "reply context cannot be closed until the continuation reaches a durable state"
        )
    if blocked_operation_type == PARENT_OP_BACKLOG_PLANNING and str(handle.status or "").strip().lower() == "completed":
        _complete_answered_backlog_planning_operation(
            session=session,
            workflow=workflow,
        )
    _close_answered_parent_planning_context_if_current(
        session=session,
        context_id=answered_context_id,
        answered_question_fingerprint=answered_question_fingerprint,
    )
    session.commit()
    return JiraParentChildSyncResult(
        handled=True,
        reason="parent_planning_clarification_resumed",
        extra={
            "operation_type": blocked_operation_type,
            "continuation_operation_type": continuation_operation_type,
            "operation_status": handle.status,
            "webhook_event": context.webhook_event,
        },
    )


def handle_engineering_clarification_reply(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
    extract_jira_comment_text_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    if context.comment_command is not None:
        return JiraParentChildSyncResult(handled=False)
    if not context.project_id or context.webhook_event not in {"comment_created", "comment_updated"}:
        return JiraParentChildSyncResult(handled=False)
    followup_context = resolve_issue_followup_context(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    )
    if followup_context is None:
        return JiraParentChildSyncResult(handled=False)
    comment_body = _extract_comment_body(
        context=context,
        extract_jira_comment_text_fn=extract_jira_comment_text_fn,
    )
    if not comment_body or is_system_generated_comment(text=comment_body):
        return JiraParentChildSyncResult(handled=False)

    metadata = dict(getattr(followup_context, "metadata_json", {}) or {})
    affected_child_keys = [
        str(value).strip().upper() for value in metadata.get("affected_child_keys", []) if str(value).strip()
    ]
    if not affected_child_keys:
        raise RuntimeError(
            f"Engineering clarification context for {context.issue_key} is missing affected_child_keys"
        )

    jira = _jira_adapter(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    )
    oauth = _atlassian_oauth_context(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    )
    parent_detail = jira.get_issue_detail(issue_id_or_key=context.issue_key)
    lifecycle, fanout_step = _start_parent_workflow_step(
        session=session,
        context=context,
        parent_detail=parent_detail,
        operation_type=PARENT_OP_JIRA_CHILD_FANOUT,
    )
    attempt_context = _context_with_attempt(context=context, lifecycle=lifecycle, step=fanout_step)
    child_details = [
        jira.get_issue_detail(issue_id_or_key=child_key)
        for child_key in affected_child_keys
    ]
    prompt_markdown = _build_clarification_followup_prompt(
        parent_detail=parent_detail,
        child_details=child_details,
        metadata=metadata,
        reply_text=comment_body,
    )
    try:
        _, seed_data = seed_issues_with_runtime_fn(
            session=session,
            tenant=context.tenant,
            prompt_markdown=prompt_markdown,
            scoped_project_id=context.project_id,
            force_issue_keys=[context.issue_key, *affected_child_keys],
            allow_create=True,
            allow_empty_children=True,
            scoped_project_keys=[_project_key_for_issue(context.issue_key)],
            codex_working_dir=".",
            workflow_id=fanout_step.workflow_id,
            operation_id=fanout_step.operation_id,
            attempt_ref=fanout_step.ref,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "jira_engineering_clarification_reply_failed request_id=%s tenant_id=%s parent_issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=[context.issue_key, *affected_child_keys])
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=f"Clarification reply was captured but parent/child refresh failed: {exc}",
            post_jira_comment_fn=post_jira_comment_fn,
        )
        fail_workflow_step_attempt(
            lifecycle=lifecycle,
            step=fanout_step,
            category=classify_external_workflow_failure(error=exc),
            message=str(exc),
        )
        session.commit()
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_clarification_refresh_failed",
            extra={"stale_child_keys": affected_child_keys, "webhook_event": context.webhook_event},
            failed=True,
        )

    try:
        seed_evaluation = ParentPlanningFanoutService().evaluate_seed_data(
            seed_data=seed_data,
            combine_child_updates_fn=_combined_child_updates,
        )
        if not seed_evaluation.completed:
            _mark_issues_sync_blocked(oauth=oauth, issue_keys=[context.issue_key, *affected_child_keys])
            metadata["parent_updated"] = bool(seed_data.get("updated_parent") or seed_data.get("created_parent"))
            metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
            clarification_service = ParentPlanningClarificationService()
            waiting_state = clarification_service.ensure_waiting_clarification(
                issue_key=context.issue_key,
                questions=seed_evaluation.questions,
                publisher=JiraEngineeringClarificationPublisher(
                    session=session,
                    context=attempt_context,
                    settings=settings,
                    metadata=metadata,
                    create_jira_comment_fn=create_jira_comment_fn,
                ),
                context="Engineering clarification reply",
            )
            wait_workflow_step_attempt(
                lifecycle=lifecycle,
                step=fanout_step,
                summary=waiting_state.message,
            )
            session.commit()
            return JiraParentChildSyncResult(
                handled=True,
                reason="engineering_clarification_still_open",
                extra={
                    "questions": ClarificationQuestionSet.from_values(seed_evaluation.questions).to_payload(),
                    "stale_child_keys": affected_child_keys,
                    "webhook_event": context.webhook_event,
                },
            )

        close_followup_contexts(
            session=session,
            tenant_id=context.tenant_id,
            context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
            issue_key=context.issue_key,
        )
        updated_children = seed_evaluation.updated_children
        created_children = seed_evaluation.created_children
        changed_children = seed_evaluation.changed_children
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=(
                "Clarification reply applied to the parent feature and engineering child tickets are current again. "
                f"{'Refreshed children: ' + ', '.join(updated_children) + '.' if updated_children else ''} "
                f"{'Created children: ' + ', '.join(created_children) + '.' if created_children else ''} "
                f"{'No engineering child changes were required.' if not updated_children and not created_children else ''}"
            ).strip(),
            post_jira_comment_fn=post_jira_comment_fn,
        )
        for child_key in changed_children or affected_child_keys:
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=child_key,
                settings=settings,
                body=f"Product clarification from parent feature {context.issue_key} has been applied. Sync is current again.",
                post_jira_comment_fn=post_jira_comment_fn,
            )
        complete_workflow_step_attempt(
            lifecycle=lifecycle,
            step=fanout_step,
            summary="Engineering clarification reply refreshed the parent and child tickets.",
        )
        session.commit()
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_clarification_resolved",
            extra={
                "parent_issue_key": context.issue_key,
                "updated_children": changed_children,
                "parent_revision": seed_data.get("parent_revision"),
                "children_sync_status": seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )
    except Exception as exc:  # noqa: BLE001
        fail_workflow_step_attempt(
            lifecycle=lifecycle,
            step=fanout_step,
            category=classify_external_workflow_failure(error=exc),
            message=str(exc),
        )
        session.commit()
        raise


def _has_waiting_pm_brief_gate(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
) -> bool:
    workflow = resolve_latest_workflow_execution_by_source(
        session=session,
        tenant_id=tenant_id,
        source_system="jira",
        source_ref=issue_key,
    )
    if workflow is None or workflow.status != "waiting_for_input":
        return False
    operation = session.execute(
        select(WorkflowOperation).where(
            WorkflowOperation.workflow_id == workflow.workflow_id,
            WorkflowOperation.operation_type == PARENT_OP_BRIEF_NORMALIZATION,
            WorkflowOperation.status == "waiting_for_input",
        )
    ).scalar_one_or_none()
    return operation is not None


def _completed_pm_interview_replay_case(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
    comment_id: str | None,
) -> PMInterviewCase | None:
    normalized_comment_id = str(comment_id or "").strip()
    if not normalized_comment_id or not _has_waiting_pm_brief_gate(
        session=session,
        tenant_id=tenant_id,
        issue_key=issue_key,
    ):
        return None
    cases = session.execute(
        select(PMInterviewCase)
        .where(
            PMInterviewCase.tenant_id == tenant_id,
            PMInterviewCase.parent_issue_key == issue_key,
            PMInterviewCase.status == PM_INTERVIEW_STATUS_PM_COMPLETED,
        )
        .order_by(desc(PMInterviewCase.updated_at))
    ).scalars()
    for interview_case in cases:
        evidence_items = normalize_pm_interview_evidence(interview_case.evidence_json or [])
        if any(item.source_ref == normalized_comment_id for item in evidence_items):
            return interview_case
    return None


def _advance_parent_after_pm_interview(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    build_workflow_runtime_fn,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
    parent_detail,
    brief_payload: dict,
    next_questions,
    ready_to_write: bool,
) -> JiraParentChildSyncResult:
    handler_registry = build_installed_workflow_handler_registry(
        integration_router=integration_router,
        extract_changed_fields_fn=lambda *args, **kwargs: [],
        extract_status_transition_fn=lambda *args, **kwargs: (None, None),
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
        post_jira_comment_fn=post_jira_comment_fn,
        create_jira_comment_fn=create_jira_comment_fn,
    )
    runtime = build_workflow_runtime_fn(
        session=session,
        settings=settings,
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
        resolve_advance_handler_fn=handler_registry.resolve_advance_handler,
        workflow_handler_registry=handler_registry,
    )
    advance_result = runtime.advance(
        request=WorkflowAdvanceRequest(
            workflow_handler_key="jira_parent_feature",
            tenant_id=context.tenant_id,
            tenant=context.tenant,
            project_id=context.project_id,
            execution=_jira_workflow_execution_reference(
                issue_key=context.issue_key,
                issue_summary=parent_detail.summary,
                issue_description=parent_detail.description,
                issue_labels=tuple(parent_detail.labels or []),
            ),
            payload={
                **dict(context.payload or {}),
                "request_id": context.request_id,
                "_mb_pm_interview_followup": True,
                "brief_payload": brief_payload,
                "next_questions": ClarificationQuestionSet.from_values(next_questions).to_payload(),
                "ready_to_write": ready_to_write,
            },
            trigger=WorkflowTrigger(
                event=context.webhook_event,
                command=context.comment_command,
                argument=context.comment_command_argument,
            ),
        )
    )
    if advance_result.handled:
        _sync_parent_issue_reference_links(
            session=session,
            settings=settings,
            integration_router=integration_router,
            context=context,
            issue_key=context.issue_key,
            issue_summary=parent_detail.summary,
            issue_labels=list(parent_detail.labels or []),
        )
    session.commit()
    return JiraParentChildSyncResult(
        handled=advance_result.handled,
        reason=advance_result.reason,
        extra=dict(advance_result.extra or {}),
    )


def handle_pm_interview_reply(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    integration_router,
    build_workflow_runtime_fn,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    create_jira_comment_fn,
    extract_jira_comment_text_fn,
    extract_jira_comment_id_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    if context.comment_command is not None:
        return JiraParentChildSyncResult(handled=False)
    if not context.project_id or context.webhook_event not in {"comment_created", "comment_updated"}:
        return JiraParentChildSyncResult(handled=False)

    comment_body = str(extract_jira_comment_text_fn(context.payload) or "").strip()
    if not comment_body or is_system_generated_comment(text=comment_body):
        return JiraParentChildSyncResult(handled=False)

    comment_id = extract_jira_comment_id_fn(context.payload)
    jira_followup = resolve_active_clarification_context(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
        transport=_pm_interview_jira_transport(),
        reply_scope=_pm_interview_jira_reply_scope(),
    )
    pm_request_id = None
    if jira_followup is not None:
        metadata = dict(getattr(jira_followup, "metadata_json", {}) or {})
        pm_request_id = str(metadata.get("pm_request_id") or "").strip() or None
    if not pm_request_id:
        replay_case = _completed_pm_interview_replay_case(
            session=session,
            tenant_id=context.tenant_id,
            issue_key=context.issue_key,
            comment_id=comment_id,
        )
        if replay_case is None:
            return JiraParentChildSyncResult(handled=False)
        parent_detail = _jira_adapter(
            integration_router=integration_router,
            session=session,
            tenant=context.tenant,
            settings=settings,
        ).get_issue_detail(issue_id_or_key=context.issue_key)
        replay_assessment = pm_interview_case_from_row(replay_case)
        return _advance_parent_after_pm_interview(
            context=context,
            session=session,
            settings=settings,
            integration_router=integration_router,
            build_workflow_runtime_fn=build_workflow_runtime_fn,
            build_runtime_for_selector_fn=build_runtime_for_selector_fn,
            seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
            post_jira_comment_fn=post_jira_comment_fn,
            create_jira_comment_fn=create_jira_comment_fn,
            parent_detail=parent_detail,
            brief_payload=replay_assessment.brief.to_payload(),
            next_questions=(),
            ready_to_write=True,
        )

    runtime = build_runtime_for_selector_fn(
        session=session,
        settings=settings,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        selector="discord.pm_answer",
        agent_role="pm",
        agent_name="pm_primary",
    )
    project_key = _project_key_for_issue(context.issue_key)
    try:
        followup_result = continue_pm_interview_from_followup(
            session=session,
            runtime=runtime,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            request_id=pm_request_id,
            reply_text=comment_body,
            source_transport="jira_comment",
            source_ref=comment_id or context.delivery_id,
            actor_ref=str(context.payload.get("comment", {}).get("author", {}).get("accountId") or "").strip() or None,
            invocation_context=AgentInvocationContext(
                channel="jira",
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                command="pm",
                stage="interview",
                working_dir=".",
                workflow_id=context.workflow_id,
                operation_id=context.operation_id,
                attempt_id=context.attempt_id,
                issue_key=context.issue_key,
                db_session=session,
            ),
            project_keys=[project_key],
            issues=[],
            status_counts={},
            settings=settings,
        )
    except CodexRuntimeError as exc:
        logger.warning(
            "jira_pm_interview_reply_retryable_failure request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        raise RetryableWebhookJobError(str(exc), retry_after_seconds=30) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "jira_pm_interview_reply_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_interview_reply_failed",
            extra={"error": str(exc), "webhook_event": context.webhook_event},
            failed=True,
        )

    parent_detail = _jira_adapter(
        integration_router=integration_router,
        session=session,
        tenant=context.tenant,
        settings=settings,
    ).get_issue_detail(issue_id_or_key=context.issue_key)
    brief_payload = followup_result.assessment.brief.to_payload()
    if bool(followup_result.assessment.ready_to_write):
        persist_parent_feature_brief_snapshot(
            session=session,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            parent_issue_key=context.issue_key,
            source_text=parent_detail.description or comment_body,
            brief=brief_payload,
            notes={"source": "jira_pm_interview_reply"},
            status=PM_INTERVIEW_STATUS_PM_COMPLETED,
        )
        mark_pm_interview_case_completed(
            session=session,
            tenant_id=context.tenant_id,
            request_id=pm_request_id,
            parent_issue_key=context.issue_key,
            notes={"source": "jira_pm_interview_reply"},
        )
        close_followup_contexts(
            session=session,
            tenant_id=context.tenant_id,
            context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
            issue_key=context.issue_key,
        )
    session.commit()
    return _advance_parent_after_pm_interview(
        context=context,
        session=session,
        settings=settings,
        integration_router=integration_router,
        build_workflow_runtime_fn=build_workflow_runtime_fn,
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
        post_jira_comment_fn=post_jira_comment_fn,
        create_jira_comment_fn=create_jira_comment_fn,
        parent_detail=parent_detail,
        brief_payload=brief_payload,
        next_questions=followup_result.clarification_questions,
        ready_to_write=bool(followup_result.assessment.ready_to_write),
    )
