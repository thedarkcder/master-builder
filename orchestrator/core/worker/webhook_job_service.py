from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.commands.entrypoint import execute_tenant_discord_ingress_command
from orchestrator.api.discord.interactions.application import build_default_discord_interaction_dispatch_deps
from orchestrator.api.discord.interactions.dispatcher import dispatch_discord_interaction
from orchestrator.api.discord.shared.state import command_matches
from orchestrator.api.atlassian_oauth.connection_service import tenant_atlassian_oauth_context
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.api.transport_runtime import (
    build_http_transport_action_executors,
    execute_side_effect_action,
    execute_side_effect_ingress_result,
)
from orchestrator.api.webhooks.github_application import build_github_webhook_ingress_result
from orchestrator.api.webhooks.github_webhook_context import (
    GitHubWebhookContext,
    GitHubWebhookPreparedRuntime,
    build_github_review_runtime,
)
from orchestrator.api.webhooks.jira_application import _process_jira_webhook_context
from orchestrator.api.webhooks.jira_webhook_types import (
    JiraWebhookContextSnapshot,
    hydrate_jira_webhook_context,
)
from orchestrator.api.webhooks.contracts import JIRA_COMMENT_EVENTS, resolve_active_project_for_issue
from orchestrator.core.communications import (
    HttpJsonResponseAction,
    HttpJsonResponseBytesAction,
    IngressResult,
    TransportEnvelope,
)
from orchestrator.core.github.transport_executor import GitHubTransportExecutor
from orchestrator.core.projects.automation_execution_service import (
    mark_project_automation_execution_failure,
    mark_project_automation_execution_success,
    prepare_project_automation_execution,
)
from orchestrator.core.webhooks.job_errors import RetryableWebhookJobError
from orchestrator.core.webhooks.job_queue import (
    WEBHOOK_TRANSPORT_DISCORD_COMMAND,
    WEBHOOK_TRANSPORT_DISCORD_INTERACTION,
    WEBHOOK_TRANSPORT_GITHUB,
    WEBHOOK_TRANSPORT_JIRA,
    WEBHOOK_TRANSPORT_PROJECT_AUTOMATION,
    WebhookJob,
    claim_next_webhook_subject_batch,
    mark_webhook_job_ids_failed,
    mark_webhook_jobs_done,
    mark_webhook_jobs_failed,
    requeue_webhook_job_ids,
)
from orchestrator.core.config import Settings
from orchestrator.storage.models import Project, Tenant

logger = logging.getLogger(__name__)

_TEMPORAL_WRAPPER_ERROR_MESSAGES = {
    "Workflow update failed",
    "Activity task failed",
}


def _webhook_failure_message(error: Exception) -> str:
    messages: list[str] = []
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).strip()
        if message:
            messages.append(message)
        current = current.__cause__ or current.__context__
    for message in reversed(messages):
        if message not in _TEMPORAL_WRAPPER_ERROR_MESSAGES:
            return message
    return messages[0] if messages else error.__class__.__name__


def _rollback_job_session(
    session: Session,
    *,
    job_transport: str,
    job_tenant_id: str,
    job_subject_key: str,
    job_id: str,
) -> None:
    try:
        session.rollback()
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "webhook_job_rollback_failed transport=%s tenant_id=%s subject_key=%s job_id=%s error=%s",
            job_transport,
            job_tenant_id,
            job_subject_key,
            job_id,
            exc,
        )
        raise


def _non_http_ingress_result(result: IngressResult) -> IngressResult:
    return IngressResult(
        actions=tuple(
            action
            for action in result.actions
            if not isinstance(action, (HttpJsonResponseAction, HttpJsonResponseBytesAction))
        ),
    )


def _refresh_jira_context_from_live_issue(*, context, session, settings) -> None:  # noqa: ANN001
    oauth = tenant_atlassian_oauth_context(session=session, tenant=context.tenant, settings=settings)
    issue_detail = oauth.client.get_issue_detail(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=context.issue_key,
    )
    context.issue_summary = issue_detail.summary
    context.issue_description = issue_detail.description
    context.issue_status = issue_detail.status
    context.issue_status_category_key = issue_detail.status_category_key
    context.issue_labels = list(issue_detail.labels)
    context.project = resolve_active_project_for_issue(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
    )


def _jira_snapshot_from_job(job: WebhookJob) -> JiraWebhookContextSnapshot:
    payload = dict((job.context_json or {}).get("snapshot") or {})
    return JiraWebhookContextSnapshot(
        request_id=str(payload.get("request_id") or job.request_id),
        tenant_id=str(payload.get("tenant_id") or job.tenant_id or ""),
        payload=dict(payload.get("payload") or job.payload_json or {}),
        webhook_event=str(payload.get("webhook_event") or job.event_type or "").strip() or None,
        issue_key=str(payload.get("issue_key") or "").strip(),
        issue_labels=tuple(str(item).strip() for item in payload.get("issue_labels", []) if str(item).strip()),
        issue_status=str(payload.get("issue_status") or "").strip() or None,
        issue_status_category_key=str(payload.get("issue_status_category_key") or "").strip() or None,
        issue_summary=str(payload.get("issue_summary") or "").strip() or None,
        issue_description=str(payload.get("issue_description") or "").strip() or None,
        comment_command=str(payload.get("comment_command") or "").strip() or None,
        comment_command_argument=str(payload.get("comment_command_argument") or "").strip() or None,
        comment_command_error=str(payload.get("comment_command_error") or "").strip() or None,
        delivery_id=str(payload.get("delivery_id") or job.dedupe_key or "").strip() or None,
        project_id=str(payload.get("project_id") or job.project_id or "").strip() or None,
    )


def _process_jira_subject_jobs(
    *,
    session: Session,
    settings: Settings,
    owner_id: str,
    claimed_job: WebhookJob,
    related_jobs: tuple[WebhookJob, ...],
) -> tuple[WebhookJob, ...]:
    jobs = tuple(sorted((claimed_job, *related_jobs), key=lambda item: item.created_at))
    delete_seen = False
    pending_issue_context = None
    for job in jobs:
        context = hydrate_jira_webhook_context(
            snapshot=_jira_snapshot_from_job(job),
            session=session,
        )
        if context.webhook_event == "issue_deleted":
            delete_seen = True
            plan = _process_jira_webhook_context(
                context=context,
                session=session,
                settings=settings,
            )
            execute_side_effect_ingress_result(
                result=IngressResult(actions=plan.actions),
                envelope=TransportEnvelope(
                    transport=WEBHOOK_TRANSPORT_JIRA,
                    event_type=str(context.webhook_event or "jira_webhook"),
                    request_id=context.request_id,
                    tenant_id_hint=context.tenant_id,
                    delivery_id=context.delivery_id,
                ),
                transport_action_executors=build_http_transport_action_executors(
                    session=session,
                    settings=settings,
                ),
            )
            break
        if context.webhook_event in JIRA_COMMENT_EVENTS:
            plan = _process_jira_webhook_context(
                context=context,
                session=session,
                settings=settings,
            )
            execute_side_effect_ingress_result(
                result=IngressResult(actions=plan.actions),
                envelope=TransportEnvelope(
                    transport=WEBHOOK_TRANSPORT_JIRA,
                    event_type=str(context.webhook_event or "jira_webhook"),
                    request_id=context.request_id,
                    tenant_id_hint=context.tenant_id,
                    delivery_id=context.delivery_id,
                ),
                transport_action_executors=build_http_transport_action_executors(
                    session=session,
                    settings=settings,
                ),
            )
            continue
        pending_issue_context = context
    if not delete_seen and pending_issue_context is not None:
        _refresh_jira_context_from_live_issue(
            context=pending_issue_context,
            session=session,
            settings=settings,
        )
        plan = _process_jira_webhook_context(
            context=pending_issue_context,
            session=session,
            settings=settings,
        )
        execute_side_effect_ingress_result(
            result=IngressResult(actions=plan.actions),
            envelope=TransportEnvelope(
                transport=WEBHOOK_TRANSPORT_JIRA,
                event_type=str(pending_issue_context.webhook_event or "jira_webhook"),
                request_id=pending_issue_context.request_id,
                tenant_id_hint=pending_issue_context.tenant_id,
                delivery_id=pending_issue_context.delivery_id,
            ),
            transport_action_executors=build_http_transport_action_executors(
                session=session,
                settings=settings,
            ),
        )
    return mark_webhook_jobs_done(
        session,
        jobs=jobs,
        owner_id=owner_id,
    )


def _process_github_subject_jobs(
    *,
    session: Session,
    settings: Settings,
    owner_id: str,
    claimed_job: WebhookJob,
    related_jobs: tuple[WebhookJob, ...],
) -> tuple[WebhookJob, ...]:
    jobs = tuple(sorted((claimed_job, *related_jobs), key=lambda item: item.created_at))
    source_job = jobs[-1]
    context_json = dict(source_job.context_json or {})
    tenant = session.get(Tenant, context_json.get("tenant_id"))
    project = session.get(Project, context_json.get("project_id"))
    if tenant is None or project is None or not tenant.is_enabled or bool(getattr(project, "is_archived", False)):
        return mark_webhook_jobs_done(session, jobs=jobs, owner_id=owner_id)
    raw_pr_number = context_json.get("pr_number")
    pr_number = int(raw_pr_number) if raw_pr_number is not None else None
    review_summary_present = bool(context_json.get("review_summary_present"))
    context = GitHubWebhookContext(
        request_id=source_job.request_id,
        delivery_id=str(context_json.get("delivery_id") or source_job.dedupe_key or ""),
        github_event=str(context_json.get("github_event") or source_job.event_type or ""),
        payload=dict(source_job.payload_json or {}),
        normalized_action=str(context_json.get("normalized_action") or "").strip() or None,
        installation_id=int(context_json.get("installation_id") or 0),
        tenant=tenant,
        project=project,
        repo_full_name=str(context_json.get("repo_full_name") or ""),
        pr_targets=[(pr_number, review_summary_present)] if pr_number is not None else [],
        ref_name=str(context_json.get("ref_name") or "").strip() or None,
    )
    try:
        github_client, reviewer_gate = build_github_review_runtime(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
        )
        prepared_runtime = GitHubWebhookPreparedRuntime(
            context=context,
            github_client=github_client,
            reviewer_gate=reviewer_gate,
            transport_action_executors=(
                GitHubTransportExecutor(
                    github_client=github_client,
                    session=session,
                    logger_override=logger,
                ),
            ),
        )
    except ValueError as exc:
        prepared_runtime = GitHubWebhookPreparedRuntime(
            context=context,
            github_client=None,
            reviewer_gate=None,
            review_runtime_error=str(exc),
        )
    result = asyncio.run(
        build_github_webhook_ingress_result(
            prepared_runtime=prepared_runtime,
            request_id=source_job.request_id,
            session=session,
            settings=settings,
        )
    )
    execute_side_effect_ingress_result(
        result=_non_http_ingress_result(result),
        envelope=TransportEnvelope(
            transport=WEBHOOK_TRANSPORT_GITHUB,
            event_type=str(source_job.event_type or "github_webhook"),
            request_id=source_job.request_id,
            tenant_id_hint=source_job.tenant_id,
            delivery_id=source_job.dedupe_key,
        ),
        transport_action_executors=build_http_transport_action_executors(
            session=session,
            settings=settings,
            extra_transport_action_executors=tuple(getattr(prepared_runtime, "transport_action_executors", ()) or ()),
        ),
    )
    return mark_webhook_jobs_done(session, jobs=jobs, owner_id=owner_id)


def _process_project_automation_job(
    *,
    session: Session,
    settings: Settings,
    owner_id: str,
    claimed_job: WebhookJob,
) -> tuple[WebhookJob, ...]:
    if not claimed_job.tenant_id:
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error="Project automation webhook job is missing tenant_id",
        )
    tenant = session.get(Tenant, claimed_job.tenant_id)
    if tenant is None or not tenant.is_enabled:
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error=f"Project automation tenant '{claimed_job.tenant_id}' is unavailable",
        )
    project = session.get(Project, claimed_job.project_id) if claimed_job.project_id else None
    if project is not None and bool(getattr(project, "is_archived", False)):
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error=f"Project '{project.project_id}' is archived",
        )

    payload = dict(claimed_job.payload_json or {})
    execution_id = str(payload.get("execution_id") or "").strip() or claimed_job.request_id
    plan = None
    try:
        plan = prepare_project_automation_execution(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            request_id=claimed_job.request_id,
            payload_json=payload,
        )
        if plan.already_succeeded or plan.action is None:
            return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)
        delivery = execute_side_effect_action(
            action=plan.action,
            envelope=TransportEnvelope(
                transport=WEBHOOK_TRANSPORT_PROJECT_AUTOMATION,
                event_type=str(claimed_job.event_type or "project_automation"),
                request_id=claimed_job.request_id,
                tenant_id_hint=claimed_job.tenant_id,
                delivery_id=claimed_job.dedupe_key,
            ),
            transport_action_executors=build_http_transport_action_executors(
                session=session,
                settings=settings,
            ),
        )
        discord_message_id = str((delivery or {}).get("message_id") or "").strip()
        if not discord_message_id:
            raise RuntimeError("Project automation delivery did not return a Discord message id")
        mark_project_automation_execution_success(
            session=session,
            execution_id=plan.execution_id,
            window_end_at=plan.window_end_at,
            discord_message_id=discord_message_id,
        )
        return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)
    except Exception as exc:  # noqa: BLE001
        try:
            mark_project_automation_execution_failure(
                session=session,
                execution_id=execution_id if plan is None else plan.execution_id,
                error=str(exc),
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "project_automation_execution_failure_mark_failed request_id=%s tenant_id=%s job_id=%s",
                claimed_job.request_id,
                claimed_job.tenant_id,
                claimed_job.job_id,
            )
        raise


def _process_discord_command_job(
    *,
    session: Session,
    owner_id: str,
    claimed_job: WebhookJob,
) -> tuple[WebhookJob, ...]:
    if not claimed_job.tenant_id:
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error="Discord webhook job is missing tenant_id",
        )
    payload = DiscordCommandRequest.model_validate(dict(claimed_job.payload_json or {}))
    execute_tenant_discord_ingress_command(
        tenant_id=claimed_job.tenant_id,
        payload=payload,
        session=session,
        defer_seed_issues=command_matches(
            payload.command,
            command_name="issues",
            subcommand="seed",
        ),
    )
    return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)


async def _run_discord_interaction_job_async(*, session: Session, claimed_job: WebhookJob) -> None:
    queued: list[object] = []

    def _capture(awaitable: object) -> object:
        queued.append(awaitable)
        return awaitable

    await dispatch_discord_interaction(
        payload=dict(claimed_job.payload_json or {}),
        session=session,
        request_id=claimed_job.request_id,
        deps=build_default_discord_interaction_dispatch_deps(
            task_scheduler=_capture,
            logger=logger,
        ),
    )
    for awaitable in queued:
        await awaitable


def _process_discord_interaction_job(
    *,
    session: Session,
    owner_id: str,
    claimed_job: WebhookJob,
) -> tuple[WebhookJob, ...]:
    asyncio.run(_run_discord_interaction_job_async(session=session, claimed_job=claimed_job))
    return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)


def process_next_webhook_job(
    *,
    session: Session,
    settings: Settings,
    owner_id: str,
) -> WebhookJob | None:
    claim = claim_next_webhook_subject_batch(
        session,
        owner_id=owner_id,
    )
    if not claim.acquired or claim.batch is None:
        return None

    batch = claim.batch
    job = batch.primary_job
    job_transport = str(job.transport)
    job_tenant_id = str(job.tenant_id)
    job_subject_key = str(job.subject_key)
    job_id = str(job.job_id)
    failed_job_ids = batch.job_ids
    try:
        if job.transport == WEBHOOK_TRANSPORT_JIRA:
            processed = _process_jira_subject_jobs(
                session=session,
                settings=settings,
                owner_id=owner_id,
                claimed_job=job,
                related_jobs=batch.related_jobs,
            )
        elif job.transport == WEBHOOK_TRANSPORT_GITHUB:
            processed = _process_github_subject_jobs(
                session=session,
                settings=settings,
                owner_id=owner_id,
                claimed_job=job,
                related_jobs=batch.related_jobs,
            )
        elif job.transport == WEBHOOK_TRANSPORT_PROJECT_AUTOMATION:
            processed = _process_project_automation_job(
                session=session,
                settings=settings,
                owner_id=owner_id,
                claimed_job=job,
            )
        elif job.transport == WEBHOOK_TRANSPORT_DISCORD_COMMAND:
            processed = _process_discord_command_job(
                session=session,
                owner_id=owner_id,
                claimed_job=job,
            )
        elif job.transport == WEBHOOK_TRANSPORT_DISCORD_INTERACTION:
            processed = _process_discord_interaction_job(
                session=session,
                owner_id=owner_id,
                claimed_job=job,
            )
        else:
            processed = mark_webhook_jobs_failed(
                session,
                jobs=(job,),
                owner_id=owner_id,
                error=f"Unsupported webhook transport '{job.transport}'",
            )
        return processed[0] if processed else job
    except HTTPException as exc:
        _rollback_job_session(
            session,
            job_transport=job_transport,
            job_tenant_id=job_tenant_id,
            job_subject_key=job_subject_key,
            job_id=job_id,
        )
        logger.warning(
            "webhook_job_failed_http transport=%s tenant_id=%s subject_key=%s job_id=%s detail=%s",
            job_transport,
            job_tenant_id,
            job_subject_key,
            job_id,
            exc.detail,
        )
        failed_jobs = mark_webhook_job_ids_failed(
            session,
            job_ids=failed_job_ids,
            owner_id=owner_id,
            error=str(exc.detail),
        )
        return failed_jobs[0] if failed_jobs else None
    except RetryableWebhookJobError as exc:
        _rollback_job_session(
            session,
            job_transport=job_transport,
            job_tenant_id=job_tenant_id,
            job_subject_key=job_subject_key,
            job_id=job_id,
        )
        logger.warning(
            "webhook_job_requeued transport=%s tenant_id=%s subject_key=%s job_id=%s retry_after_seconds=%s error=%s",
            job_transport,
            job_tenant_id,
            job_subject_key,
            job_id,
            exc.retry_after_seconds,
            str(exc),
        )
        requeued_jobs = requeue_webhook_job_ids(
            session,
            job_ids=failed_job_ids,
            owner_id=owner_id,
            error=str(exc),
            retry_after_seconds=exc.retry_after_seconds,
        )
        return requeued_jobs[0] if requeued_jobs else None
    except Exception as exc:  # noqa: BLE001
        _rollback_job_session(
            session,
            job_transport=job_transport,
            job_tenant_id=job_tenant_id,
            job_subject_key=job_subject_key,
            job_id=job_id,
        )
        logger.exception(
            "webhook_job_failed transport=%s tenant_id=%s subject_key=%s job_id=%s error=%s",
            job_transport,
            job_tenant_id,
            job_subject_key,
            job_id,
            _webhook_failure_message(exc),
        )
        failed_jobs = mark_webhook_job_ids_failed(
            session,
            job_ids=failed_job_ids,
            owner_id=owner_id,
            error=_webhook_failure_message(exc),
        )
        return failed_jobs[0] if failed_jobs else None
