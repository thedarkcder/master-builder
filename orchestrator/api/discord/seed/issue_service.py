from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException, status

from orchestrator.api.discord.seed.description import (
    build_engineering_child_description,
    build_parent_feature_description,
)
from orchestrator.api.discord.shared.response_format import build_issue_url_list, format_issue_markdown_list
from orchestrator.core.codex_invocation import CodexInvocationContext
from orchestrator.storage.models import Tenant
from orchestrator.tools.jira_oauth import JiraIssueCreateInput, JiraIssuePreview, JiraOAuthError

SEED_FOLLOWUP_CONTEXT_MAX_AGE = timedelta(hours=24)
_MAX_ENGINEERING_CHILDREN = 12
_PM_PARENT_LABEL = "pm-parent"
_ENGINEERING_CHILD_LABEL = "engineering-child"
_SYNC_CURRENT_LABEL = "sync-current"
_SYNC_STALE_LABEL = "sync-stale"
_SYNC_BLOCKED_LABEL = "sync-blocked"
_PARENT_MULTI_STORY_HINTS = (
    "multi-story",
    "multi story",
    "multi-step",
    "multi step",
    "cross-cutting",
    "cross cutting",
    "program",
    "initiative",
    "roadmap",
)
_PARENT_PRIMARY_ISSUE_TYPES = ("Story", "Feature", "Task", "Issue")


def _string_list_field(*, issue_index: int, field_name: str, raw_value: object) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex issue draft {issue_index} has invalid '{field_name}' (expected list of strings)",
        )
    values: list[str] = []
    for entry in raw_value:
        if not isinstance(entry, str):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Codex issue draft {issue_index} has invalid '{field_name}' entry type",
            )
        text = entry.strip()
        if text:
            values.append(text)
    return values


def _optional_string(*, issue_index: int, field_name: str, raw_value: object) -> str:
    if raw_value is None:
        return ""
    if not isinstance(raw_value, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex issue draft {issue_index} has invalid '{field_name}' (expected string)",
        )
    return raw_value.strip()


def _normalize_issue_key(raw_value: object, *, field_name: str, issue_index: int, issue_key_pattern) -> str | None:  # noqa: ANN001
    if raw_value is None:
        return None
    if not isinstance(raw_value, str):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex issue draft {issue_index} has invalid '{field_name}' (expected string)",
        )
    issue_key = raw_value.strip().upper()
    if not issue_key:
        return None
    if issue_key_pattern.match(issue_key) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex issue draft {issue_index} has invalid {field_name} '{issue_key}'",
        )
    return issue_key


def _normalize_label(raw_value: str, *, prefix: str = "") -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(raw_value or "").strip().lower()).strip("-")
    if not normalized:
        return ""
    if prefix:
        normalized = f"{prefix}{normalized}"
    return normalized[:64]


def _dedupe_labels(*label_groups: list[str]) -> list[str]:
    seen: set[str] = set()
    labels: list[str] = []
    for group in label_groups:
        for raw_label in group:
            label = str(raw_label or "").strip()
            if not label:
                continue
            key = label.casefold()
            if key in seen:
                continue
            seen.add(key)
            labels.append(label)
    return labels


def _sync_label(sync_status: str) -> str:
    normalized = str(sync_status or "").strip().lower()
    if normalized == "children_current":
        return _SYNC_CURRENT_LABEL
    if normalized == "sync_blocked":
        return _SYNC_BLOCKED_LABEL
    return _SYNC_STALE_LABEL


def _compute_parent_revision(parent_issue: dict[str, Any]) -> str:
    payload = json.dumps(parent_issue, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


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


def _parse_questions(raw_questions: object) -> list[str]:
    questions: list[str] = []
    seen: set[str] = set()
    if not isinstance(raw_questions, list):
        return questions
    for raw_question in raw_questions:
        question = str(raw_question or "").strip()
        if not question or question in seen:
            continue
        seen.add(question)
        questions.append(question)
    return questions


def _project_issue_catalog(*, oauth: dict[str, Any], project_key: str) -> list[JiraIssuePreview]:
    return oauth["client"].search_issues_by_jql(
        access_token=oauth["access_token"],
        cloud_id=oauth["cloud_id"],
        jql=f'project = "{project_key}" ORDER BY updated DESC',
        max_results=100,
    )


def _child_issue_catalog(*, oauth: dict[str, Any], project_key: str, parent_issue_key: str) -> list[JiraIssuePreview]:
    parent_label = _normalize_label(parent_issue_key, prefix="parent-")
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


def _first_present_issue_type(choices: tuple[str, ...], available_issue_types: list[str]) -> str | None:
    if not available_issue_types:
        return choices[0] if choices else None
    by_lower = {name.casefold(): name for name in available_issue_types if str(name).strip()}
    for choice in choices:
        matched = by_lower.get(choice.casefold())
        if matched:
            return matched
    return None


def _parent_should_default_to_epic(*, parent_issue: dict[str, Any], engineering_children: list[dict[str, Any]]) -> bool:
    del engineering_children
    candidate_text = " ".join(
        [
            str(parent_issue.get("summary") or ""),
            str(parent_issue.get("objective") or ""),
            str(parent_issue.get("recommendation") or ""),
            " ".join(str(value) for value in parent_issue.get("scope_in") or []),
            " ".join(str(value) for value in parent_issue.get("success_outcomes") or []),
        ]
    ).casefold()
    return any(hint in candidate_text for hint in _PARENT_MULTI_STORY_HINTS)


def _normalize_parent_issue_type(
    *,
    parent_issue: dict[str, Any],
    engineering_children: list[dict[str, Any]],
    available_issue_types: list[str],
) -> str:
    requested_issue_type = str(parent_issue.get("issue_type") or "").strip()
    if available_issue_types:
        by_lower = {name.casefold(): name for name in available_issue_types if str(name).strip()}
        requested_match = by_lower.get(requested_issue_type.casefold()) if requested_issue_type else None
        if requested_match:
            return requested_match

    preferred_choices: tuple[str, ...]
    if _parent_should_default_to_epic(parent_issue=parent_issue, engineering_children=engineering_children):
        preferred_choices = ("Epic", *_PARENT_PRIMARY_ISSUE_TYPES)
    else:
        preferred_choices = (*_PARENT_PRIMARY_ISSUE_TYPES, "Epic")
    normalized = _first_present_issue_type(preferred_choices, available_issue_types)
    if normalized:
        return normalized
    if requested_issue_type:
        return requested_issue_type
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="No supported parent Jira issue type is available for this project",
    )


def _parse_parent_issue(
    *,
    raw_parent: object,
    force_issue_keys: list[str],
    issue_key_pattern,
) -> dict[str, Any]:  # noqa: ANN001
    if not isinstance(raw_parent, dict):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return parent_issue")
    issue_index = 1
    summary = _optional_string(issue_index=issue_index, field_name="summary", raw_value=raw_parent.get("summary"))
    if not summary:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex parent_issue is missing summary")
    issue_type = _optional_string(
        issue_index=issue_index,
        field_name="issue_type",
        raw_value=raw_parent.get("issue_type"),
    )
    requested_issue_key = _normalize_issue_key(
        raw_parent.get("issue_key"),
        field_name="issue_key",
        issue_index=issue_index,
        issue_key_pattern=issue_key_pattern,
    )
    if requested_issue_key is None and force_issue_keys:
        requested_issue_key = force_issue_keys[0]
    labels = _string_list_field(issue_index=issue_index, field_name="labels", raw_value=raw_parent.get("labels"))
    return {
        "summary": summary[:90],
        "issue_type": issue_type,
        "objective": _optional_string(issue_index=issue_index, field_name="objective", raw_value=raw_parent.get("objective")),
        "user_value": _optional_string(issue_index=issue_index, field_name="user_value", raw_value=raw_parent.get("user_value")),
        "recommendation": _optional_string(
            issue_index=issue_index,
            field_name="recommendation",
            raw_value=raw_parent.get("recommendation"),
        ),
        "scope_in": _string_list_field(issue_index=issue_index, field_name="scope_in", raw_value=raw_parent.get("scope_in")),
        "scope_out": _string_list_field(issue_index=issue_index, field_name="scope_out", raw_value=raw_parent.get("scope_out")),
        "acceptance_criteria": _string_list_field(
            issue_index=issue_index,
            field_name="acceptance_criteria",
            raw_value=raw_parent.get("acceptance_criteria"),
        ),
        "ui_references": _string_list_field(
            issue_index=issue_index,
            field_name="ui_references",
            raw_value=raw_parent.get("ui_references"),
        ),
        "success_outcomes": _string_list_field(
            issue_index=issue_index,
            field_name="success_outcomes",
            raw_value=raw_parent.get("success_outcomes"),
        ),
        "dependencies": _string_list_field(
            issue_index=issue_index,
            field_name="dependencies",
            raw_value=raw_parent.get("dependencies"),
        ),
        "risks": _string_list_field(issue_index=issue_index, field_name="risks", raw_value=raw_parent.get("risks")),
        "open_questions": _string_list_field(
            issue_index=issue_index,
            field_name="open_questions",
            raw_value=raw_parent.get("open_questions"),
        ),
        "labels": labels,
        "requested_issue_key": requested_issue_key,
    }


def _parse_engineering_children(
    *,
    raw_children: object,
    force_issue_keys: list[str],
    issue_key_pattern,
    allow_empty_children: bool = False,
) -> list[dict[str, Any]]:  # noqa: ANN001
    if not isinstance(raw_children, list):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return engineering_children")
    children: list[dict[str, Any]] = []
    remaining_force_keys = force_issue_keys[1:] if force_issue_keys else []
    for issue_index, item in enumerate(raw_children[:_MAX_ENGINEERING_CHILDREN], start=2):
        if not isinstance(item, dict):
            continue
        summary = _optional_string(issue_index=issue_index, field_name="summary", raw_value=item.get("summary"))
        if not summary:
            continue
        raw_issue_type = _optional_string(issue_index=issue_index, field_name="issue_type", raw_value=item.get("issue_type"))
        requested_issue_key = _normalize_issue_key(
            item.get("issue_key"),
            field_name="issue_key",
            issue_index=issue_index,
            issue_key_pattern=issue_key_pattern,
        )
        if requested_issue_key is None and len(remaining_force_keys) >= len(children) + 1:
            requested_issue_key = remaining_force_keys[len(children)]
        fallback_issue_type = (
            raw_issue_type
            if raw_issue_type and raw_issue_type.strip().casefold() not in {"sub-task", "subtask"}
            else "Task"
        )
        children.append(
            {
                "summary": summary[:90],
                "issue_type": "Sub-task",
                "fallback_issue_type": fallback_issue_type,
                "behavior_slice": _optional_string(
                    issue_index=issue_index,
                    field_name="behavior_slice",
                    raw_value=item.get("behavior_slice"),
                ),
                "technical_objective": _optional_string(
                    issue_index=issue_index,
                    field_name="technical_objective",
                    raw_value=item.get("technical_objective"),
                ),
                "implementation_plan": _string_list_field(
                    issue_index=issue_index,
                    field_name="implementation_plan",
                    raw_value=item.get("implementation_plan"),
                ),
                "technical_dependencies": _string_list_field(
                    issue_index=issue_index,
                    field_name="technical_dependencies",
                    raw_value=item.get("technical_dependencies"),
                ),
                "risks": _string_list_field(
                    issue_index=issue_index,
                    field_name="risks",
                    raw_value=item.get("risks"),
                ),
                "how_to_test": _string_list_field(
                    issue_index=issue_index,
                    field_name="how_to_test",
                    raw_value=item.get("how_to_test"),
                ),
                "done_criteria": _string_list_field(
                    issue_index=issue_index,
                    field_name="done_criteria",
                    raw_value=item.get("done_criteria"),
                ),
                "labels": _string_list_field(
                    issue_index=issue_index,
                    field_name="labels",
                    raw_value=item.get("labels"),
                ),
                "requested_issue_key": requested_issue_key,
            }
        )
    if not children and not allow_empty_children:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex returned no valid engineering_children")
    return children


def _build_parent_issue_input(*, parent_issue: dict[str, Any], parent_revision: str, sync_status: str) -> JiraIssueCreateInput:
    return JiraIssueCreateInput(
        summary=parent_issue["summary"],
        description=build_parent_feature_description(
            objective=parent_issue["objective"],
            user_value=parent_issue["user_value"],
            recommendation=parent_issue["recommendation"],
            scope_in=parent_issue["scope_in"],
            scope_out=parent_issue["scope_out"],
            acceptance_criteria=parent_issue["acceptance_criteria"],
            ui_references=parent_issue["ui_references"],
            success_outcomes=parent_issue["success_outcomes"],
            dependencies_and_risks=[*parent_issue["dependencies"], *parent_issue["risks"]],
            open_questions=parent_issue["open_questions"],
            parent_revision=parent_revision,
            sync_status=sync_status,
        ),
        labels=_dedupe_labels(
            parent_issue["labels"],
            [_PM_PARENT_LABEL, _sync_label(sync_status)],
        ),
        issue_type=parent_issue["issue_type"],
    )


def _build_child_issue_input(
    *,
    child_issue: dict[str, Any],
    parent_issue_key: str,
    parent_summary: str,
    parent_revision: str,
    sync_status: str,
    use_subtask: bool,
) -> JiraIssueCreateInput:
    parent_label = _normalize_label(parent_issue_key, prefix="parent-")
    return JiraIssueCreateInput(
        summary=child_issue["summary"],
        description=build_engineering_child_description(
            parent_issue_key=parent_issue_key,
            parent_summary=parent_summary,
            parent_revision=parent_revision,
            behavior_slice=child_issue["behavior_slice"],
            technical_objective=child_issue["technical_objective"],
            implementation_plan=child_issue["implementation_plan"],
            how_to_test=child_issue["how_to_test"],
            done_criteria=child_issue["done_criteria"],
            dependencies_and_risks=[*child_issue["technical_dependencies"], *child_issue["risks"]],
        ),
        labels=_dedupe_labels(
            child_issue["labels"],
            [_ENGINEERING_CHILD_LABEL, parent_label, _sync_label(sync_status)],
        ),
        issue_type=child_issue["issue_type"] if use_subtask else child_issue["fallback_issue_type"],
        parent_issue_key=parent_issue_key if use_subtask else None,
        linked_parent_issue_key=parent_issue_key,
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


def seed_issues_with_codex(
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
    build_codex_runtime_fn,
    plan_seed_issues_with_codex_fn,
    codex_runtime_error_type,
    build_seed_issue_description_fn,
    issue_key_pattern,
    tenant_jira_oauth_context_fn,
    select_seed_match_fn,
    allow_empty_children: bool = False,
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
    runtime = build_codex_runtime_fn(session=session, settings=settings)
    try:
        plan_payload = plan_seed_issues_with_codex_fn(
            runtime=runtime,
            prompt_markdown=prompt_markdown,
            allowed_project_keys=project_keys,
            invocation_context=CodexInvocationContext(
                channel="discord",
                tenant_id=tenant.tenant_id,
                project_id=scoped_project_id,
                command="issues",
                stage="seed",
                working_dir=codex_working_dir,
            ),
        )
    except codex_runtime_error_type as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex issue seeding is unavailable: {exc}",
        ) from exc

    project_key_raw = plan_payload.get("project_key")
    project_key = str(project_key_raw).strip().upper() if isinstance(project_key_raw, str) else ""
    if not project_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return project_key")
    if project_key not in project_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex selected unsupported Jira project key '{project_key}'",
        )

    normalized_force_issue_keys = [str(value).strip().upper() for value in (force_issue_keys or []) if str(value).strip()]
    clarification_questions = _parse_questions(plan_payload.get("questions"))
    parent_issue = _parse_parent_issue(
        raw_parent=plan_payload.get("parent_issue"),
        force_issue_keys=normalized_force_issue_keys,
        issue_key_pattern=issue_key_pattern,
    )
    engineering_children = _parse_engineering_children(
        raw_children=plan_payload.get("engineering_children"),
        force_issue_keys=normalized_force_issue_keys,
        issue_key_pattern=issue_key_pattern,
        allow_empty_children=allow_empty_children,
    )
    parent_revision = _compute_parent_revision(parent_issue)

    oauth = _resolve_seed_oauth_context(
        session=session,
        tenant=tenant,
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context_fn,
    )
    browse_base_url = str(oauth["connection"].site_url or "").strip().rstrip("/")
    available_issue_types = _project_available_issue_types(oauth=oauth, project_key=project_key)
    parent_issue["issue_type"] = _normalize_parent_issue_type(
        parent_issue=parent_issue,
        engineering_children=engineering_children,
        available_issue_types=available_issue_types,
    )
    try:
        all_project_issues = _project_issue_catalog(oauth=oauth, project_key=project_key)
        matched_issue_keys: set[str] = set()
        parent_sync_status = "children_syncing"
        parent_issue_input = _build_parent_issue_input(
            parent_issue=parent_issue,
            parent_revision=parent_revision,
            sync_status=parent_sync_status,
        )
        parent_issue_key, parent_created, parent_updated = _upsert_issue(
            oauth=oauth,
            project_key=project_key,
            issue_input=parent_issue_input,
            requested_issue_key=parent_issue["requested_issue_key"],
            allow_create=allow_create,
            existing_issues=all_project_issues,
            matched_issue_keys=matched_issue_keys,
            select_seed_match_fn=select_seed_match_fn,
        )
        if not parent_issue_key:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Parent issue could not be matched and issue creation is disabled",
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
            child_input = _build_child_issue_input(
                child_issue=child_issue,
                parent_issue_key=parent_issue_key,
                parent_summary=parent_issue["summary"],
                parent_revision=parent_revision,
                sync_status=final_sync_status,
                use_subtask=True,
            )
            child_key: str | None = None
            child_created = False
            child_updated = False
            try:
                child_key, child_created, child_updated = _upsert_issue(
                    oauth=oauth,
                    project_key=project_key,
                    issue_input=child_input,
                    requested_issue_key=child_issue["requested_issue_key"],
                    allow_create=allow_create,
                    existing_issues=child_catalog,
                    matched_issue_keys=matched_child_keys,
                    select_seed_match_fn=select_seed_match_fn,
                )
            except JiraOAuthError as exc:
                if "Subtask issue type is not available" not in str(exc):
                    raise
                fallback_input = _build_child_issue_input(
                    child_issue=child_issue,
                    parent_issue_key=parent_issue_key,
                    parent_summary=parent_issue["summary"],
                    parent_revision=parent_revision,
                    sync_status=final_sync_status,
                    use_subtask=False,
                )
                child_key, child_created, child_updated = _upsert_issue(
                    oauth=oauth,
                    project_key=project_key,
                    issue_input=fallback_input,
                    requested_issue_key=child_issue["requested_issue_key"],
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
            if not child_key:
                create_errors.append(f"Engineering child '{child_issue['summary']}' was not matched and creation is disabled")
                final_sync_status = "sync_blocked"
                continue
            stale_child_keys.append(child_key)
            if child_created:
                created_child_keys.append(child_key)
            if child_updated:
                updated_child_keys.append(child_key)

        parent_final_input = _build_parent_issue_input(
            parent_issue=parent_issue,
            parent_revision=parent_revision,
            sync_status=final_sync_status,
        )
        oauth["client"].update_issue_fields(
            access_token=oauth["access_token"],
            cloud_id=oauth["cloud_id"],
            issue_id_or_key=parent_issue_key,
            summary=parent_final_input.summary,
            description=parent_final_input.description,
            labels=parent_final_input.labels,
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
            "parent_revision": parent_revision,
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
