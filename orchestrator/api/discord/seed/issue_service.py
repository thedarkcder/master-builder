from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status

from orchestrator.api.discord.shared.response_format import build_issue_url_list, format_issue_markdown_list
from orchestrator.core.codex_invocation import CodexInvocationContext
from orchestrator.storage.models import Tenant
from orchestrator.tools.jira_oauth import JiraIssueCreateInput, JiraOAuthError

SEED_FOLLOWUP_CONTEXT_MAX_AGE = timedelta(hours=24)


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


def seed_issues_with_codex(
    *,
    session,
    tenant: Tenant,
    prompt_markdown: str,
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
    runtime = build_codex_runtime_fn(session=session, settings=settings)
    try:
        plan_payload = plan_seed_issues_with_codex_fn(
            runtime=runtime,
            prompt_markdown=prompt_markdown,
            allowed_project_keys=project_keys,
            invocation_context=CodexInvocationContext(
                channel="discord",
                tenant_id=tenant.tenant_id,
                project_id=None,
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
    project_key = str(project_key_raw).strip() if isinstance(project_key_raw, str) else ""
    if not project_key:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return project_key")
    if project_key not in project_keys:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Codex selected unsupported Jira project key '{project_key}'",
        )

    raw_issues = plan_payload.get("issues")
    if not isinstance(raw_issues, list):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex did not return issue drafts")

    clarification_questions: list[str] = []
    question_set: set[str] = set()
    raw_questions = plan_payload.get("questions")
    if isinstance(raw_questions, list):
        for raw_question in raw_questions:
            question = str(raw_question).strip()
            if question and question not in question_set:
                question_set.add(question)
                clarification_questions.append(question)
    normalized_force_issue_keys = [str(value).strip() for value in (force_issue_keys or []) if str(value).strip()]
    issue_inputs: list[JiraIssueCreateInput] = []
    issue_requested_keys: list[str | None] = []
    for issue_index, item in enumerate(raw_issues[:12], start=1):
        if not isinstance(item, dict):
            continue
        summary = _optional_string(issue_index=issue_index, field_name="summary", raw_value=item.get("summary"))
        objective = _optional_string(issue_index=issue_index, field_name="objective", raw_value=item.get("objective"))
        scope_in = _string_list_field(issue_index=issue_index, field_name="scope_in", raw_value=item.get("scope_in"))
        scope_out = _string_list_field(issue_index=issue_index, field_name="scope_out", raw_value=item.get("scope_out"))
        acceptance = _string_list_field(
            issue_index=issue_index,
            field_name="acceptance_criteria",
            raw_value=item.get("acceptance_criteria"),
        )
        how_to_test = _string_list_field(issue_index=issue_index, field_name="how_to_test", raw_value=item.get("how_to_test"))
        nfr_intent = _optional_string(issue_index=issue_index, field_name="nfr_intent", raw_value=item.get("nfr_intent"))
        dependencies = _string_list_field(issue_index=issue_index, field_name="dependencies", raw_value=item.get("dependencies"))
        risks = _string_list_field(issue_index=issue_index, field_name="risks", raw_value=item.get("risks"))
        labels = _string_list_field(issue_index=issue_index, field_name="labels", raw_value=item.get("labels"))
        issue_type = _optional_string(issue_index=issue_index, field_name="issue_type", raw_value=item.get("issue_type"))
        if not issue_type:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Codex issue draft {issue_index} has missing issue_type",
            )
        requested_issue_key_raw = item.get("issue_key")
        requested_issue_key = str(requested_issue_key_raw).strip() if isinstance(requested_issue_key_raw, str) else None
        if requested_issue_key and issue_key_pattern.match(requested_issue_key) is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Codex issue draft {issue_index} has invalid issue_key '{requested_issue_key}'",
            )
        if not requested_issue_key and len(normalized_force_issue_keys) > len(issue_requested_keys):
            requested_issue_key = normalized_force_issue_keys[len(issue_requested_keys)]
        if not summary:
            continue
        issue_inputs.append(
            JiraIssueCreateInput(
                summary=summary[:90],
                description=build_seed_issue_description_fn(
                    objective=objective,
                    scope_in=scope_in,
                    scope_out=scope_out,
                    acceptance_criteria=acceptance,
                    how_to_test=how_to_test,
                    nfr_intent=nfr_intent,
                    dependencies_and_risks=[*dependencies, *risks],
                ),
                labels=labels,
                issue_type=issue_type,
            )
        )
        issue_requested_keys.append(requested_issue_key)

    if not issue_inputs:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Codex returned no valid issue drafts")

    try:
        oauth = tenant_jira_oauth_context_fn(session=session, tenant=tenant, settings=settings)
        existing_issues = oauth["client"].search_issues_by_jql(
            access_token=oauth["access_token"],
            cloud_id=oauth["connection"].cloud_id,
            jql=f'project = "{project_key}" ORDER BY updated DESC',
            max_results=100,
        )
        matched_issue_keys: set[str] = set()
        to_create: list[JiraIssueCreateInput] = []
        updated_issue_keys: list[str] = []

        for idx, issue_input in enumerate(issue_inputs):
            requested_issue_key = issue_requested_keys[idx] if idx < len(issue_requested_keys) else None
            matched = select_seed_match_fn(
                existing_issues=existing_issues,
                summary=issue_input.summary,
                requested_issue_key=requested_issue_key,
                matched_issue_keys=matched_issue_keys,
            )
            if matched is None:
                to_create.append(issue_input)
                continue
            matched_issue_keys.add(matched.key)
            oauth["client"].update_issue_fields(
                access_token=oauth["access_token"],
                cloud_id=oauth["connection"].cloud_id,
                issue_id_or_key=matched.key,
                summary=issue_input.summary,
                description=issue_input.description,
                labels=issue_input.labels,
            )
            updated_issue_keys.append(matched.key)

        create_result = (
            oauth["client"].create_issues_bulk(
                access_token=oauth["access_token"],
                cloud_id=oauth["connection"].cloud_id,
                project_key=project_key,
                issues=to_create,
            )
            if to_create and allow_create
            else None
        )
    except (ValueError, JiraOAuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to seed Jira issues: {exc}",
        ) from exc

    created_keys = [issue.key for issue in create_result.created] if create_result else []
    create_errors = create_result.errors if create_result else []
    if not created_keys and not updated_issue_keys:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Jira seed upsert produced no changes: {'; '.join(create_errors) or 'unknown error'}",
        )
    browse_base_url = str(oauth["connection"].site_url or "").strip().rstrip("/")

    message = (
        "Issue upsert complete. "
        f"Updated {len(updated_issue_keys)}: "
        f"{format_issue_markdown_list(issue_keys=updated_issue_keys, browse_base_url=browse_base_url)}. "
        f"Created {len(created_keys)}: "
        f"{format_issue_markdown_list(issue_keys=created_keys, browse_base_url=browse_base_url)}."
    )
    if create_errors:
        message = f"{message} (partial errors: {'; '.join(create_errors)})"
    if clarification_questions:
        prompt = "\n".join(f"{idx}. {question}" for idx, question in enumerate(clarification_questions, start=1))
        message = (
            f"{message}\n\nI still need more detail to finish issue quality. "
            "Reply in the follow-up thread and I will update these tickets.\n"
            f"{prompt}"
        )
    return (
        message,
        {
            "project_key": project_key,
            "requires_input": bool(clarification_questions),
            "questions": clarification_questions,
            "prompt_markdown": prompt_markdown,
            "updated_issue_keys": updated_issue_keys,
            "updated_issue_links": build_issue_url_list(
                issue_keys=updated_issue_keys,
                browse_base_url=browse_base_url,
            ),
            "created_issue_keys": created_keys,
            "created_issue_links": build_issue_url_list(
                issue_keys=created_keys,
                browse_base_url=browse_base_url,
            ),
            "all_issue_keys": [*updated_issue_keys, *created_keys],
            "errors": create_errors,
        },
    )
