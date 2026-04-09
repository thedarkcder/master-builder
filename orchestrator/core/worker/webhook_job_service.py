from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.commands.entrypoint import execute_tenant_discord_ingress_command
from orchestrator.api.discord.interactions.application import build_default_discord_interaction_dispatch_deps
from orchestrator.api.discord.interactions.dispatcher import dispatch_discord_interaction
from orchestrator.api.discord.shared.state import command_matches
from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
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
from orchestrator.core.deployment_runtime import ingest_coolify_deployment_event
from orchestrator.core.communications import (
    HttpJsonResponseAction,
    HttpJsonResponseBytesAction,
    IngressResult,
    TransportEnvelope,
)
from orchestrator.core.github.transport_executor import GitHubTransportExecutor
from orchestrator.core.project_automation_execution_service import (
    mark_project_automation_execution_failure,
    mark_project_automation_execution_success,
    prepare_project_automation_execution,
)
from orchestrator.core.project_app_artifact_pr_runtime import create_project_app_artifact_pr
from orchestrator.core.project_app_analysis_runtime import run_project_app_analysis
from orchestrator.core.project_app_planner import (
    mark_project_app_analysis_run_completed,
    mark_project_app_analysis_run_failed,
    mark_project_app_analysis_run_running,
    persist_project_app_analysis_result,
)
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_DISCORD_COMMAND,
    WEBHOOK_TRANSPORT_DISCORD_INTERACTION,
    WEBHOOK_TRANSPORT_COOLIFY_DEPLOYMENT,
    WEBHOOK_TRANSPORT_GITHUB,
    WEBHOOK_TRANSPORT_JIRA,
    WEBHOOK_TRANSPORT_PROJECT_AUTOMATION,
    WEBHOOK_TRANSPORT_PROJECT_APP_ANALYSIS,
    WebhookJob,
    claim_next_webhook_job,
    claim_pending_jobs_for_subject,
    mark_webhook_job_ids_failed,
    mark_webhook_jobs_done,
    mark_webhook_jobs_failed,
)
from orchestrator.core.config import Settings
from orchestrator.storage.models import Project, ProjectAppAnalysisRun, Tenant
from orchestrator.storage.models import ProjectApp

logger = logging.getLogger(__name__)


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _coerce_result_payload(run: ProjectAppAnalysisRun) -> dict[str, object]:
    payload = getattr(run, "result_payload", None)
    return payload if isinstance(payload, dict) else {}


def _artifact_pr_context_for_run(run: ProjectAppAnalysisRun) -> tuple[int | None, str | None]:
    payload = _coerce_result_payload(run)
    artifact_pr = payload.get("artifact_pr")
    if not isinstance(artifact_pr, dict):
        return None, None
    pr_number_raw = artifact_pr.get("pr_number")
    pr_number = pr_number_raw if isinstance(pr_number_raw, int) and pr_number_raw > 0 else None
    head_branch = _normalize_optional_string(artifact_pr.get("head_branch"))
    return pr_number, head_branch


def _normalized_app_source_paths_for_run(run: ProjectAppAnalysisRun) -> set[str]:
    payload = _coerce_result_payload(run)
    normalized_apps = payload.get("normalized_apps")
    if not isinstance(normalized_apps, list):
        return set()
    source_paths: set[str] = set()
    for item in normalized_apps:
        if not isinstance(item, dict):
            continue
        source_path = _normalize_optional_string(item.get("source_path"))
        if source_path is None:
            continue
        source_paths.add(source_path)
    return source_paths


def _needs_generated_source_paths_for_run(run: ProjectAppAnalysisRun) -> set[str]:
    payload = _coerce_result_payload(run)
    normalized_apps = payload.get("normalized_apps")
    if not isinstance(normalized_apps, list):
        return set()
    source_paths: set[str] = set()
    for item in normalized_apps:
        if not isinstance(item, dict):
            continue
        source_path = _normalize_optional_string(item.get("source_path"))
        if source_path is None:
            continue
        if bool(item.get("needs_generated_files")):
            source_paths.add(source_path)
    return source_paths


def _latest_completed_analysis_runs_for_project(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
) -> list[ProjectAppAnalysisRun]:
    return session.execute(
        select(ProjectAppAnalysisRun)
        .where(
            ProjectAppAnalysisRun.tenant_id == tenant_id,
            ProjectAppAnalysisRun.project_id == project_id,
            ProjectAppAnalysisRun.status == "completed",
        )
        .order_by(ProjectAppAnalysisRun.completed_at.desc(), ProjectAppAnalysisRun.created_at.desc())
    ).scalars().all()


def _reconcile_project_app_status_after_artifact_pr_merge(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    pr_number: int,
    head_branch: str | None,
) -> int:
    analysis_runs = _latest_completed_analysis_runs_for_project(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
    )
    if not analysis_runs:
        return 0

    matching_run: ProjectAppAnalysisRun | None = None
    for run in analysis_runs:
        run_pr_number, run_head_branch = _artifact_pr_context_for_run(run)
        if run_pr_number == pr_number:
            matching_run = run
            break
        if head_branch and run_head_branch == head_branch:
            matching_run = run
            break
    if matching_run is None:
        return 0

    candidate_source_paths = _needs_generated_source_paths_for_run(matching_run)
    if not candidate_source_paths:
        return 0

    # Guard against stale merge events: only update paths for which this matching run is
    # still the latest completed analysis run mentioning that source path.
    latest_run_by_source_path: dict[str, str] = {}
    for run in analysis_runs:
        run_id = str(run.run_id or "").strip()
        if not run_id:
            continue
        for source_path in _normalized_app_source_paths_for_run(run):
            if source_path not in latest_run_by_source_path:
                latest_run_by_source_path[source_path] = run_id

    eligible_source_paths = {
        source_path
        for source_path in candidate_source_paths
        if latest_run_by_source_path.get(source_path) == matching_run.run_id
    }
    if not eligible_source_paths:
        return 0

    apps = session.execute(
        select(ProjectApp).where(
            ProjectApp.tenant_id == tenant_id,
            ProjectApp.project_id == project_id,
            ProjectApp.source_path.in_(sorted(eligible_source_paths)),
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    updated = 0
    for app in apps:
        if str(app.status or "").strip().lower() != "needs_pr_merge":
            continue
        app.status = "ready"
        app.updated_at = now
        updated += 1
    return updated


def _merged_pull_request_context(
    *,
    github_event: str | None,
    normalized_action: str | None,
    payload: dict[str, object],
    fallback_pr_number: int | None,
) -> tuple[int, str | None] | None:
    if str(github_event or "").strip().lower() != "pull_request":
        return None
    if str(normalized_action or "").strip().lower() != "closed":
        return None
    pull_request = payload.get("pull_request")
    if not isinstance(pull_request, dict):
        return None
    if not bool(pull_request.get("merged")):
        return None
    pr_number_raw = pull_request.get("number")
    if isinstance(pr_number_raw, int) and pr_number_raw > 0:
        pr_number = pr_number_raw
    elif isinstance(fallback_pr_number, int) and fallback_pr_number > 0:
        pr_number = fallback_pr_number
    else:
        return None
    head_branch = None
    head = pull_request.get("head")
    if isinstance(head, dict):
        head_branch = _normalize_optional_string(head.get("ref"))
    return pr_number, head_branch

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
    pr_number = int(context_json.get("pr_number"))
    github_event = str(context_json.get("github_event") or source_job.event_type or "").strip() or None
    normalized_action = str(context_json.get("normalized_action") or "").strip() or None
    payload_json = dict(source_job.payload_json or {})
    merged_context = _merged_pull_request_context(
        github_event=github_event,
        normalized_action=normalized_action,
        payload=payload_json,
        fallback_pr_number=pr_number,
    )
    if merged_context is not None:
        merged_pr_number, merged_head_branch = merged_context
        updated_count = _reconcile_project_app_status_after_artifact_pr_merge(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            pr_number=merged_pr_number,
            head_branch=merged_head_branch,
        )
        logger.info(
            "project_app_artifact_pr_merge_reconciled tenant_id=%s project_id=%s pr_number=%s head_branch=%s updated_app_count=%s",
            tenant.tenant_id,
            project.project_id,
            merged_pr_number,
            merged_head_branch or "none",
            updated_count,
        )
        return mark_webhook_jobs_done(session, jobs=jobs, owner_id=owner_id)

    review_summary_present = bool(context_json.get("review_summary_present"))
    context = GitHubWebhookContext(
        request_id=source_job.request_id,
        delivery_id=str(context_json.get("delivery_id") or source_job.dedupe_key or ""),
        github_event=str(github_event or ""),
        payload=payload_json,
        normalized_action=normalized_action,
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


def _process_coolify_deployment_job(
    *,
    session: Session,
    owner_id: str,
    claimed_job: WebhookJob,
) -> tuple[WebhookJob, ...]:
    if not claimed_job.tenant_id or not claimed_job.project_id:
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error="Coolify deployment webhook job is missing tenant_id or project_id",
        )
    payload = dict(claimed_job.payload_json or {})
    context = dict(claimed_job.context_json or {})
    webhook_token = str(context.get("webhook_token") or context.get("token") or "").strip()
    result = ingest_coolify_deployment_event(
        session=session,
        tenant_id=claimed_job.tenant_id,
        project_id=claimed_job.project_id,
        webhook_token=webhook_token,
        payload=payload,
    )
    if not bool(result.get("ok", False)):
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error="Coolify deployment webhook was not accepted",
        )
    return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)


def _process_project_app_analysis_job(
    *,
    session: Session,
    settings: Settings,
    owner_id: str,
    claimed_job: WebhookJob,
) -> tuple[WebhookJob, ...]:
    if not claimed_job.tenant_id or not claimed_job.project_id:
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error="Project app analysis webhook job is missing tenant_id or project_id",
        )

    payload = dict(claimed_job.payload_json or {})
    analysis_run_id = str(payload.get("analysis_run_id") or "").strip()
    if not analysis_run_id:
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error="Project app analysis webhook job is missing analysis_run_id",
        )

    run = session.get(ProjectAppAnalysisRun, analysis_run_id)
    if run is None or run.tenant_id != claimed_job.tenant_id or run.project_id != claimed_job.project_id:
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error="Project app analysis run was not found",
        )
    if run.status == "completed":
        return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)

    tenant = session.get(Tenant, claimed_job.tenant_id)
    project = session.get(Project, claimed_job.project_id)
    if tenant is None or project is None or not tenant.is_enabled or bool(getattr(project, "is_archived", False)):
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error="Project app analysis context is unavailable",
        )

    request_payload = dict(run.request_payload or {})
    checkout_path = str(payload.get("checkout_path") or request_payload.get("checkout_path") or "").strip()
    if not checkout_path:
        return mark_webhook_jobs_failed(
            session,
            jobs=(claimed_job,),
            owner_id=owner_id,
            error="Project app analysis run is missing checkout_path",
        )
    analysis_source = str(payload.get("analysis_source") or request_payload.get("analysis_source") or "").strip() or None
    planner_version = (
        str(payload.get("planner_version") or request_payload.get("planner_version") or run.planner_version or "").strip()
        or None
    )

    try:
        mark_project_app_analysis_run_running(
            session=session,
            analysis_run_id=analysis_run_id,
        )
        session.commit()

        analysis_result = run_project_app_analysis(
            tenant=tenant,
            project=project,
            checkout_path=checkout_path,
            analysis_source=analysis_source,
            planner_version=planner_version,
            session=session,
            settings=settings,
        )
        persist_project_app_analysis_result(
            session=session,
            tenant_id=claimed_job.tenant_id,
            project_id=claimed_job.project_id,
            analysis_run_id=analysis_run_id,
            apps=analysis_result.apps,
            raw_planner_result_json=analysis_result.metadata.raw_planner_result_json,
            analysis_source=analysis_source,
            planner_version=planner_version,
        )
        artifact_pr_result = create_project_app_artifact_pr(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            checkout_path=checkout_path,
            analysis_run_id=analysis_run_id,
            analysis_result=analysis_result,
        )
        result_payload = {
            "raw_planner_result_json": analysis_result.metadata.raw_planner_result_json,
            "normalized_apps": [app.to_result_json() for app in analysis_result.apps],
            "analysis_source": analysis_source,
            "planner_version": planner_version,
            "pre_scan_count": analysis_result.metadata.pre_scan_count,
            "runtime_count": analysis_result.metadata.runtime_count,
            "normalized_count": analysis_result.metadata.normalized_count,
        }
        if artifact_pr_result is not None:
            result_payload["artifact_pr"] = artifact_pr_result.to_result_json()
        mark_project_app_analysis_run_completed(
            session=session,
            analysis_run_id=analysis_run_id,
            result_payload=result_payload,
        )
        session.commit()
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        try:
            mark_project_app_analysis_run_failed(
                session=session,
                analysis_run_id=analysis_run_id,
                error=str(exc),
            )
            session.commit()
        except Exception:  # noqa: BLE001
            logger.exception(
                "project_app_analysis_run_failure_mark_failed request_id=%s tenant_id=%s job_id=%s",
                claimed_job.request_id,
                claimed_job.tenant_id,
                claimed_job.job_id,
            )
        raise

    return mark_webhook_jobs_done(session, jobs=(claimed_job,), owner_id=owner_id)


def process_next_webhook_job(
    *,
    session: Session,
    settings: Settings,
    owner_id: str,
) -> WebhookJob | None:
    claim = claim_next_webhook_job(
        session,
        owner_id=owner_id,
    )
    if not claim.acquired or claim.job is None:
        return None

    job = claim.job
    job_transport = str(job.transport)
    job_tenant_id = str(job.tenant_id)
    job_subject_key = str(job.subject_key)
    job_id = str(job.job_id)
    failed_job_ids: tuple[str, ...] = (job_id,)
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
            failed_job_ids = (job_id, *(str(item.job_id) for item in additional_jobs))
            processed = _process_jira_subject_jobs(
                session=session,
                settings=settings,
                owner_id=owner_id,
                claimed_job=job,
                related_jobs=additional_jobs,
            )
        elif job.transport == WEBHOOK_TRANSPORT_GITHUB:
            additional_jobs = claim_pending_jobs_for_subject(
                session,
                transport=WEBHOOK_TRANSPORT_GITHUB,
                subject_key=job.subject_key,
                owner_id=owner_id,
                exclude_job_id=job.job_id,
            )
            failed_job_ids = (job_id, *(str(item.job_id) for item in additional_jobs))
            processed = _process_github_subject_jobs(
                session=session,
                settings=settings,
                owner_id=owner_id,
                claimed_job=job,
                related_jobs=additional_jobs,
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
        elif job.transport == WEBHOOK_TRANSPORT_COOLIFY_DEPLOYMENT:
            processed = _process_coolify_deployment_job(
                session=session,
                owner_id=owner_id,
                claimed_job=job,
            )
        elif job.transport == WEBHOOK_TRANSPORT_PROJECT_APP_ANALYSIS:
            processed = _process_project_app_analysis_job(
                session=session,
                settings=settings,
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
            exc,
        )
        failed_jobs = mark_webhook_job_ids_failed(
            session,
            job_ids=failed_job_ids,
            owner_id=owner_id,
            error=str(exc),
        )
        return failed_jobs[0] if failed_jobs else None
