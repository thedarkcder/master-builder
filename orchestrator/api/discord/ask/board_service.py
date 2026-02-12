from __future__ import annotations

from fastapi import HTTPException, status

from orchestrator.storage.models import Tenant


def ask_board_message(
    *,
    session,
    tenant: Tenant,
    user_id: str,
    channel_id: str,
    question: str,
    scoped_issue_key: str | None,
    collect_ask_context_with_history_context_fn,
    get_settings_fn,
    build_codex_runtime_fn,
    tenant_project_keys_fn,
    normalize_scope_channel_id_fn,
    channel_scope_repository,
    answer_board_question_with_codex_fn,
    collect_github_ask_context_fn,
    codex_runtime_error_type,
    store_ask_history_entry_fn,
):  # noqa: ANN001
    normalized_issue_key, requested_status, issues, status_counts, history_context = collect_ask_context_with_history_context_fn(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        scoped_issue_key=scoped_issue_key,
    )

    settings = get_settings_fn()
    runtime = build_codex_runtime_fn(session=session, settings=settings)
    scoped_project_keys = tenant_project_keys_fn(session=session, tenant=tenant)
    normalized_scope_channel_id = normalize_scope_channel_id_fn(channel_id)
    if normalized_scope_channel_id:
        scope = channel_scope_repository.resolve_project_scope(
            session=session,
            tenant=tenant,
            channel_id=normalized_scope_channel_id,
        )
        if scope is not None:
            scoped_project_keys = [scope.jira_project_key]
    github_context = collect_github_ask_context_fn(
        session=session,
        tenant=tenant,
        project_keys=[str(key).strip().upper() for key in scoped_project_keys if str(key).strip()],
    )
    try:
        message = answer_board_question_with_codex_fn(
            runtime=runtime,
            question=question,
            project_keys=[str(key).strip().upper() for key in scoped_project_keys if str(key).strip()],
            issues=issues,
            status_counts=status_counts,
            history=history_context,
            github_context=github_context,
        )
    except codex_runtime_error_type as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Codex board assistant is unavailable: {exc}",
        ) from exc

    store_ask_history_entry_fn(
        session=session,
        tenant=tenant,
        user_id=user_id,
        channel_id=channel_id,
        question=question,
        answer=message,
        issue_key=normalized_issue_key,
        status_name=requested_status,
    )

    return (
        message,
        {
            "issue_key": normalized_issue_key,
            "status": requested_status,
            "status_counts": status_counts,
            "issues": issues,
            "question": question,
            "memory_entries_used": len(history_context),
        },
    )
