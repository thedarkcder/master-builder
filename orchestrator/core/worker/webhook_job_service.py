from __future__ import annotations

import asyncio
import logging

from fastapi import HTTPException

from orchestrator.api.commands.entrypoint import execute_tenant_discord_ingress_command
from orchestrator.api.discord.interactions.application import build_default_discord_interaction_dispatch_deps
from orchestrator.api.discord.interactions.dispatcher import dispatch_discord_interaction
from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.api.schemas import DiscordCommandRequest
from orchestrator.api.transport_runtime import (
    build_http_transport_action_executors,
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
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_DISCORD_COMMAND,
    WEBHOOK_TRANSPORT_DISCORD_INTERACTION,
    WEBHOOK_TRANSPORT_GITHUB,
    WEBHOOK_TRANSPORT_JIRA,
    WebhookJob,
    claim_next_webhook_job,
    claim_pending_jobs_for_subject,
    mark_webhook_jobs_done,
    mark_webhook_jobs_failed,
)
from orchestrator.core.runs import cancel_queued_issue_runs
from orchestrator.storage.models import Project, Tenant

logger = logging.getLogger(__name__)

_STALE_QUEUE_BLOCKING_REASONS = {
    "decision_gate_required",
    "gtd_required",
    "missing_ready_label",
    "issue_done",
    "issue_in_backlog",
    "ready_for_agent_backlog",
}


def _non_http_ingress_result(result: IngressResult) -> IngressResult:
    return IngressResult(
        actions=tuple(
            action
            for action in result.actions
            if not isinstance(action, (HttpJsonResponseAction, HttpJsonResponseBytesAction))
        ),
    )


def _refresh_jira_context_from_live_issue(*, context, session, settings) -> None:  # noqa: ANN001
    oauth = tenant_jira_oauth_context(session=session, tenant=context.tenant, settings=settings)
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


def _cancel_stale_queued_runs_if_blocked(*, session, context, response_content: dict[str, object]) -> None:  # noqa: ANN001
    if bool(response_content.get("enqueued")):
        return
    reason = str(response_content.get("reason") or "").strip()
    if reason not in _STALE_QUEUE_BLOCKING_REASONS:
        return
    cancel_queued_issue_runs(
        session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        cancelled_by=f"jira_webhook:{reason}",
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
    session,
    settings,  # noqa: ANN001
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
        _cancel_stale_queued_runs_if_blocked(
            session=session,
            context=pending_issue_context,
            response_content=plan.content,
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


def _process_github_job(
    *,
    session,
    settings,  # noqa: ANN001
    owner_id: str,
    claimed_job: WebhookJob,
) -> tuple[WebhookJob, ...]:
    context_json = dict(claimed_job.context_json or {})
    tenant = session.get(Tenant, context_json.get("tenant_id"))
    project = session.get(Project, context_json.get("project_id"))
    if tenant is None or project is None or not tenant.is_enabled or bool(getattr(project, "is_archived", False)):
        return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)
    pr_number = int(context_json.get("pr_number"))
    review_summary_present = bool(context_json.get("review_summary_present"))
    context = GitHubWebhookContext(
        request_id=claimed_job.request_id,
        delivery_id=str(context_json.get("delivery_id") or claimed_job.dedupe_key or ""),
        github_event=str(context_json.get("github_event") or claimed_job.event_type or ""),
        payload=dict(claimed_job.payload_json or {}),
        normalized_action=str(context_json.get("normalized_action") or "").strip() or None,
        installation_id=int(context_json.get("installation_id") or 0),
        tenant=tenant,
        project=project,
        repo_full_name=str(context_json.get("repo_full_name") or ""),
        pr_targets=[(pr_number, review_summary_present)],
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
            request_id=claimed_job.request_id,
            session=session,
            settings=settings,
        )
    )
    execute_side_effect_ingress_result(
        result=_non_http_ingress_result(result),
        envelope=TransportEnvelope(
            transport=WEBHOOK_TRANSPORT_GITHUB,
            event_type=str(claimed_job.event_type or "github_webhook"),
            request_id=claimed_job.request_id,
            tenant_id_hint=claimed_job.tenant_id,
            delivery_id=claimed_job.dedupe_key,
        ),
        transport_action_executors=build_http_transport_action_executors(
            session=session,
            settings=settings,
            extra_transport_action_executors=tuple(getattr(prepared_runtime, "transport_action_executors", ()) or ()),
        ),
    )
    return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)


def _process_discord_command_job(
    *,
    session,
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
        defer_seed_issues=bool((claimed_job.context_json or {}).get("defer_seed_issues")),
    )
    return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)


async def _run_discord_interaction_job_async(*, session, claimed_job: WebhookJob) -> None:  # noqa: ANN001
    queued: list[object] = []

    def _capture(awaitable) -> object:  # noqa: ANN001
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
    session,
    owner_id: str,
    claimed_job: WebhookJob,
) -> tuple[WebhookJob, ...]:
    asyncio.run(_run_discord_interaction_job_async(session=session, claimed_job=claimed_job))
    return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)


def process_next_webhook_job(
    *,
    session,
    settings,  # noqa: ANN001
    owner_id: str,
) -> WebhookJob | None:
    claim = claim_next_webhook_job(
        session,
        owner_id=owner_id,
    )
    if not claim.acquired or claim.job is None:
        return None

    job = claim.job
    additional_jobs: tuple[WebhookJob, ...] = ()
    try:
        if job.transport == WEBHOOK_TRANSPORT_JIRA:
            additional_jobs = claim_pending_jobs_for_subject(
                session,
                transport=WEBHOOK_TRANSPORT_JIRA,
                subject_key=job.subject_key,
                owner_id=owner_id,
                exclude_job_id=job.job_id,
            )
            processed = _process_jira_subject_jobs(
                session=session,
                settings=settings,
                owner_id=owner_id,
                claimed_job=job,
                related_jobs=additional_jobs,
            )
        elif job.transport == WEBHOOK_TRANSPORT_GITHUB:
            processed = _process_github_job(
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
        logger.warning(
            "webhook_job_failed_http transport=%s tenant_id=%s subject_key=%s job_id=%s detail=%s",
            job.transport,
            job.tenant_id,
            job.subject_key,
            job.job_id,
            exc.detail,
        )
        failed_jobs = (job, *additional_jobs)
        return mark_webhook_jobs_failed(
            session,
            jobs=failed_jobs,
            owner_id=owner_id,
            error=str(exc.detail),
        )[0]
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "webhook_job_failed transport=%s tenant_id=%s subject_key=%s job_id=%s error=%s",
            job.transport,
            job.tenant_id,
            job.subject_key,
            job.job_id,
            exc,
        )
        failed_jobs = (job, *additional_jobs)
        return mark_webhook_jobs_failed(
            session,
            jobs=failed_jobs,
            owner_id=owner_id,
            error=str(exc),
        )[0]
