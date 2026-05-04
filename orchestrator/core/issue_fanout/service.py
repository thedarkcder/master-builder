from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status

from orchestrator.core.issue_fanout.draft_assembly import (
    parse_engineering_seed_drafts,
    parse_parent_seed_drafts,
)
from orchestrator.core.projects.architecture_document_service import (
    ArchitectureDocumentGate,
    ArchitectureDocumentLink,
    ArchitectureDocumentService,
    architecture_required_for_issue,
)
from orchestrator.core.observability.audit import record_audit_event
from orchestrator.core.issue_fanout.formatting import build_issue_url_list, format_issue_markdown_list
from orchestrator.core.integrations.atlassian.links import architecture_document_remote_link_spec
from orchestrator.core.runtime.invocation import AgentInvocationContext
from orchestrator.core.workflow.attempt_ref import WorkflowAttemptRef
from orchestrator.core.workflow.operation_logging import emit_workflow_operation_log
from orchestrator.storage.models import Project, Tenant, WorkflowOperation
from orchestrator.tools.atlassian_oauth import JiraIssueCreateInput, JiraIssuePreview, AtlassianOAuthError

SEED_FOLLOWUP_CONTEXT_MAX_AGE = timedelta(hours=24)
_PM_PARENT_LABEL = "pm-parent"


def _parent_label(parent_issue_key: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(parent_issue_key or "").strip().lower()).strip("-")
    return f"parent-{normalized[:64]}" if normalized else "parent"


def _project_for_seed_or_404(
    *,
    session,
    tenant: Tenant,
    project_id: str | None,
) -> Project:
    normalized_project_id = str(project_id or "").strip()
    if not normalized_project_id:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Issue seeding requires a scoped project")
    project = session.get(Project, normalized_project_id)
    if project is None or project.tenant_id != tenant.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Project not found")
    return project


def _architecture_gate_for_parent_issue(
    *,
    session,
    tenant: Tenant,
    project_id: str | None,
    parent_issue_key: str,
    issue_summary: str,
    issue_labels: list[str],
    actor: str | None,
    settings_factory,
) -> tuple[ArchitectureDocumentGate, ArchitectureDocumentLink | None]:
    project = _project_for_seed_or_404(session=session, tenant=tenant, project_id=project_id)
    service = ArchitectureDocumentService(settings_factory=settings_factory)
    gate = service.resolve_gate(
        session=session,
        project=project,
        parent_issue_key=parent_issue_key,
        issue_summary=issue_summary,
        issue_labels=issue_labels,
        actor=actor,
    )
    if gate.document is None:
        return gate, None
    normalized_url = str(gate.document.canonical_url or "").strip()
    if not normalized_url:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Architecture document '{gate.document.title}' is missing its canonical URL",
        )
    return gate, ArchitectureDocumentLink(title=gate.document.title, url=normalized_url)


def _assert_stage_spi_allows_parent_seed(
    *,
    session,
    tenant: Tenant,
    scoped_project_id: str | None,
    settings: object,
    pm_interview_notes_json: dict | None,
) -> None:
    from orchestrator.core.stages.spi_policy import resolve_stage_spi_enabled

    if not resolve_stage_spi_enabled(
        session=session,
        settings=settings,
        tenant_id=str(tenant.tenant_id),
        project_id=scoped_project_id,
    ):
        return
    if not isinstance(pm_interview_notes_json, dict):
        return
    spi = pm_interview_notes_json.get("stage_spi")
    if not isinstance(spi, dict):
        return
    if bool(spi.get("stage_ready_for_implementation")):
        return
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Stage plugin has not approved implementation seeding (stage_ready_for_implementation is false)",
    )


def _resolve_seed_oauth_context(*, session, tenant: Tenant, settings, tenant_atlassian_oauth_context_fn):  # noqa: ANN001
    try:
        oauth = tenant_atlassian_oauth_context_fn(session=session, tenant=tenant, settings=settings)
    except (ValueError, AtlassianOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to seed Jira issues: {exc}",
        ) from exc

    if not isinstance(oauth, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to seed Jira issues: Atlassian context is incomplete",
        )
    client = oauth.get("client")
    access_token = str(oauth.get("access_token") or "").strip()
    connection = oauth.get("connection")
    cloud_id = str(getattr(connection, "cloud_id", "") or "").strip()
    if client is None or not access_token or connection is None or not cloud_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to seed Jira issues: Atlassian context is incomplete",
        )
    return {
        "client": client,
        "access_token": access_token,
        "connection": connection,
        "cloud_id": cloud_id,
    }


def validate_seed_followup_context(
    *,
    session,
    tenant: Tenant,
    context: dict,
    get_settings_fn,
    tenant_atlassian_oauth_context_fn,
) -> tuple[bool, str | None]:  # noqa: ANN001
    updated_at_raw = str(context.get("updated_at") or "").strip()
    if updated_at_raw:
        try:
            updated_at = datetime.fromisoformat(updated_at_raw)
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)) > SEED_FOLLOWUP_CONTEXT_MAX_AGE:
                return False, "stale follow-up context"
        except ValueError:
            return False, "invalid follow-up context timestamp"

    issue_keys = [str(value).strip().upper() for value in context.get("issue_keys", []) if str(value).strip()]
    if not issue_keys:
        return True, None
    try:
        settings = get_settings_fn()
        oauth = tenant_atlassian_oauth_context_fn(session=session, tenant=tenant, settings=settings)
        escaped_keys = ", ".join(f'"{value.replace(chr(34), "").strip()}"' for value in issue_keys)
        existing = oauth["client"].search_issues_by_jql(
            access_token=oauth["access_token"],
            cloud_id=oauth["connection"].cloud_id,
            jql=f"issuekey in ({escaped_keys})",
            max_results=min(len(issue_keys), 50),
        )
    except (HTTPException, ValueError, AtlassianOAuthError):
        return True, None
    existing_keys = {str(issue.key).strip().upper() for issue in existing if str(issue.key).strip()}
    if not existing_keys:
        return False, "referenced Jira issues no longer exist"
    return True, None


def _project_issue_catalog(*, oauth: dict[str, Any], project_key: str) -> list[JiraIssuePreview]:
    return oauth["client"].search_issues_by_jql(
        access_token=oauth["access_token"],
        cloud_id=oauth["cloud_id"],
        jql=f'project = "{project_key}" ORDER BY updated DESC',
        max_results=100,
    )


def _child_issue_catalog(*, oauth: dict[str, Any], project_key: str, parent_issue_key: str) -> list[JiraIssuePreview]:
    parent_label = _parent_label(parent_issue_key)
    queries = [
        f'project = "{project_key}" AND parent = "{parent_issue_key}" ORDER BY updated DESC',
        f'project = "{project_key}" AND labels = "{parent_label}" ORDER BY updated DESC',
    ]
    matched: dict[str, JiraIssuePreview] = {}
    for query in queries:
        issues = oauth["client"].search_issues_by_jql(
            access_token=oauth["access_token"],
            cloud_id=oauth["cloud_id"],
            jql=query,
            max_results=100,
        )
        for issue in issues:
            if issue.key not in matched:
                matched[issue.key] = issue
    return list(matched.values())


def list_child_issue_previews_for_parent(
    *,
    oauth: dict[str, Any],
    project_key: str,
    parent_issue_key: str,
) -> list[JiraIssuePreview]:
    return _child_issue_catalog(
        oauth=oauth,
        project_key=project_key,
        parent_issue_key=parent_issue_key,
    )


def _project_available_issue_types(*, oauth: dict[str, Any], project_key: str) -> list[str]:
    client = oauth.get("client")
    list_issue_types = getattr(client, "list_project_issue_types_for_create", None)
    if not callable(list_issue_types):
        raise AtlassianOAuthError("Atlassian client does not support Jira issue type discovery")
    payload = list_issue_types(
        access_token=oauth["access_token"],
        cloud_id=oauth["cloud_id"],
        project_key=project_key,
    )
    issue_types = [str(value).strip() for value in payload if str(value).strip()]
    if not issue_types:
        raise AtlassianOAuthError(f"Jira issue type discovery returned no issue types for project {project_key}")
    return issue_types


def _project_issue_types_by_key(*, oauth: dict[str, Any], project_keys: list[str]) -> dict[str, list[str]]:
    issue_types_by_key: dict[str, list[str]] = {}
    for project_key in project_keys:
        normalized_project_key = str(project_key).strip().upper()
        if not normalized_project_key:
            continue
        issue_types_by_key[normalized_project_key] = _project_available_issue_types(
            oauth=oauth,
            project_key=normalized_project_key,
        )
    if not issue_types_by_key:
        raise AtlassianOAuthError("Jira issue type discovery returned no project issue type catalogs")
    return issue_types_by_key


def _require_available_issue_type(*, issue_type: str, available_issue_types: list[str], summary: str) -> str:
    normalized_issue_type = str(issue_type or "").strip()
    if not normalized_issue_type:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Issue '{summary}' is missing Jira issue_type",
        )
    by_exact = {str(value).strip(): str(value).strip() for value in available_issue_types if str(value).strip()}
    if normalized_issue_type in by_exact:
        return by_exact[normalized_issue_type]
    normalized_alias = normalized_issue_type.replace("-", "").replace(" ", "").casefold()
    if normalized_alias == "subtask":
        for available_issue_type in available_issue_types:
            available_value = str(available_issue_type).strip()
            if available_value.replace("-", "").replace(" ", "").casefold() == "subtask":
                return available_value
    available = ", ".join(available_issue_types)
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"Issue '{summary}' requested unavailable Jira issue_type '{normalized_issue_type}'; available issue types: {available}",
    )


def _matching_subtask_issue_type(*, available_issue_types: list[str]) -> str | None:
    for available_issue_type in available_issue_types:
        available_value = str(available_issue_type).strip()
        if available_value.replace("-", "").replace(" ", "").casefold() == "subtask":
            return available_value
    return None


def _engineering_child_issue_type_for_parent(
    *,
    parent_issue_type: str,
    available_issue_types: list[str],
    parent_issue_key: str,
) -> str:
    normalized_parent_type = str(parent_issue_type or "").strip()
    if not normalized_parent_type:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot determine Jira parent issue type for {parent_issue_key}",
        )
    if normalized_parent_type.casefold() == "epic":
        return _require_available_issue_type(
            issue_type="Story",
            available_issue_types=available_issue_types,
            summary=f"engineering child for epic {parent_issue_key}",
        )
    if normalized_parent_type.casefold() != "story":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Parent issue {parent_issue_key} is {normalized_parent_type}; "
                "engineering child fanout supports Epic -> Story and Story -> Sub-task only"
            ),
        )
    subtask_issue_type = _matching_subtask_issue_type(available_issue_types=available_issue_types)
    if subtask_issue_type is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Story parent {parent_issue_key} requires a Jira subtask issue type",
        )
    return subtask_issue_type


def _actual_parent_issue_type(
    *,
    oauth: dict[str, Any],
    parent_issue_key: str,
    created_parent_issue_type: str | None,
    parent_created: bool,
) -> str:
    if parent_created and str(created_parent_issue_type or "").strip():
        return str(created_parent_issue_type).strip()
    detail = oauth["client"].get_issue_detail(
        access_token=oauth["access_token"],
        cloud_id=oauth["cloud_id"],
        issue_id_or_key=parent_issue_key,
    )
    if not detail.issue_type:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Jira issue {parent_issue_key} did not return an issue type",
        )
    return detail.issue_type


def _record_seed_operation_event(
    *,
    session,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str | None,
    operation_id: str | None,
    issue_key: str | None,
    attempt_ref: WorkflowAttemptRef | None,
    event_kind: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_workflow_id = str(workflow_id or "").strip() or None
    normalized_operation_id = attempt_ref.require_operation_id() if attempt_ref is not None else None
    explicit_operation_id = str(operation_id or "").strip() or None
    if explicit_operation_id is not None and explicit_operation_id != normalized_operation_id:
        raise ValueError("Jira seed operation event operation_id does not match attempt_ref")
    if not normalized_tenant_id or not normalized_workflow_id or not normalized_operation_id:
        return
    if attempt_ref is None:
        raise ValueError("Jira seed operation events require attempt_ref")
    normalized_attempt_id = attempt_ref.require_attempt_id()
    operation = session.get(WorkflowOperation, normalized_operation_id)
    if operation is None:
        raise ValueError(f"Jira seed operation event references missing operation {normalized_operation_id}")
    event_payload = {"attempt": attempt_ref.number, **dict(payload or {})}
    record_audit_event(
        session,
        tenant_id=normalized_tenant_id,
        project_id=str(project_id or "").strip() or None,
        workflow_id=normalized_workflow_id,
        run_id=operation.run_id,
        operation_id=normalized_operation_id,
        attempt_id=normalized_attempt_id,
        issue_key=str(issue_key or "").strip() or None,
        actor_type="agent",
        actor_id="system",
        source_component="issue_seed_service",
        event_kind=event_kind,
        level="info",
        message=message,
        payload=event_payload,
    )
    emit_workflow_operation_log(
        session,
        operation=operation,
        attempt_ref=attempt_ref,
        event_type=event_kind,
        message=message,
        metadata={
            **event_payload,
            "attempt_id": normalized_attempt_id,
            "attempt_number": attempt_ref.number,
        },
    )


def _upsert_issue(
    *,
    oauth: dict[str, Any],
    project_key: str,
    issue_input: JiraIssueCreateInput,
    requested_issue_key: str | None,
    allow_create: bool,
    existing_issues: list[JiraIssuePreview],
    matched_issue_keys: set[str],
    select_seed_match_fn,
) -> tuple[str | None, bool, bool]:  # noqa: ANN001
    matched = select_seed_match_fn(
        existing_issues=existing_issues,
        summary=issue_input.summary,
        requested_issue_key=requested_issue_key,
        matched_issue_keys=matched_issue_keys,
    )
    if matched is not None:
        matched_issue_keys.add(matched.key)
        oauth["client"].update_issue_fields(
            access_token=oauth["access_token"],
            cloud_id=oauth["cloud_id"],
            issue_id_or_key=matched.key,
            summary=issue_input.summary,
            description=issue_input.description,
            labels=issue_input.labels,
        )
        return matched.key, False, True
    if not allow_create:
        return None, False, False
    created = oauth["client"].create_issue(
        access_token=oauth["access_token"],
        cloud_id=oauth["cloud_id"],
        project_key=project_key,
        issue=issue_input,
    )
    return created.key, True, False


def _upsert_parent_issue(
    *,
    oauth: dict[str, Any],
    project_key: str,
    issue_input: JiraIssueCreateInput,
    requested_issue_key: str | None,
    allow_create: bool,
    existing_issues: list[JiraIssuePreview],
    matched_issue_keys: set[str],
    select_seed_match_fn,
) -> tuple[str | None, bool, bool]:  # noqa: ANN001
    matched = select_seed_match_fn(
        existing_issues=existing_issues,
        summary=issue_input.summary,
        requested_issue_key=requested_issue_key,
        matched_issue_keys=matched_issue_keys,
    )
    if matched is not None:
        matched_issue_keys.add(matched.key)
        oauth["client"].update_issue_summary(
            access_token=oauth["access_token"],
            cloud_id=oauth["cloud_id"],
            issue_id_or_key=matched.key,
            summary=issue_input.summary,
        )
        oauth["client"].replace_issue_labels(
            access_token=oauth["access_token"],
            cloud_id=oauth["cloud_id"],
            issue_id_or_key=matched.key,
            labels=issue_input.labels,
        )
        return matched.key, False, True
    if not allow_create:
        return None, False, False
    created = oauth["client"].create_issue(
        access_token=oauth["access_token"],
        cloud_id=oauth["cloud_id"],
        project_key=project_key,
        issue=issue_input,
    )
    return created.key, True, False


def _update_parent_issue_metadata(
    *,
    oauth: dict[str, Any],
    issue_key: str,
    summary: str,
    labels: list[str],
) -> None:
    oauth["client"].update_issue_summary(
        access_token=oauth["access_token"],
        cloud_id=oauth["cloud_id"],
        issue_id_or_key=issue_key,
        summary=summary,
    )
    oauth["client"].replace_issue_labels(
        access_token=oauth["access_token"],
        cloud_id=oauth["cloud_id"],
        issue_id_or_key=issue_key,
        labels=labels,
    )


def _upsert_architecture_document_remote_link(
    *,
    oauth: dict[str, Any],
    issue_key: str,
    architecture_link: ArchitectureDocumentLink | None,
) -> None:
    if architecture_link is None:
        return
    spec = architecture_document_remote_link_spec(
        issue_key=issue_key,
        title=architecture_link.title,
        url=architecture_link.url,
    )
    oauth["client"].upsert_remote_issue_link(
        access_token=oauth["access_token"],
        cloud_id=oauth["cloud_id"],
        issue_id_or_key=issue_key,
        global_id=spec.global_id,
        relationship=spec.relationship,
        title=spec.title,
        url=spec.url,
    )


def seed_issues_with_runtime(
    *,
    session,
    tenant: Tenant,
    prompt_markdown: str,
    scoped_project_id: str | None,
    force_issue_keys: list[str] | None,
    allow_create: bool,
    scoped_project_keys: list[str] | None,
    codex_working_dir: str,
    tenant_project_keys_fn,
    get_settings_fn,
    build_runtime_fn,
    plan_seed_issues_with_runtime_fn,
    codex_runtime_error_type,
    build_seed_issue_description_fn,
    issue_key_pattern,
    tenant_atlassian_oauth_context_fn,
    select_seed_match_fn,
    allow_empty_children: bool = False,
    pm_status: str | None = None,
    planning_package: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    attempt_ref: WorkflowAttemptRef | None = None,
):  # noqa: ANN001
    del build_seed_issue_description_fn
    attempt = attempt_ref.number if attempt_ref is not None else None
    attempt_id = attempt_ref.attempt_id if attempt_ref is not None else None
    if attempt_ref is not None:
        ref_operation_id = attempt_ref.require_operation_id()
        explicit_operation_id = str(operation_id or "").strip() or None
        if explicit_operation_id is not None and explicit_operation_id != ref_operation_id:
            raise ValueError("Issue seeding operation_id does not match attempt_ref")
        operation_id = ref_operation_id
    project_keys = tenant_project_keys_fn(session=session, tenant=tenant)
    normalized_scoped_project_keys = [
        str(value).strip().upper()
        for value in (scoped_project_keys or [])
        if str(value).strip()
    ]
    if normalized_scoped_project_keys:
        allowed = set(normalized_scoped_project_keys)
        project_keys = [value for value in project_keys if str(value).strip().upper() in allowed]
    if not project_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")

    settings = get_settings_fn()
    oauth = _resolve_seed_oauth_context(
        session=session,
        tenant=tenant,
        settings=settings,
        tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context_fn,
    )
    browse_base_url = str(oauth["connection"].site_url or "").strip().rstrip("/")
    try:
        project_issue_types_by_key = _project_issue_types_by_key(oauth=oauth, project_keys=project_keys)
    except (ValueError, TypeError, AttributeError, AtlassianOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to seed Jira issues: {exc}",
        ) from exc
    runtime = build_runtime_fn(session=session, settings=settings)
    try:
        plan_payload = plan_seed_issues_with_runtime_fn(
            runtime=runtime,
            prompt_markdown=prompt_markdown,
            allowed_project_keys=project_keys,
            project_issue_types_by_key=project_issue_types_by_key,
            invocation_context=AgentInvocationContext(
                channel="discord",
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                command="issues",
                stage="seed",
                working_dir=codex_working_dir,
                workflow_id=workflow_id,
                operation_id=operation_id,
                attempt_id=attempt_id,
                attempt=attempt,
                db_session=session,
            ),
        )
    except codex_runtime_error_type as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Issue seeding runtime is unavailable: {exc}",
        ) from exc

    project_key = plan_payload.project_key
    if not project_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Issue seeding runtime did not return project_key")
    if project_key not in project_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Issue seeding runtime selected unsupported Jira project key '{project_key}'",
        )
    available_issue_types = project_issue_types_by_key.get(project_key)
    if available_issue_types is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Issue seeding runtime selected project key '{project_key}' without discovered Jira issue types",
        )

    normalized_force_issue_keys = [str(value).strip().upper() for value in (force_issue_keys or []) if str(value).strip()]
    draft_set = parse_engineering_seed_drafts(
        plan_payload=plan_payload.to_payload(),
        force_issue_keys=normalized_force_issue_keys,
        issue_key_pattern=issue_key_pattern,
        allow_empty_children=allow_empty_children,
        pm_status=pm_status,
        planning_package=planning_package,
    )
    clarification_questions = draft_set.clarification_questions
    effective_pm_status = draft_set.pm_status
    effective_planning_package = draft_set.planning_package
    specialist_summary = list(effective_planning_package.specialist_summary)
    technical_decisions = list(effective_planning_package.technical_decisions)
    planning_blocked = effective_planning_package.blocked
    planning_state_for_description = effective_planning_package.planning_state_for_description
    parent_issue = draft_set.parent_issue
    engineering_children = draft_set.engineering_children
    architecture_gate: ArchitectureDocumentGate | None = None
    architecture_link: ArchitectureDocumentLink | None = None
    architecture_required = False
    architecture_blocked = False
    architecture_labels = list(parent_issue.labels)

    parent_issue = parent_issue.with_issue_type(parent_issue.normalized_issue_type(
        engineering_children=engineering_children,
        available_issue_types=available_issue_types,
    ))
    try:
        all_project_issues = _project_issue_catalog(oauth=oauth, project_key=project_key)
        matched_issue_keys: set[str] = set()
        parent_sync_status = "planning_blocked" if planning_blocked else "children_syncing"
        parent_issue_input = parent_issue.to_jira_input(
            sync_status=parent_sync_status,
            pm_status=effective_pm_status or None,
            planning_state=planning_state_for_description,
        )
        _record_seed_operation_event(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            workflow_id=workflow_id,
            operation_id=operation_id,
            issue_key=None,
            attempt_ref=attempt_ref,
            event_kind="jira_parent_upsert_request",
            message="Submitting parent Jira issue upsert.",
            payload={
                "project_key": project_key,
                "requested_issue_key": parent_issue.requested_issue_key,
                "summary": parent_issue_input.summary,
                "description": parent_issue_input.description,
                "labels": parent_issue_input.labels,
                "issue_type": parent_issue_input.issue_type,
            },
        )
        parent_issue_key, parent_created, parent_updated = _upsert_parent_issue(
                oauth=oauth,
                project_key=project_key,
                issue_input=parent_issue_input,
                requested_issue_key=parent_issue.requested_issue_key,
                allow_create=allow_create,
                existing_issues=all_project_issues,
                matched_issue_keys=matched_issue_keys,
                select_seed_match_fn=select_seed_match_fn,
            )
        _record_seed_operation_event(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            workflow_id=workflow_id,
            operation_id=operation_id,
            issue_key=parent_issue_key,
            attempt_ref=attempt_ref,
            event_kind="jira_parent_upsert_response",
            message="Parent Jira issue upsert completed.",
            payload={
                "project_key": project_key,
                "issue_key": parent_issue_key,
                "created": parent_created,
                "updated": parent_updated,
            },
        )
        if not parent_issue_key:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Parent issue could not be matched and issue creation is disabled",
            )
        if architecture_required_for_issue(issue_labels=architecture_labels):
            architecture_gate, architecture_link = _architecture_gate_for_parent_issue(
                session=session,
                tenant=tenant,
                project_id=scoped_project_id,
                parent_issue_key=parent_issue_key,
                issue_summary=parent_issue.summary,
                issue_labels=architecture_labels,
                actor="system",
                settings_factory=get_settings_fn,
            )
            if architecture_gate.required and architecture_link is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=architecture_gate.block_reason or f"Architecture document link is required for {parent_issue_key}",
                )
            architecture_required = architecture_gate.required
            architecture_blocked = architecture_required and not architecture_gate.ready
        _upsert_architecture_document_remote_link(
            oauth=oauth,
            issue_key=parent_issue_key,
            architecture_link=architecture_link,
        )
        planning_blocked = planning_blocked or architecture_blocked

        if planning_blocked:
            parent_final_input = parent_issue.to_jira_input(
                sync_status=parent_sync_status,
                pm_status=effective_pm_status or None,
                planning_state=planning_state_for_description,
            )
            _record_seed_operation_event(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                workflow_id=workflow_id,
                operation_id=operation_id,
                issue_key=parent_issue_key,
                attempt_ref=attempt_ref,
                event_kind="jira_parent_update_request",
                message="Submitting blocked-planning update for parent Jira issue.",
                payload={
                    "issue_key": parent_issue_key,
                    "summary": parent_final_input.summary,
                    "description": parent_final_input.description,
                    "labels": parent_final_input.labels,
                },
            )
            _update_parent_issue_metadata(
                oauth=oauth,
                issue_key=parent_issue_key,
                summary=parent_final_input.summary,
                labels=parent_final_input.labels,
            )
            _record_seed_operation_event(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                workflow_id=workflow_id,
                operation_id=operation_id,
                issue_key=parent_issue_key,
                attempt_ref=attempt_ref,
                event_kind="jira_parent_update_response",
                message="Updated parent Jira issue after blocked planning.",
                payload={
                    "issue_key": parent_issue_key,
                    "labels": parent_final_input.labels,
                    "planning_state": planning_state_for_description,
                    "children_sync_status": parent_sync_status,
                },
            )
            parent_updated = not parent_created
            updated_issue_keys = [parent_issue_key] if parent_updated else []
            created_issue_keys = [parent_issue_key] if parent_created else []
            message = (
                "Issue upsert complete. "
                f"Parent: {format_issue_markdown_list(issue_keys=[parent_issue_key], browse_base_url=browse_base_url)}. "
                f"Updated {len(updated_issue_keys)}: "
                f"{format_issue_markdown_list(issue_keys=updated_issue_keys, browse_base_url=browse_base_url)}. "
                f"Created {len(created_issue_keys)}: "
                f"{format_issue_markdown_list(issue_keys=created_issue_keys, browse_base_url=browse_base_url)}."
            )
            message = (
                f"{message}\n\n"
                + (
                    "Architecture is required for this epic, and the linked architecture document is not ready. "
                    "Mark the architecture document ready before refreshing this parent issue to create the child tickets."
                    if architecture_blocked
                    else "Specialist planning is not complete yet, so engineering child tickets were not created. "
                    "Once the planning package is complete, refresh this parent issue to create the child tickets."
                )
            )
            return (
                message,
                {
                    "project_key": project_key,
                    "requires_input": bool(clarification_questions),
                    "questions": clarification_questions,
                    "prompt_markdown": prompt_markdown,
                    "parent_issue_key": parent_issue_key,
                    "parent_revision": parent_issue.revision,
                    "pm_status": effective_pm_status or None,
                    "planning_state": planning_state_for_description,
                    "planning_summary": specialist_summary,
                    "architecture_document_required": architecture_blocked,
                    "architecture_document_title": architecture_link.title if architecture_link else None,
                    "architecture_document_url": architecture_link.url if architecture_link else None,
                    "children_sync_status": "planning_blocked",
                    "stale_child_keys": [],
                    "updated_parent": parent_issue_key if parent_updated else None,
                    "created_parent": parent_issue_key if parent_created else None,
                    "updated_children": [],
                    "created_children": [],
                    "updated_issue_keys": updated_issue_keys,
                    "updated_issue_links": build_issue_url_list(
                        issue_keys=updated_issue_keys,
                        browse_base_url=browse_base_url,
                    ),
                    "created_issue_keys": created_issue_keys,
                    "created_issue_links": build_issue_url_list(
                        issue_keys=created_issue_keys,
                        browse_base_url=browse_base_url,
                    ),
                    "all_issue_keys": [parent_issue_key],
                    "errors": [],
                },
            )

        parent_jira_issue_type = _actual_parent_issue_type(
            oauth=oauth,
            parent_issue_key=parent_issue_key,
            created_parent_issue_type=parent_issue.issue_type,
            parent_created=parent_created,
        )
        resolved_child_issue_type = _engineering_child_issue_type_for_parent(
            parent_issue_type=parent_jira_issue_type,
            available_issue_types=available_issue_types,
            parent_issue_key=parent_issue_key,
        )
        child_catalog = _child_issue_catalog(
            oauth=oauth,
            project_key=project_key,
            parent_issue_key=parent_issue_key,
        )
        matched_child_keys: set[str] = set()
        created_child_keys: list[str] = []
        updated_child_keys: list[str] = []
        stale_child_keys: list[str] = []
        final_sync_status = "sync_blocked" if clarification_questions else "children_current"
        for child_issue in engineering_children:
            requested_child_issue_type = child_issue.issue_type
            child_issue = child_issue.with_issue_type(resolved_child_issue_type)
            child_input = child_issue.to_jira_input(
                parent_issue_key=parent_issue_key,
                parent_summary=parent_issue.summary,
                parent_revision=parent_issue.revision,
                sync_status=final_sync_status,
                specialist_summary=specialist_summary,
                technical_decisions=technical_decisions,
                planning_state=planning_state_for_description,
                pm_status=effective_pm_status or None,
            )
            _record_seed_operation_event(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                workflow_id=workflow_id,
                operation_id=operation_id,
                issue_key=parent_issue_key,
                attempt_ref=attempt_ref,
                event_kind="jira_child_upsert_request",
                message=f"Submitting child Jira issue upsert for {child_issue.summary}.",
                payload={
                    "project_key": project_key,
                    "parent_issue_key": parent_issue_key,
                    "requested_issue_key": child_issue.requested_issue_key,
                    "parent_issue_type": parent_jira_issue_type,
                    "requested_issue_type": requested_child_issue_type,
                    "resolved_issue_type": resolved_child_issue_type,
                    "summary": child_input.summary,
                    "description": child_input.description,
                    "labels": child_input.labels,
                    "issue_type": child_input.issue_type,
                },
            )
            try:
                child_key, child_created, child_updated = _upsert_issue(
                    oauth=oauth,
                    project_key=project_key,
                    issue_input=child_input,
                    requested_issue_key=child_issue.requested_issue_key,
                    allow_create=allow_create,
                    existing_issues=child_catalog,
                    matched_issue_keys=matched_child_keys,
                    select_seed_match_fn=select_seed_match_fn,
                )
            except AtlassianOAuthError as exc:
                if "Subtask issue type is not available" not in str(exc):
                    raise
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"Failed to seed Jira issues: project {project_key} does not support subtasks "
                        f"for parent issue {parent_issue_key}"
                    ),
                ) from exc
            if not child_key:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Engineering child '{child_issue.summary}' was not matched and issue creation is disabled",
                )
            _record_seed_operation_event(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                workflow_id=workflow_id,
                operation_id=operation_id,
                issue_key=child_key,
                attempt_ref=attempt_ref,
                event_kind="jira_child_upsert_response",
                message=f"Child Jira issue upsert completed for {child_issue.summary}.",
                payload={
                    "issue_key": child_key,
                    "created": child_created,
                    "updated": child_updated,
                    "parent_issue_key": parent_issue_key,
                },
            )
            stale_child_keys.append(child_key)
            _upsert_architecture_document_remote_link(
                oauth=oauth,
                issue_key=child_key,
                architecture_link=architecture_link,
            )
            if child_created:
                created_child_keys.append(child_key)
            if child_updated:
                updated_child_keys.append(child_key)

        parent_final_input = parent_issue.to_jira_input(
            sync_status=final_sync_status,
            pm_status=effective_pm_status or None,
            planning_state=planning_state_for_description,
        )
        _record_seed_operation_event(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            workflow_id=workflow_id,
            operation_id=operation_id,
            issue_key=parent_issue_key,
            attempt_ref=attempt_ref,
            event_kind="jira_parent_update_request",
            message="Submitting final parent Jira issue update.",
            payload={
                "issue_key": parent_issue_key,
                "summary": parent_final_input.summary,
                "description": parent_final_input.description,
                "labels": parent_final_input.labels,
            },
        )
        _update_parent_issue_metadata(
            oauth=oauth,
            issue_key=parent_issue_key,
            summary=parent_final_input.summary,
            labels=parent_final_input.labels,
        )
        _record_seed_operation_event(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            workflow_id=workflow_id,
            operation_id=operation_id,
            issue_key=parent_issue_key,
            attempt_ref=attempt_ref,
            event_kind="jira_parent_update_response",
            message="Updated parent Jira issue with final sync state.",
            payload={
                "issue_key": parent_issue_key,
                "labels": parent_final_input.labels,
                "planning_state": planning_state_for_description,
                "children_sync_status": final_sync_status,
                "clarification_questions": clarification_questions,
            },
        )
        if parent_created:
            parent_updated = False
        else:
            parent_updated = True
    except HTTPException:
        raise
    except (ValueError, TypeError, AttributeError, AtlassianOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to seed Jira issues: {exc}",
        ) from exc

    updated_issue_keys = [parent_issue_key] if parent_updated else []
    created_issue_keys = [parent_issue_key] if parent_created else []
    all_issue_keys = [parent_issue_key, *updated_child_keys, *created_child_keys]
    created_issue_keys.extend(created_child_keys)
    updated_issue_keys.extend(updated_child_keys)

    if not created_issue_keys and not updated_issue_keys:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Jira seed upsert produced no changes",
        )

    message = (
        "Issue upsert complete. "
        f"Parent: {format_issue_markdown_list(issue_keys=[parent_issue_key], browse_base_url=browse_base_url)}. "
        f"Updated {len(updated_issue_keys)}: "
        f"{format_issue_markdown_list(issue_keys=updated_issue_keys, browse_base_url=browse_base_url)}. "
        f"Created {len(created_issue_keys)}: "
        f"{format_issue_markdown_list(issue_keys=created_issue_keys, browse_base_url=browse_base_url)}."
    )
    if clarification_questions:
        prompt = "\n".join(f"{idx}. {question}" for idx, question in enumerate(clarification_questions, start=1))
        message = (
            f"{message}\n\nI still need more detail to finish issue quality. "
            "Reply in the follow-up thread and I will update the parent feature and affected engineering tickets.\n"
            f"{prompt}"
        )
    return (
        message,
        {
            "project_key": project_key,
            "requires_input": bool(clarification_questions),
            "questions": clarification_questions,
            "prompt_markdown": prompt_markdown,
            "parent_issue_key": parent_issue_key,
            "parent_revision": parent_issue.revision,
            "pm_status": effective_pm_status or None,
            "planning_state": planning_state_for_description,
            "planning_summary": specialist_summary,
            "architecture_document_required": architecture_blocked,
            "architecture_document_title": architecture_link.title if architecture_link else None,
            "architecture_document_url": architecture_link.url if architecture_link else None,
            "children_sync_status": final_sync_status,
            "stale_child_keys": stale_child_keys if final_sync_status != "children_current" else [],
            "updated_parent": parent_issue_key if parent_updated else None,
            "created_parent": parent_issue_key if parent_created else None,
            "updated_children": updated_child_keys,
            "created_children": created_child_keys,
            "updated_issue_keys": updated_issue_keys,
            "updated_issue_links": build_issue_url_list(
                issue_keys=updated_issue_keys,
                browse_base_url=browse_base_url,
            ),
            "created_issue_keys": created_issue_keys,
            "created_issue_links": build_issue_url_list(
                issue_keys=created_issue_keys,
                browse_base_url=browse_base_url,
            ),
            "all_issue_keys": all_issue_keys,
            "errors": [],
        },
    )


def seed_parent_issues_with_runtime(
    *,
    session,
    tenant: Tenant,
    prompt_markdown: str,
    scoped_project_id: str | None,
    force_issue_keys: list[str] | None,
    allow_create: bool,
    scoped_project_keys: list[str] | None,
    codex_working_dir: str,
    tenant_project_keys_fn,
    get_settings_fn,
    build_runtime_fn,
    plan_pm_parent_issues_with_runtime_fn,
    codex_runtime_error_type,
    issue_key_pattern,
    tenant_atlassian_oauth_context_fn,
    select_seed_match_fn,
    pm_status: str | None = None,
    pm_interview_notes_json: dict | None = None,
):  # noqa: ANN001
    project_keys = tenant_project_keys_fn(session=session, tenant=tenant)
    normalized_scoped_project_keys = [
        str(value).strip().upper()
        for value in (scoped_project_keys or [])
        if str(value).strip()
    ]
    if normalized_scoped_project_keys:
        allowed = set(normalized_scoped_project_keys)
        project_keys = [value for value in project_keys if str(value).strip().upper() in allowed]
    if not project_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")

    settings = get_settings_fn()
    _assert_stage_spi_allows_parent_seed(
        session=session,
        tenant=tenant,
        scoped_project_id=scoped_project_id,
        settings=settings,
        pm_interview_notes_json=pm_interview_notes_json,
    )
    oauth = _resolve_seed_oauth_context(
        session=session,
        tenant=tenant,
        settings=settings,
        tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context_fn,
    )
    browse_base_url = str(oauth["connection"].site_url or "").strip().rstrip("/")
    try:
        project_issue_types_by_key = _project_issue_types_by_key(oauth=oauth, project_keys=project_keys)
    except (ValueError, TypeError, AttributeError, AtlassianOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to seed Jira issues: {exc}",
        ) from exc
    runtime = build_runtime_fn(session=session, settings=settings)
    try:
        plan_payload = plan_pm_parent_issues_with_runtime_fn(
            runtime=runtime,
            prompt_markdown=prompt_markdown,
            allowed_project_keys=project_keys,
            project_issue_types_by_key=project_issue_types_by_key,
            invocation_context=AgentInvocationContext(
                channel="discord",
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                command="issues",
                stage="pm-seed",
                working_dir=codex_working_dir,
            ),
        )
    except codex_runtime_error_type as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"PM parent seeding runtime is unavailable: {exc}",
        ) from exc

    project_key = plan_payload.project_key
    if not project_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="PM parent seeding runtime did not return project_key")
    if project_key not in project_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"PM parent seeding runtime selected unsupported Jira project key '{project_key}'",
        )
    available_issue_types = project_issue_types_by_key.get(project_key)
    if available_issue_types is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"PM parent seeding runtime selected project key '{project_key}' without discovered Jira issue types",
        )

    normalized_force_issue_keys = [str(value).strip().upper() for value in (force_issue_keys or []) if str(value).strip()]
    draft_set = parse_parent_seed_drafts(
        plan_payload=plan_payload.to_payload(),
        force_issue_keys=normalized_force_issue_keys,
        issue_key_pattern=issue_key_pattern,
        pm_status=pm_status,
    )
    clarification_questions = draft_set.clarification_questions
    effective_pm_status = draft_set.pm_status
    parent_issues = draft_set.parent_issues

    try:
        all_project_issues = _project_issue_catalog(oauth=oauth, project_key=project_key)
        matched_issue_keys: set[str] = set()
        created_parent_issue_keys: list[str] = []
        updated_parent_issue_keys: list[str] = []
        for parent_issue in parent_issues:
            parent_issue = parent_issue.with_issue_type(parent_issue.normalized_issue_type(
                engineering_children=[],
                available_issue_types=available_issue_types,
            ))
            parent_input = parent_issue.to_jira_input(
                sync_status="children_stale",
                pm_status=effective_pm_status or None,
            )
            parent_key, parent_created, parent_updated = _upsert_parent_issue(
                oauth=oauth,
                project_key=project_key,
                issue_input=parent_input,
                requested_issue_key=parent_issue.requested_issue_key,
                allow_create=allow_create,
                existing_issues=all_project_issues,
                matched_issue_keys=matched_issue_keys,
                select_seed_match_fn=select_seed_match_fn,
            )
            if not parent_key:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Parent issue '{parent_issue.summary}' was not matched and issue creation is disabled",
                )
            architecture_gate = None
            architecture_link = None
            if architecture_required_for_issue(issue_labels=parent_issue.labels):
                architecture_gate, architecture_link = _architecture_gate_for_parent_issue(
                    session=session,
                    tenant=tenant,
                    project_id=scoped_project_id,
                    parent_issue_key=parent_key,
                    issue_summary=parent_issue.summary,
                    issue_labels=parent_issue.labels,
                    actor="system",
                    settings_factory=get_settings_fn,
                )
                if architecture_gate.required and architecture_link is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=architecture_gate.block_reason or f"Architecture document link is required for {parent_key}",
                    )
            _upsert_architecture_document_remote_link(
                oauth=oauth,
                issue_key=parent_key,
                architecture_link=architecture_link,
            )
            final_parent_input = parent_issue.to_jira_input(
                sync_status=(
                    "planning_blocked"
                    if architecture_gate is not None and architecture_gate.required and not architecture_gate.ready
                    else "children_stale"
                ),
                pm_status=effective_pm_status or None,
            )
            _update_parent_issue_metadata(
                oauth=oauth,
                issue_key=parent_key,
                summary=final_parent_input.summary,
                labels=final_parent_input.labels,
            )
            if parent_created:
                created_parent_issue_keys.append(parent_key)
            if parent_updated:
                updated_parent_issue_keys.append(parent_key)
        if not created_parent_issue_keys and not updated_parent_issue_keys:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Jira seed upsert produced no changes",
            )
    except HTTPException:
        raise
    except (ValueError, TypeError, AttributeError, AtlassianOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to seed Jira issues: {exc}",
        ) from exc

    all_parent_issue_keys = [*updated_parent_issue_keys, *created_parent_issue_keys]
    message = (
        "PM parent issue upsert complete. "
        f"Updated {len(updated_parent_issue_keys)}: "
        f"{format_issue_markdown_list(issue_keys=updated_parent_issue_keys, browse_base_url=browse_base_url)}. "
        f"Created {len(created_parent_issue_keys)}: "
        f"{format_issue_markdown_list(issue_keys=created_parent_issue_keys, browse_base_url=browse_base_url)}."
    )
    if clarification_questions:
        prompt = "\n".join(f"{idx}. {question}" for idx, question in enumerate(clarification_questions, start=1))
        message = (
            f"{message}\n\nI still need more PM detail to finish these parent issues. "
            "Reply in the follow-up thread and I will update the parent briefs.\n"
            f"{prompt}"
        )
    return (
        message,
        {
            "project_key": project_key,
            "requires_input": bool(clarification_questions),
            "questions": clarification_questions,
            "prompt_markdown": prompt_markdown,
            "created_parent_issue_keys": created_parent_issue_keys,
            "created_parent_issue_links": build_issue_url_list(
                issue_keys=created_parent_issue_keys,
                browse_base_url=browse_base_url,
            ),
            "updated_parent_issue_keys": updated_parent_issue_keys,
            "updated_parent_issue_links": build_issue_url_list(
                issue_keys=updated_parent_issue_keys,
                browse_base_url=browse_base_url,
            ),
            "all_parent_issue_keys": all_parent_issue_keys,
            "created_issue_keys": created_parent_issue_keys,
            "created_issue_links": build_issue_url_list(
                issue_keys=created_parent_issue_keys,
                browse_base_url=browse_base_url,
            ),
            "updated_issue_keys": updated_parent_issue_keys,
            "updated_issue_links": build_issue_url_list(
                issue_keys=updated_parent_issue_keys,
                browse_base_url=browse_base_url,
            ),
            "all_issue_keys": all_parent_issue_keys,
            "pm_status": effective_pm_status or None,
            "errors": [],
        },
    )
