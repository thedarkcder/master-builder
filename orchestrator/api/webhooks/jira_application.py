from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import status

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.atlassian_oauth.connection_service import tenant_atlassian_oauth_context
from orchestrator.api.webhooks.jira_comment_planner import plan_jira_comment_flow
from orchestrator.api.webhooks.jira_admission_flow import (
    build_jira_enqueue_skipped_notification_action,
    plan_jira_run_flow,
)
from orchestrator.api.webhooks.jira_event_classifier import evaluate_jira_trigger_state
from orchestrator.api.webhooks.jira_parent_child_sync import handle_parent_feature_sync
from orchestrator.api.webhooks.jira_webhook_board_gate import resolve_project_issue_board_location
from orchestrator.api.webhooks.jira_webhook_types import (
    JiraWebhookContext,
    JiraWebhookContextSnapshot,
    hydrate_jira_webhook_context,
    jira_webhook_response,
    snapshot_jira_webhook_context,
)
from orchestrator.core.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_JIRA,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.core.communications import HttpJsonResponseAction, IngressResult, TransportAction, TransportEnvelope
from orchestrator.core.communications.execution_admission_format import present_jira_admission
from orchestrator.core.decision_state_machine import (
    ExecutionAdmissionReason,
    build_execution_admission_block,
)
from orchestrator.core.jira_issue_intake_routing import classify_jira_issue_intake_with_runtime
from orchestrator.core.observability import reset_log_context, set_log_context
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.webhook_job_errors import RetryableWebhookJobError
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Tenant
from orchestrator.storage.run_queue_events import notify_webhook_job_enqueued

logger = logging.getLogger(__name__)
_PM_PARENT_LABEL = "pm-parent"
_ENGINEERING_CHILD_LABEL = "engineering-child"


@dataclass(frozen=True)
class JiraWebhookPlan:
    content: dict
    actions: tuple[TransportAction, ...] = ()
    status_code: int = 200


def _route_label_for_issue_intake(route: str) -> str | None:
    normalized = str(route or "").strip().lower()
    if normalized == "pm_parent":
        return _PM_PARENT_LABEL
    if normalized == "engineering_child":
        return _ENGINEERING_CHILD_LABEL
    return None


def _maybe_apply_runtime_issue_intake_routing(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> str | None:
    normalized_event = str(context.webhook_event or "").strip().lower()
    normalized_status = str(context.issue_status or "").strip().casefold()
    normalized_labels = {str(label).strip().casefold() for label in context.issue_labels or []}
    if normalized_event not in {"issue_created", "issue_updated"}:
        return None
    if _PM_PARENT_LABEL in normalized_labels or _ENGINEERING_CHILD_LABEL in normalized_labels:
        return None
    board_location, _detail = resolve_project_issue_board_location(
        context=context,
        session=session,
        settings=settings,
    )
    if board_location is None:
        if normalized_status != "backlog":
            return None
    elif board_location != "backlog":
        return None
    runtime = build_runtime_for_selector(
        session=session,
        settings=settings,
        tenant_id=context.tenant_id,
        project_id=context.project.project_id if context.project is not None else None,
        selector="workflow.jira_issue_intake_routing",
    )
    try:
        routing = classify_jira_issue_intake_with_runtime(
            runtime=runtime,
            issue_key=context.issue_key,
            issue_summary=str(context.issue_summary or "").strip(),
            issue_description=str(context.issue_description or "").strip(),
            issue_status=context.issue_status,
            issue_labels=context.issue_labels,
            webhook_event=context.webhook_event,
            invocation_context=AgentInvocationContext(
                channel="jira_webhook",
                tenant_id=context.tenant_id,
                project_id=context.project.project_id if context.project is not None else None,
                command="jira_webhook",
                stage="jira_issue_intake_routing",
                working_dir=".",
                issue_key=context.issue_key,
                issue_description_chars=len(str(context.issue_description or "")),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "jira_issue_intake_routing_retryable_classification_failure request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        raise RetryableWebhookJobError(
            "Jira issue intake routing classification failed",
            retry_after_seconds=45,
        ) from exc
    target_label = _route_label_for_issue_intake(routing["route"])
    if not target_label or target_label in normalized_labels:
        return None
    try:
        oauth = tenant_atlassian_oauth_context(
            session=session,
            tenant=context.tenant,
            settings=settings,
        )
        oauth.client.add_issue_labels(
            access_token=oauth.access_token,
            cloud_id=oauth.connection.cloud_id,
            issue_id_or_key=context.issue_key,
            labels=[target_label],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "jira_issue_intake_routing_retryable_label_failure request_id=%s tenant_id=%s issue_key=%s target_label=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            target_label,
            exc,
        )
        raise RetryableWebhookJobError(
            "Jira issue intake routing label update failed",
            retry_after_seconds=45,
        ) from exc
    context.issue_labels.append(target_label)
    if target_label == _PM_PARENT_LABEL and normalized_event == "issue_updated":
        context.payload["_mb_pm_parent_routed_from_backlog"] = True
    logger.info(
        "jira_issue_intake_routed request_id=%s tenant_id=%s issue_key=%s route=%s confidence=%s reason=%s",
        context.request_id,
        context.tenant_id,
        context.issue_key,
        routing["route"],
        routing["confidence"],
        routing["reason"],
    )
    return target_label

async def build_jira_webhook_ingress_result(
    *,
    tenant_id: str,
    request,
    session: Session,
    settings,  # noqa: ANN001
    envelope: TransportEnvelope,
) -> IngressResult:
    context_tokens = set_log_context(correlation_id=envelope.request_id, tenant_id=tenant_id)
    try:
        from orchestrator.api.webhooks.jira_ingress import stage_parse_jira_webhook_context

        logger.info("jira_webhook_received request_id=%s tenant_id=%s", envelope.request_id, tenant_id)

        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            logger.warning("jira_webhook_unknown_tenant request_id=%s tenant_id=%s", envelope.request_id, tenant_id)
            raise HTTPException(status_code=404, detail="Unknown tenant")
        if not tenant.is_enabled:
            logger.info(
                "jira_webhook_ignored request_id=%s tenant_id=%s reason=tenant_disabled",
                envelope.request_id,
                tenant_id,
            )
            return _http_json_result(
                JiraWebhookPlan(
                    content={
                        "request_id": envelope.request_id,
                        "tenant_id": tenant_id,
                        "enqueued": False,
                        "reason": "tenant_disabled",
                    }
                )
            )

        context = await stage_parse_jira_webhook_context(
            tenant_id=tenant_id,
            tenant=tenant,
            request=request,
            request_id=envelope.request_id,
            session=session,
            settings=settings,
        )
        context_snapshot = snapshot_jira_webhook_context(context=context)
        enqueue_result = enqueue_webhook_job(
            session,
            request=WebhookJobEnqueueRequest(
                transport=WEBHOOK_TRANSPORT_JIRA,
                request_id=context.request_id,
                tenant_id=context.tenant_id,
                project_id=context.project.project_id if context.project is not None else None,
                subject_key=f"jira:{context.tenant_id}:{context.issue_key}",
                dedupe_key=context.delivery_id,
                event_type=context.webhook_event,
                payload_json=dict(context.payload),
                context_json={
                    "snapshot": {
                        "request_id": context_snapshot.request_id,
                        "tenant_id": context_snapshot.tenant_id,
                        "payload": dict(context_snapshot.payload),
                        "webhook_event": context_snapshot.webhook_event,
                        "issue_key": context_snapshot.issue_key,
                        "issue_labels": list(context_snapshot.issue_labels),
                        "issue_status": context_snapshot.issue_status,
                        "issue_status_category_key": context_snapshot.issue_status_category_key,
                        "issue_summary": context_snapshot.issue_summary,
                        "issue_description": context_snapshot.issue_description,
                        "comment_command": context_snapshot.comment_command,
                        "comment_command_argument": context_snapshot.comment_command_argument,
                        "comment_command_error": context_snapshot.comment_command_error,
                        "delivery_id": context_snapshot.delivery_id,
                        "project_id": context_snapshot.project_id,
                    }
                },
            ),
        )
        notify_webhook_job_enqueued(
            session,
            transport=WEBHOOK_TRANSPORT_JIRA,
            tenant_id=context.tenant_id,
            project_id=context.project.project_id if context.project is not None else None,
            subject_key=f"jira:{context.tenant_id}:{context.issue_key}",
            job_id=enqueue_result.job.job_id,
            dedupe_key=enqueue_result.job.dedupe_key,
        )
        session.commit()
        logger.info(
            "jira_webhook_job_enqueued request_id=%s tenant_id=%s issue_key=%s job_id=%s delivery_id=%s created=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            enqueue_result.job.job_id,
            context.delivery_id,
            enqueue_result.created,
        )
        return _http_json_result(
            JiraWebhookPlan(
                content={
                    "request_id": context.request_id,
                    "tenant_id": context.tenant_id,
                    "project_id": context.project.project_id if context.project is not None else None,
                    "issue_key": context.issue_key,
                    "delivery_id": context.delivery_id,
                    "accepted": True,
                    "enqueued": False,
                    "queued": enqueue_result.created,
                    "reason": "queued_for_reconciliation" if enqueue_result.created else "duplicate_delivery",
                    "job_id": enqueue_result.job.job_id,
                    "webhook_event": context.webhook_event,
                },
                status_code=status.HTTP_202_ACCEPTED,
            )
        )
    finally:
        reset_log_context(context_tokens)


def _process_jira_webhook_context(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> JiraWebhookPlan:
    comment_plan = plan_jira_comment_flow(
        context=context,
        session=session,
        settings=settings,
    )
    if comment_plan.content is not None:
        return JiraWebhookPlan(content=comment_plan.content)

    if context.project is None:
        logger.info(
            "jira_webhook_ignored request_id=%s tenant_id=%s issue_key=%s reason=project_not_mapped",
            context.request_id,
            context.tenant_id,
            context.issue_key,
        )
        admission = build_execution_admission_block(
            reason=ExecutionAdmissionReason.PROJECT_NOT_MAPPED,
        )
        admission_presentation = present_jira_admission(admission=admission)
        return JiraWebhookPlan(
            content=jira_webhook_response(
                context,
                enqueued=False,
                command=context.comment_command,
                webhook_event=context.webhook_event,
                **admission_presentation.response_fields,
            ),
            actions=(
                build_jira_enqueue_skipped_notification_action(
                    context=context,
                    admission=admission,
                ),
            ),
        )

    _maybe_apply_runtime_issue_intake_routing(
        context=context,
        session=session,
        settings=settings,
    )

    parent_sync_response = handle_parent_feature_sync(
        context=context,
        session=session,
        settings=settings,
    )
    if parent_sync_response is not None:
        return JiraWebhookPlan(content=parent_sync_response)

    run_plan = plan_jira_run_flow(
        context=context,
        session=session,
        settings=settings,
        evaluate_jira_trigger_state_fn=evaluate_jira_trigger_state,
        jira_webhook_response_fn=jira_webhook_response,
    )
    return JiraWebhookPlan(
        content=run_plan.content,
        actions=run_plan.actions,
    )


def _process_jira_webhook_context_in_thread(
    *,
    context_snapshot: JiraWebhookContextSnapshot,
    settings,  # noqa: ANN001
) -> JiraWebhookPlan:
    session_factory = create_session_factory(getattr(settings, "database_url", None))
    session = session_factory()
    try:
        context = hydrate_jira_webhook_context(
            snapshot=context_snapshot,
            session=session,
        )
        return _process_jira_webhook_context(
            context=context,
            session=session,
            settings=settings,
        )
    finally:
        session.close()


def _http_json_result(plan: JiraWebhookPlan) -> IngressResult:
    return IngressResult(
        actions=(
            *plan.actions,
            HttpJsonResponseAction(status_code=plan.status_code, content=plan.content),
        )
    )
