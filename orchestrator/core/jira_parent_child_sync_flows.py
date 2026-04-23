from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from orchestrator.core.clarification_projection_service import resolve_active_clarification_context
from orchestrator.core.clarification_questions import ClarificationQuestionSet
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.followup_context_service import (
    FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
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
from orchestrator.core.jira_parent_child_sync_service import (
    _JiraParentIssueGateway,
    _jira_adapter,
    _atlassian_oauth_context,
    build_workflow_advance_handler_resolver,
)
from orchestrator.core.jira_parent_child_sync_shared import (
    JiraParentChildSyncContext,
    JiraParentChildSyncResult,
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
from orchestrator.core.pm_interview_followup_service import continue_pm_interview_from_followup
from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_STATUS_PM_COMPLETED,
    mark_pm_interview_case_completed,
)
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.webhook_job_errors import RetryableWebhookJobError
from orchestrator.core.workflow_runtime import WorkflowAdvanceRequest

logger = logging.getLogger(__name__)


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
    runtime = build_workflow_runtime_fn(
        session=session,
        settings=settings,
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
        resolve_advance_handler_fn=build_workflow_advance_handler_resolver(
            integration_router=integration_router,
            extract_changed_fields_fn=extract_changed_fields_fn,
            extract_status_transition_fn=extract_status_transition_fn,
            build_runtime_for_selector_fn=build_runtime_for_selector_fn,
            seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
            post_jira_comment_fn=post_jira_comment_fn,
            create_jira_comment_fn=create_jira_comment_fn,
        ),
    )
    result = runtime.advance(
        request=WorkflowAdvanceRequest(
            workflow_handler_key="jira_parent_feature",
            tenant_id=context.tenant_id,
            tenant=context.tenant,
            project_id=context.project_id,
            issue_key=context.issue_key,
            issue_labels=tuple(context.issue_labels or []),
            payload={**dict(context.payload or {}), "request_id": context.request_id},
            webhook_event=context.webhook_event,
            comment_command=context.comment_command,
            comment_command_argument=context.comment_command_argument,
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
    return JiraParentChildSyncResult(
        handled=result.handled,
        reason=result.reason,
        extra=dict(result.extra or {}),
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
                workflow_id=context.workflow_id,
                operation_id=context.operation_id,
                attempt_id=context.attempt_id,
                issue_key=child_detail.key,
            ),
        )
    except CodexRuntimeError as exc:
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarification_translation_failed",
            extra={"error": str(exc), "webhook_event": context.webhook_event},
        )
    classification = translation.classification
    stakeholder_question = translation.stakeholder_question or ""
    child_block_note = translation.child_block_note
    reason = translation.reason
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
            context=context,
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
    comment = context.payload.get("comment")
    comment_body = ""
    if isinstance(comment, dict):
        body = comment.get("body")
        if isinstance(body, str):
            comment_body = body
    if not comment_body:
        comment_body = str(extract_jira_comment_text_fn(context.payload) or "")
    if not comment_body or is_system_generated_comment(text=comment_body):
        return JiraParentChildSyncResult(handled=False)

    metadata = dict(getattr(followup_context, "metadata_json", {}) or {})
    affected_child_keys = [
        str(value).strip().upper() for value in metadata.get("affected_child_keys", []) if str(value).strip()
    ]
    if not affected_child_keys:
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarification_context_missing_children",
            extra={"webhook_event": context.webhook_event},
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
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_clarification_refresh_failed",
            extra={"stale_child_keys": affected_child_keys, "webhook_event": context.webhook_event},
        )

    if bool(seed_data.get("requires_input")):
        questions = ClarificationQuestionSet.from_values(seed_data.get("questions", [])).questions
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=[context.issue_key, *affected_child_keys])
        metadata["parent_updated"] = bool(seed_data.get("updated_parent") or seed_data.get("created_parent"))
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        clarification_service = ParentPlanningClarificationService()
        clarification_service.ensure_active_clarification(
            issue_key=context.issue_key,
            questions=questions,
            publisher=JiraEngineeringClarificationPublisher(
                session=session,
                context=context,
                settings=settings,
                metadata=metadata,
                create_jira_comment_fn=create_jira_comment_fn,
            ),
        )
        session.commit()
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_clarification_still_open",
            extra={
                "questions": ClarificationQuestionSet.from_values(questions).to_payload(),
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
    session.commit()
    updated_children, created_children, changed_children = _combined_child_updates(seed_data=seed_data)
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
        return JiraParentChildSyncResult(handled=False)

    runtime = build_runtime_for_selector_fn(
        session=session,
        settings=settings,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        selector="discord.pm_answer",
        agent_role="pm",
        agent_name="pm_primary",
    )
    comment_id = extract_jira_comment_id_fn(context.payload)
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
    runtime = build_workflow_runtime_fn(
        session=session,
        settings=settings,
        process_claimed_run_fn=None,
        build_runner_fn=None,
        runtime_kwargs_fn=None,
        resolve_advance_handler_fn=build_workflow_advance_handler_resolver(
            integration_router=integration_router,
            extract_changed_fields_fn=lambda *args, **kwargs: [],
            extract_status_transition_fn=lambda *args, **kwargs: (None, None),
            build_runtime_for_selector_fn=build_runtime_for_selector_fn,
            seed_issues_with_runtime_fn=seed_issues_with_runtime_fn,
            post_jira_comment_fn=post_jira_comment_fn,
            create_jira_comment_fn=create_jira_comment_fn,
        ),
    )
    advance_result = runtime.advance(
        request=WorkflowAdvanceRequest(
            workflow_handler_key="jira_parent_feature",
            tenant_id=context.tenant_id,
            tenant=context.tenant,
            project_id=context.project_id,
            issue_key=context.issue_key,
            issue_labels=tuple(parent_detail.labels or []),
            payload={
                **dict(context.payload or {}),
                "request_id": context.request_id,
                "_mb_pm_interview_followup": True,
                "brief_payload": brief_payload,
                "next_questions": ClarificationQuestionSet.from_values(
                    followup_result.clarification_questions
                ).to_payload(),
                "ready_to_write": bool(followup_result.assessment.ready_to_write),
            },
            webhook_event=context.webhook_event,
            comment_command=context.comment_command,
            comment_command_argument=context.comment_command_argument,
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
        transition_plan=advance_result.transition_plan,
    )
