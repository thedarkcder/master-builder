from __future__ import annotations

from dataclasses import dataclass
import json
from uuid import uuid4

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.webhooks.payload_utils import read_json_payload
from orchestrator.api.webhooks.contracts import (
    extract_delivery_id,
    extract_installation_id,
    extract_push_deployment_source,
    extract_pull_request_targets,
    extract_repository_full_name,
    find_tenant_by_installation_id,
    resolve_active_project_for_repo,
    resolve_global_github_webhook_secret,
    resolve_tenant_github_webhook_secret,
    validate_github_webhook_signature,
)
from orchestrator.core.communications.integration_contracts import TransportActionExecutor
from orchestrator.core.github.transport_executor import GitHubTransportExecutor
from orchestrator.core.platform.secret_service import resolve_platform_secret_ref
from orchestrator.core.platform.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.projects.policy import resolve_effective_policy
from orchestrator.core.review.reviewer import ReviewAgentGate
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.models import Run, WorkflowExecution
from orchestrator.tools.github_app import github_client_from_tenant_config


@dataclass(frozen=True)
class GitHubWebhookContext:
    request_id: str
    delivery_id: str
    github_event: str
    payload: dict
    normalized_action: str | None
    installation_id: int
    tenant: object
    project: object
    repo_full_name: str
    pr_targets: list[tuple[int, bool]]
    ref_name: str | None = None


@dataclass(frozen=True)
class GitHubWebhookPreparedRuntime:
    context: GitHubWebhookContext
    github_client: object | None
    reviewer_gate: ReviewAgentGate | None
    review_runtime_error: str | None = None
    transport_action_executors: tuple[TransportActionExecutor, ...] = ()



def github_response(*, status_code: int, **content) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=content)


async def resolve_github_webhook_context(*, request: Request, session, settings, request_id: str, logger) -> GitHubWebhookContext | JSONResponse:
    delivery_id = extract_delivery_id(request) or str(uuid4())
    github_event = (request.headers.get("X-GitHub-Event") or "").strip().lower()

    logger.info(
        "github_webhook_received request_id=%s delivery_id=%s event=%s",
        request_id,
        delivery_id,
        github_event or "unknown",
    )

    payload, payload_bytes = await read_json_payload(request, request_id=request_id, source="github")
    global_secret = resolve_global_github_webhook_secret(
        request_id=request_id,
        session=session,
        settings=settings,
    )
    if global_secret is not None:
        validate_github_webhook_signature(
            request=request,
            payload_bytes=payload_bytes,
            shared_secret=global_secret,
            request_id=request_id,
            tenant_id=None,
        )

    if github_event == "ping":
        return github_response(
            status_code=status.HTTP_200_OK,
            request_id=request_id,
            delivery_id=delivery_id,
            event=github_event,
            accepted=True,
            reason="ping",
        )

    installation_id = extract_installation_id(payload)
    if installation_id is None:
        logger.warning(
            "github_webhook_invalid_payload request_id=%s delivery_id=%s reason=missing_installation_id",
            request_id,
            delivery_id,
        )
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing installation identifier")

    tenant = find_tenant_by_installation_id(session, installation_id=installation_id)
    if tenant is None:
        logger.warning(
            "github_webhook_unknown_installation request_id=%s delivery_id=%s installation_id=%s",
            request_id,
            delivery_id,
            installation_id,
        )
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            event=github_event,
            installation_id=installation_id,
            accepted=False,
            reason="unknown_installation",
        )

    if not tenant.is_enabled:
        logger.info(
            "github_webhook_ignored request_id=%s delivery_id=%s tenant_id=%s reason=tenant_disabled",
            request_id,
            delivery_id,
            tenant.tenant_id,
        )
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            accepted=False,
            reason="tenant_disabled",
        )

    if global_secret is None:
        tenant_secret = resolve_tenant_github_webhook_secret(
            tenant=tenant,
            request_id=request_id,
            session=session,
            settings=settings,
        )
        if tenant_secret is not None:
            validate_github_webhook_signature(
                request=request,
                payload_bytes=payload_bytes,
                shared_secret=tenant_secret,
                request_id=request_id,
                tenant_id=tenant.tenant_id,
            )

    action = payload.get("action")
    normalized_action = action.strip() if isinstance(action, str) else None
    logger.info(
        "github_webhook_accepted request_id=%s delivery_id=%s tenant_id=%s event=%s action=%s installation_id=%s",
        request_id,
        delivery_id,
        tenant.tenant_id,
        github_event or "unknown",
        normalized_action or "none",
        installation_id,
    )

    review_events = {
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "issue_comment",
        "check_suite",
        "check_run",
    }
    deployment_events = {"push"}
    if github_event not in review_events | deployment_events:
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            action=normalized_action,
            accepted=True,
            reason="ignored_event",
        )

    repo_full_name = extract_repository_full_name(payload)
    pr_targets = extract_pull_request_targets(payload)
    ref_name = str(payload.get("ref") or "").strip() or None
    push_source = extract_push_deployment_source(payload) if github_event == "push" else None
    if repo_full_name is None or (github_event in review_events and not pr_targets):
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            action=normalized_action,
            accepted=False,
            reason="missing_pr_context",
        )
    if github_event == "push" and push_source is None:
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            action=normalized_action,
            accepted=False,
            reason="missing_push_deployment_source",
        )

    project = resolve_active_project_for_repo(
        session=session,
        tenant_id=tenant.tenant_id,
        repo_full_name=repo_full_name,
    )
    if project is None:
        return github_response(
            status_code=status.HTTP_202_ACCEPTED,
            request_id=request_id,
            delivery_id=delivery_id,
            tenant_id=tenant.tenant_id,
            event=github_event,
            action=normalized_action,
            accepted=False,
            reason="project_not_mapped",
            repository=repo_full_name,
        )

    return GitHubWebhookContext(
        request_id=request_id,
        delivery_id=delivery_id,
        github_event=github_event,
        payload=payload,
        normalized_action=normalized_action,
        installation_id=installation_id,
        tenant=tenant,
        project=project,
        repo_full_name=repo_full_name,
        pr_targets=pr_targets,
        ref_name=ref_name,
    )



def build_github_review_runtime(*, session, settings, tenant, project):
    github_client = github_client_from_tenant_config(
        tenant.github_config,
        tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
        ),
        platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        ),
    )
    effective_policy = resolve_effective_policy(
        tenant_policy=dict(getattr(tenant, "policy_config", {}) or {}),
        project_overrides=dict(getattr(project, "policy_overrides", {}) or {}),
        default_codex_model=getattr(settings, "codex_model", None),
        default_codex_reasoning_effort=getattr(settings, "codex_reasoning_effort", None),
    )
    reviewer_gate = ReviewAgentGate(
        github_client,
        require_demo_evidence=bool(effective_policy.get("qa_demo_recording_enabled")),
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        demo_artifact_public_base_url=getattr(settings, "qa_demo_artifact_public_base_url", None),
        demo_evidence_run_id_resolver=lambda pr_url: _latest_run_id_for_pr_url(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            pr_url=pr_url,
        ),
        demo_evidence_recordings_resolver=lambda run_id: _qa_demo_recordings_for_run(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            run_id=run_id,
        ),
        demo_proof_status_resolver=lambda pr_url, _head_sha: _qa_demo_proof_status_for_pr_url(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            pr_url=pr_url,
        ),
    )
    return github_client, reviewer_gate


def _latest_run_id_for_pr_url(
    *,
    session,
    tenant_id: str,
    project_id: str,
    pr_url: str | None,
) -> str | None:
    normalized_pr_url = str(pr_url or "").strip()
    if not normalized_pr_url:
        return None
    statement = (
        select(Run.run_id)
        .where(
            Run.tenant_id == tenant_id,
            Run.project_id == project_id,
            Run.pr_url == normalized_pr_url,
        )
        .order_by(Run.created_at.desc(), Run.run_id.desc())
        .limit(1)
    )
    return session.execute(statement).scalar_one_or_none()


def _qa_demo_recordings_for_run(
    *,
    session,
    tenant_id: str,
    project_id: str,
    run_id: str | None,
) -> tuple[dict[str, str], ...] | None:
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        return None
    statement = select(Run.status, Run.plan).where(
        Run.tenant_id == tenant_id,
        Run.project_id == project_id,
        Run.run_id == normalized_run_id,
    )
    row = session.execute(statement).one_or_none()
    if row is None:
        return None
    run_status, run_plan = row
    if str(run_status or "").strip() not in {"running", "succeeded"}:
        return None
    snapshot = ExecutionSnapshot.load(run_plan)
    if snapshot is None:
        return None
    qa_result = snapshot.qa_result()
    if qa_result is None or qa_result.outcome != "continue" or not qa_result.recordings:
        return None
    return tuple(
        {
            "name": recording.name,
            "artifact_url": recording.artifact_url,
            "object_key": recording.object_key,
            "capture_target": recording.capture_target,
            "capture_reference": recording.capture_reference,
            "content_sha256": recording.content_sha256,
            "release_commit_sha": recording.release_commit_sha,
            "release_context_sha256": recording.release_context_sha256,
        }
        for recording in qa_result.recordings
    )


def _qa_demo_proof_status_for_pr_url(
    *,
    session,
    tenant_id: str,
    project_id: str,
    pr_url: str | None,
) -> dict[str, object] | None:
    normalized_pr_url = str(pr_url or "").strip()
    if not normalized_pr_url:
        return None
    statement = (
        select(
            WorkflowExecution.workflow_id,
            WorkflowExecution.status,
            WorkflowExecution.source_description,
            WorkflowExecution.updated_at,
        )
        .where(
            WorkflowExecution.tenant_id == tenant_id,
            WorkflowExecution.project_id == project_id,
            WorkflowExecution.workflow_type_key == "demo_proof",
        )
        .order_by(WorkflowExecution.updated_at.desc(), WorkflowExecution.workflow_id.desc())
    )
    for workflow_id, workflow_status, raw_description, updated_at in session.execute(statement):
        description = _decode_workflow_description(raw_description)
        if str(description.get("pr_url") or "").strip() != normalized_pr_url:
            continue
        events = [str(event or "").strip() for event in list(description.get("demo_proof_events") or [])]
        terminal_event = events[-1] if events else ""
        release_metadata = _demo_proof_event_metadata(description, "ReleaseLive")
        pr_evidence_metadata = _demo_proof_event_metadata(description, "PREvidenceAttached")
        return {
            "workflow_id": workflow_id,
            "status": str(workflow_status or "").strip(),
            "demo_proof_state": str(description.get("demo_proof_state") or "").strip(),
            "terminal_event": terminal_event,
            "release_commit_sha": str(release_metadata.get("release_commit_sha") or "").strip().lower(),
            "artifact_url_check_status": str(pr_evidence_metadata.get("artifact_url_check_status") or "").strip(),
            "checked_artifact_urls": tuple(
                str(url or "").strip()
                for url in list(pr_evidence_metadata.get("checked_artifact_urls") or [])
                if str(url or "").strip()
            ),
            "updated_at": updated_at,
        }
    return None


def _decode_workflow_description(raw_description: object) -> dict[str, object]:
    if isinstance(raw_description, dict):
        return dict(raw_description)
    normalized = str(raw_description or "").strip()
    if not normalized:
        return {}
    try:
        parsed = json.loads(normalized)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _demo_proof_event_metadata(description: dict[str, object], event_name: str) -> dict[str, object]:
    for entry in list(description.get("demo_proof_event_metadata") or []):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("event") or "").strip() != event_name:
            continue
        metadata = entry.get("metadata")
        return dict(metadata) if isinstance(metadata, dict) else {}
    return {}


async def prepare_github_webhook_runtime(
    *,
    request: Request,
    session: Session,
    settings,  # noqa: ANN001
    request_id: str,
    logger,
) -> GitHubWebhookPreparedRuntime | JSONResponse:
    context = await resolve_github_webhook_context(
        request=request,
        session=session,
        settings=settings,
        request_id=request_id,
        logger=logger,
    )
    if isinstance(context, JSONResponse):
        return context
    try:
        github_client, reviewer_gate = build_github_review_runtime(
            session=session,
            settings=settings,
            tenant=context.tenant,
            project=context.project,
        )
    except ValueError as exc:
        return GitHubWebhookPreparedRuntime(
            context=context,
            github_client=None,
            reviewer_gate=None,
            review_runtime_error=str(exc),
        )
    return GitHubWebhookPreparedRuntime(
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
