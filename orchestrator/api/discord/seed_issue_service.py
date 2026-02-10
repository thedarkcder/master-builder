from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.api.discord.response_format import build_issue_url_list, format_issue_markdown_list
from orchestrator.storage.models import Tenant
from orchestrator.tools.jira_oauth import JiraIssueCreateInput, JiraOAuthError


def seed_issues_with_codex(
    *,
    session,
    tenant: Tenant,
    prompt_markdown: str,
    force_issue_keys: list[str] | None,
    allow_create: bool,
    tenant_project_keys_fn,
    get_settings_fn,
    build_codex_runtime_fn,
    plan_seed_issues_with_codex_fn,
    codex_runtime_error_type,
    normalize_seed_issue_scope_fn,
    normalize_seed_issue_key_fn,
    normalize_seed_issue_tags_fn,
    normalize_seed_issue_labels_fn,
    collect_seed_issue_questions_fn,
    build_seed_issue_description_fn,
    parse_seed_issue_type_fn,
    tenant_jira_oauth_context_fn,
    select_seed_match_fn,
):  # noqa: ANN001
    project_keys = tenant_project_keys_fn(session=session, tenant=tenant)
    if not project_keys:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tenant has no Jira project keys")

    settings = get_settings_fn()
    runtime = build_codex_runtime_fn(session=session, settings=settings)
    try:
        plan_payload = plan_seed_issues_with_codex_fn(
            runtime=runtime,
            prompt_markdown=prompt_markdown,
            allowed_project_keys=project_keys,
        )
    except codex_runtime_error_type as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex issue seeding is unavailable: {exc}",
        ) from exc

    project_key = str(plan_payload.get("project_key") or project_keys[0]).strip().upper()
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
    normalized_force_issue_keys = [
        str(value).strip().upper() for value in (force_issue_keys or []) if str(value).strip()
    ]
    issue_inputs: list[JiraIssueCreateInput] = []
    issue_requested_keys: list[str | None] = []
    for item in raw_issues[:12]:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        objective = str(item.get("objective") or "").strip()
        scope_in = normalize_seed_issue_scope_fn(item.get("scope_in"))
        scope_out = normalize_seed_issue_scope_fn(item.get("scope_out"))
        acceptance_raw = item.get("acceptance_criteria")
        acceptance = (
            [str(entry).strip() for entry in acceptance_raw if str(entry).strip()]
            if isinstance(acceptance_raw, list)
            else []
        )
        requested_issue_key = normalize_seed_issue_key_fn(item.get("issue_key"))
        if not requested_issue_key and len(normalized_force_issue_keys) > len(issue_requested_keys):
            requested_issue_key = normalized_force_issue_keys[len(issue_requested_keys)]
        tags = normalize_seed_issue_tags_fn(item.get("tags"))
        labels = normalize_seed_issue_labels_fn(item.get("labels"))
        for tag in tags:
            if tag not in labels:
                labels.append(tag)
        if not summary:
            continue
        draft_questions = collect_seed_issue_questions_fn(
            issue_summary=summary,
            objective=objective,
            scope_in=scope_in,
            scope_out=scope_out,
            acceptance=acceptance,
        )
        for question in draft_questions:
            if question not in question_set:
                question_set.add(question)
                clarification_questions.append(question)
        issue_inputs.append(
            JiraIssueCreateInput(
                summary=summary[:90],
                description=build_seed_issue_description_fn(
                    objective=objective,
                    scope_in=scope_in,
                    scope_out=scope_out,
                    acceptance_criteria=acceptance,
                ),
                labels=labels,
                issue_type=parse_seed_issue_type_fn(item.get("issue_type")),
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
