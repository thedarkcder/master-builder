from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status

from orchestrator.api.discord.seed.draft_assembly import (
    parse_engineering_seed_drafts,
    parse_parent_seed_drafts,
)
from orchestrator.api.discord.shared.response_format import build_issue_url_list, format_issue_markdown_list
from orchestrator.core.audit_events import record_audit_event
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.workflow_operation_logging import emit_workflow_operation_log
from orchestrator.storage.models import Tenant, WorkflowOperation
from orchestrator.tools.jira_oauth import JiraIssueCreateInput, JiraIssuePreview, JiraOAuthError

SEED_FOLLOWUP_CONTEXT_MAX_AGE = timedelta(hours=24)
_PM_PARENT_LABEL = "pm-parent"


def _parent_label(parent_issue_key: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(parent_issue_key or "").strip().lower()).strip("-")
    return f"parent-{normalized[:64]}" if normalized else "parent"

def _assert_stage_spi_allows_parent_seed(
    *,
    session,
    tenant: Tenant,
    scoped_project_id: str | None,
    settings: object,
    pm_interview_notes_json: dict | None,
) -> None:
    from orchestrator.core.stage_spi_policy import resolve_stage_spi_enabled

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


def _resolve_seed_oauth_context(*, session, tenant: Tenant, settings, tenant_jira_oauth_context_fn):  # noqa: ANN001
    try:
        oauth = tenant_jira_oauth_context_fn(session=session, tenant=tenant, settings=settings)
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to seed Jira issues: {exc}",
        ) from exc

    if not isinstance(oauth, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to seed Jira issues: Jira OAuth context is incomplete",
        )
    client = oauth.get("client")
    access_token = str(oauth.get("access_token") or "").strip()
    connection = oauth.get("connection")
    cloud_id = str(getattr(connection, "cloud_id", "") or "").strip()
    if client is None or not access_token or connection is None or not cloud_id:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to seed Jira issues: Jira OAuth context is incomplete",
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
    tenant_jira_oauth_context_fn,
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
        oauth = tenant_jira_oauth_context_fn(session=session, tenant=tenant, settings=settings)
        escaped_keys = ", ".join(f'"{value.replace(chr(34), "").strip()}"' for value in issue_keys)
        existing = oauth["client"].search_issues_by_jql(
            access_token=oauth["access_token"],
            cloud_id=oauth["connection"].cloud_id,
            jql=f"issuekey in ({escaped_keys})",
            max_results=min(len(issue_keys), 50),
        )
    except (HTTPException, ValueError, JiraOAuthError):
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
        try:
            issues = oauth["client"].search_issues_by_jql(
                access_token=oauth["access_token"],
                cloud_id=oauth["cloud_id"],
                jql=query,
                max_results=100,
            )
        except (AttributeError, TypeError, ValueError, JiraOAuthError):
            continue
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
    if callable(list_issue_types):
        try:
            payload = list_issue_types(
                access_token=oauth["access_token"],
                cloud_id=oauth["cloud_id"],
                project_key=project_key,
            )
        except (AttributeError, TypeError, ValueError, JiraOAuthError):
            return []
        return [str(value).strip() for value in payload if str(value).strip()]
    return []


def _record_seed_operation_event(
    *,
    session,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str | None,
    operation_id: str | None,
    issue_key: str | None,
    attempt: int | None,
    event_kind: str,
    message: str,
    payload: dict[str, Any] | None = None,
) -> None:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_workflow_id = str(workflow_id or "").strip() or None
    normalized_operation_id = str(operation_id or "").strip() or None
    if not normalized_tenant_id or not normalized_workflow_id or not normalized_operation_id:
        return
    operation = session.get(WorkflowOperation, normalized_operation_id)
    if operation is None:
        return
    event_payload = {"attempt": attempt, **dict(payload or {})}
    record_audit_event(
        session,
        tenant_id=normalized_tenant_id,
        project_id=str(project_id or "").strip() or None,
        workflow_id=normalized_workflow_id,
        run_id=operation.run_id,
        operation_id=normalized_operation_id,
        attempt_id=None,
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
        event_type=event_kind,
        message=message,
        metadata=event_payload,
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
    tenant_jira_oauth_context_fn,
    select_seed_match_fn,
    allow_empty_children: bool = False,
    pm_status: str | None = None,
    planning_package: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    attempt: int | None = None,
):  # noqa: ANN001
    del build_seed_issue_description_fn
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
    runtime = build_runtime_fn(session=session, settings=settings)
    try:
        plan_payload = plan_seed_issues_with_runtime_fn(
            runtime=runtime,
            prompt_markdown=prompt_markdown,
            allowed_project_keys=project_keys,
            invocation_context=AgentInvocationContext(
                channel="discord",
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                command="issues",
                stage="seed",
                working_dir=codex_working_dir,
                workflow_id=workflow_id,
                operation_id=operation_id,
                attempt=attempt,
            ),
        )
    except codex_runtime_error_type as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Issue seeding runtime is unavailable: {exc}",
        ) from exc

    project_key_raw = plan_payload.get("project_key")
    project_key = str(project_key_raw).strip().upper() if isinstance(project_key_raw, str) else ""
    if not project_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Issue seeding runtime did not return project_key")
    if project_key not in project_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Issue seeding runtime selected unsupported Jira project key '{project_key}'",
        )

    normalized_force_issue_keys = [str(value).strip().upper() for value in (force_issue_keys or []) if str(value).strip()]
    draft_set = parse_engineering_seed_drafts(
        plan_payload=plan_payload,
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
    architecture_summary = list(effective_planning_package.architecture_summary)
    architecture_diagram = effective_planning_package.architecture_diagram
    planning_blocked = effective_planning_package.blocked
    planning_state_for_description = effective_planning_package.planning_state_for_description
    parent_issue = draft_set.parent_issue
    engineering_children = draft_set.engineering_children

    oauth = _resolve_seed_oauth_context(
        session=session,
        tenant=tenant,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
    )
    browse_base_url = str(oauth["connection"].site_url or "").strip().rstrip("/")
    available_issue_types = _project_available_issue_types(oauth=oauth, project_key=project_key)
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
            architecture_summary=architecture_summary,
            architecture_diagram=architecture_diagram if isinstance(architecture_diagram, str) else None,
        )
        _record_seed_operation_event(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            workflow_id=workflow_id,
            operation_id=operation_id,
            issue_key=None,
            attempt=attempt,
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
        parent_issue_key, parent_created, parent_updated = _upsert_issue(
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
            attempt=attempt,
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

        if planning_blocked:
            parent_final_input = parent_issue.to_jira_input(
                sync_status=parent_sync_status,
                pm_status=effective_pm_status or None,
                planning_state=planning_state_for_description,
                architecture_summary=architecture_summary,
                architecture_diagram=architecture_diagram if isinstance(architecture_diagram, str) else None,
            )
            _record_seed_operation_event(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                workflow_id=workflow_id,
                operation_id=operation_id,
                issue_key=parent_issue_key,
                attempt=attempt,
                event_kind="jira_parent_update_request",
                message="Submitting blocked-planning update for parent Jira issue.",
                payload={
                    "issue_key": parent_issue_key,
                    "summary": parent_final_input.summary,
                    "description": parent_final_input.description,
                    "labels": parent_final_input.labels,
                },
            )
            oauth["client"].update_issue_fields(
                access_token=oauth["access_token"],
                cloud_id=oauth["cloud_id"],
                issue_id_or_key=parent_issue_key,
                summary=parent_final_input.summary,
                description=parent_final_input.description,
                labels=parent_final_input.labels,
            )
            _record_seed_operation_event(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                workflow_id=workflow_id,
                operation_id=operation_id,
                issue_key=parent_issue_key,
                attempt=attempt,
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
                f"{message}\n\nSpecialist planning is not complete yet, so engineering child tickets were not created. "
                "Once the planning package is complete, refresh this parent issue to create the child tickets."
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

        child_catalog = _child_issue_catalog(
            oauth=oauth,
            project_key=project_key,
            parent_issue_key=parent_issue_key,
        )
        matched_child_keys: set[str] = set()
        created_child_keys: list[str] = []
        updated_child_keys: list[str] = []
        stale_child_keys: list[str] = []
        create_errors: list[str] = []

        final_sync_status = "sync_blocked" if clarification_questions else "children_current"
        for child_issue in engineering_children:
            child_input = child_issue.to_jira_input(
                parent_issue_key=parent_issue_key,
                parent_summary=parent_issue.summary,
                parent_revision=parent_issue.revision,
                sync_status=final_sync_status,
                use_subtask=True,
                specialist_summary=specialist_summary,
                planning_state=planning_state_for_description,
                pm_status=effective_pm_status or None,
            )
            child_key: str | None = None
            child_created = False
            child_updated = False
            _record_seed_operation_event(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                workflow_id=workflow_id,
                operation_id=operation_id,
                issue_key=parent_issue_key,
                attempt=attempt,
                event_kind="jira_child_upsert_request",
                message=f"Submitting child Jira issue upsert for {child_issue.summary}.",
                payload={
                    "project_key": project_key,
                    "parent_issue_key": parent_issue_key,
                    "requested_issue_key": child_issue.requested_issue_key,
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
            except JiraOAuthError as exc:
                if "Subtask issue type is not available" not in str(exc):
                    raise
                fallback_input = child_issue.to_jira_input(
                    parent_issue_key=parent_issue_key,
                    parent_summary=parent_issue.summary,
                    parent_revision=parent_issue.revision,
                    sync_status=final_sync_status,
                    use_subtask=False,
                    specialist_summary=specialist_summary,
                    planning_state=planning_state_for_description,
                    pm_status=effective_pm_status or None,
                )
                child_key, child_created, child_updated = _upsert_issue(
                    oauth=oauth,
                    project_key=project_key,
                    issue_input=fallback_input,
                    requested_issue_key=child_issue.requested_issue_key,
                    allow_create=allow_create,
                    existing_issues=child_catalog,
                    matched_issue_keys=matched_child_keys,
                    select_seed_match_fn=select_seed_match_fn,
                )
                if child_created and child_key:
                    oauth["client"].add_issue_link(
                        access_token=oauth["access_token"],
                        cloud_id=oauth["cloud_id"],
                        inward_issue_key=child_key,
                        outward_issue_key=parent_issue_key,
                    )
                    _record_seed_operation_event(
                        session=session,
                        tenant_id=tenant.tenant_id,
                        project_id=scoped_project_id,
                        workflow_id=workflow_id,
                        operation_id=operation_id,
                        issue_key=parent_issue_key,
                        attempt=attempt,
                        event_kind="jira_child_link_response",
                        message=f"Linked child issue {child_key} to parent {parent_issue_key}.",
                        payload={
                            "child_issue_key": child_key,
                            "parent_issue_key": parent_issue_key,
                        },
                    )
            if not child_key:
                create_errors.append(f"Engineering child '{child_issue.summary}' was not matched and creation is disabled")
                final_sync_status = "sync_blocked"
                continue
            _record_seed_operation_event(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                workflow_id=workflow_id,
                operation_id=operation_id,
                issue_key=child_key,
                attempt=attempt,
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
            attempt=attempt,
            event_kind="jira_parent_update_request",
            message="Submitting final parent Jira issue update.",
            payload={
                "issue_key": parent_issue_key,
                "summary": parent_final_input.summary,
                "description": parent_final_input.description,
                "labels": parent_final_input.labels,
            },
        )
        oauth["client"].update_issue_fields(
            access_token=oauth["access_token"],
            cloud_id=oauth["cloud_id"],
            issue_id_or_key=parent_issue_key,
            summary=parent_final_input.summary,
            description=parent_final_input.description,
            labels=parent_final_input.labels,
        )
        _record_seed_operation_event(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=scoped_project_id,
            workflow_id=workflow_id,
            operation_id=operation_id,
            issue_key=parent_issue_key,
            attempt=attempt,
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
    except (ValueError, TypeError, AttributeError, JiraOAuthError) as exc:
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
            detail=f"Jira seed upsert produced no changes: {'; '.join(create_errors) or 'unknown error'}",
        )

    message = (
        "Issue upsert complete. "
        f"Parent: {format_issue_markdown_list(issue_keys=[parent_issue_key], browse_base_url=browse_base_url)}. "
        f"Updated {len(updated_issue_keys)}: "
        f"{format_issue_markdown_list(issue_keys=updated_issue_keys, browse_base_url=browse_base_url)}. "
        f"Created {len(created_issue_keys)}: "
        f"{format_issue_markdown_list(issue_keys=created_issue_keys, browse_base_url=browse_base_url)}."
    )
    if create_errors:
        message = f"{message} (partial errors: {'; '.join(create_errors)})"
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
            "errors": create_errors,
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
    tenant_jira_oauth_context_fn,
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
    runtime = build_runtime_fn(session=session, settings=settings)
    try:
        plan_payload = plan_pm_parent_issues_with_runtime_fn(
            runtime=runtime,
            prompt_markdown=prompt_markdown,
            allowed_project_keys=project_keys,
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

    project_key_raw = plan_payload.get("project_key")
    project_key = str(project_key_raw).strip().upper() if isinstance(project_key_raw, str) else ""
    if not project_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="PM parent seeding runtime did not return project_key")
    if project_key not in project_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"PM parent seeding runtime selected unsupported Jira project key '{project_key}'",
        )

    normalized_force_issue_keys = [str(value).strip().upper() for value in (force_issue_keys or []) if str(value).strip()]
    draft_set = parse_parent_seed_drafts(
        plan_payload=plan_payload,
        force_issue_keys=normalized_force_issue_keys,
        issue_key_pattern=issue_key_pattern,
        pm_status=pm_status,
    )
    clarification_questions = draft_set.clarification_questions
    effective_pm_status = draft_set.pm_status
    parent_issues = draft_set.parent_issues

    oauth = _resolve_seed_oauth_context(
        session=session,
        tenant=tenant,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
    )
    browse_base_url = str(oauth["connection"].site_url or "").strip().rstrip("/")
    available_issue_types = _project_available_issue_types(oauth=oauth, project_key=project_key)

    try:
        all_project_issues = _project_issue_catalog(oauth=oauth, project_key=project_key)
        matched_issue_keys: set[str] = set()
        created_parent_issue_keys: list[str] = []
        updated_parent_issue_keys: list[str] = []
        errors: list[str] = []

        for parent_issue in parent_issues:
            parent_issue = parent_issue.with_issue_type(parent_issue.normalized_issue_type(
                engineering_children=[],
                available_issue_types=available_issue_types,
            ))
            parent_input = parent_issue.to_jira_input(
                sync_status="children_stale",
                pm_status=effective_pm_status or None,
            )
            parent_key, parent_created, parent_updated = _upsert_issue(
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
                errors.append(f"Parent issue '{parent_issue.summary}' was not matched and creation is disabled")
                continue
            if parent_created:
                created_parent_issue_keys.append(parent_key)
            if parent_updated:
                updated_parent_issue_keys.append(parent_key)
        if not created_parent_issue_keys and not updated_parent_issue_keys:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Jira seed upsert produced no changes: {'; '.join(errors) or 'unknown error'}",
            )
    except HTTPException:
        raise
    except (ValueError, TypeError, AttributeError, JiraOAuthError) as exc:
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
