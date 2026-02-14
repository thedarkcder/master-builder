from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.core.communications.enqueue_reason_contract import (
    format_enqueue_conflict_detail,
)
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.runs import cancel_run, enqueue_run
from orchestrator.storage.models import Run, Tenant


def _oauth_context_value(oauth_context: Any, field: str) -> Any:
    if isinstance(oauth_context, dict):
        return oauth_context.get(field)
    return getattr(oauth_context, field, None)


def _decision_gate_remaining_questions_message(*, issue_key: str, reason: str, questions: list[str]) -> str:
    lines = [
        f"Decision Gate still needs clarification for `{issue_key}`.",
        f"Reason: {reason}",
    ]
    if questions:
        lines.append("Please reply with:")
        lines.extend(f"- {question}" for question in questions[:5])
    return "\n".join(lines)


def _plan_decision_gate_jira_update(
    *,
    runtime,  # noqa: ANN001
    issue_key: str,
    current_summary: str,
    current_description: str,
    reply_text: str,
) -> tuple[str, str]:
    payload = runtime.run_json(
        system_prompt=(
            "You normalize Decision Gate clarifications into Jira issue context. "
            "Return strict JSON only with keys: summary, description. "
            "description must include explicit sections named exactly: "
            "Objective, Scope, Acceptance Criteria, How to test, NFR intent."
        ),
        user_prompt=(
            "Stage: decision-gate-reply-normalization\n"
            f"Issue key: {issue_key}\n"
            f"Current summary: {current_summary}\n"
            f"Current description:\n{current_description}\n\n"
            f"User reply text:\n{reply_text}\n\n"
            "Constraints:\n"
            "- Preserve known context from current summary/description.\n"
            "- Integrate reply details into missing GTD sections.\n"
            "- Keep summary concise (<=255 chars).\n"
            "- Keep description implementation-ready and deterministic.\n"
        ),
    )
    summary = str(payload.get("summary") or "").strip()
    description = str(payload.get("description") or "").strip()
    if not summary:
        raise CodexRuntimeError("Codex did not return a summary for Decision Gate update")
    if not description:
        raise CodexRuntimeError("Codex did not return a description for Decision Gate update")
    return summary[:255], description


def dispatch_run_control_command(
    *,
    session: Session,
    tenant: Tenant,
    tenant_id: str,
    payload: DiscordCommandRequest,
    command_name: str,
    arguments: list[str],
    scope: CommandScope,
    retryable_statuses: set[str],
    resolve_project_for_issue: Callable[..., Any],
    fetch_issue_preview: Callable[..., Any],
    fetch_issue_detail: Callable[..., Any],
    settings_factory: Callable[[], Any],
    build_codex_runtime: Callable[..., Any],
    tenant_jira_oauth_context: Callable[..., Any],
    evaluate_decision_gate: Callable[..., Any],
    ensure_issue_is_executable: Callable[..., Any],
) -> DiscordCommandResponse | None:
    if command_name == "run":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !run <ISSUE_KEY>")
        issue_key = arguments[0].strip().upper()
        project = resolve_project_for_issue(
            session=session,
            tenant=tenant,
            issue_key=issue_key,
        )
        if scope.project_keys and project.jira_project_key not in set(scope.project_keys):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Issue {issue_key} is outside the mapped project scope",
            )
        issue_preview = fetch_issue_preview(session=session, tenant=tenant, issue_key=issue_key)
        ensure_issue_is_executable(issue_status=issue_preview.status, tenant=tenant)
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=None,
            repo_url=project.github_repository,
            delivery_id=None,
            max_concurrent_runs=resolve_effective_policy(
                tenant_policy=tenant.policy_config,
                project_overrides=project.policy_overrides,
            ).get("max_concurrent_runs"),
        )
        if not enqueue_result.enqueued:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=format_enqueue_conflict_detail(
                    prefix="Run could not be queued",
                    enqueue_reason=str(enqueue_result.reason),
                    enqueue_run_obj=enqueue_result.run,
                ),
            )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Queued run {enqueue_result.run.run_id} for {issue_key}",
            data={"run_id": enqueue_result.run.run_id, "issue_key": issue_key},
        )

    if command_name == "cancel":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !cancel <RUN_ID>")
        run_id = arguments[0].strip()
        run = session.get(Run, run_id)
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Run {run_id} was not found")
        if scope.project_id and run.project_id != scope.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Run {run_id} is outside the mapped project scope",
            )
        cancelled = cancel_run(session, run_id=run_id, cancelled_by=payload.user_id)
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Cancelled run {cancelled.run_id}",
            data={"run_id": cancelled.run_id, "status": cancelled.status},
        )

    if command_name == "retry":
        if len(arguments) != 1:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Usage: !retry <ISSUE_KEY|RUN_ID>")
        target = arguments[0].strip()
        run = session.get(Run, target)
        if run is None:
            issue_key = target.upper()
            run = session.execute(
                select(Run)
                .where(Run.tenant_id == tenant_id, Run.issue_key == issue_key)
                .order_by(Run.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
        if run is None or run.tenant_id != tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No run was found for '{target}'",
            )
        if scope.project_id and run.project_id != scope.project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Run {run.run_id} is outside the mapped project scope",
            )
        if run.status not in retryable_statuses:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Run {run.run_id} is {run.status}; only failed/blocked/cancelled runs can be retried",
            )
        issue_preview = fetch_issue_preview(session=session, tenant=tenant, issue_key=run.issue_key)
        ensure_issue_is_executable(issue_status=issue_preview.status, tenant=tenant)
        issue_description = run.issue_description
        try:
            issue_detail = fetch_issue_detail(session=session, tenant=tenant, issue_key=run.issue_key)
            refreshed_description = str(getattr(issue_detail, "description", "") or "").strip()
            if refreshed_description:
                issue_description = refreshed_description
        except HTTPException:
            issue_description = run.issue_description
        project = resolve_project_for_issue(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
        )
        if scope.project_keys and project.jira_project_key not in set(scope.project_keys):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Issue {run.issue_key} is outside the mapped project scope",
            )
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            issue_key=run.issue_key,
            issue_summary=issue_preview.summary,
            issue_description=issue_description,
            repo_url=project.github_repository,
            delivery_id=None,
            max_concurrent_runs=resolve_effective_policy(
                tenant_policy=tenant.policy_config,
                project_overrides=project.policy_overrides,
            ).get("max_concurrent_runs"),
        )
        if not enqueue_result.enqueued:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=format_enqueue_conflict_detail(
                    prefix="Retry could not be queued",
                    enqueue_reason=str(enqueue_result.reason),
                    enqueue_run_obj=enqueue_result.run,
                ),
            )
        return DiscordCommandResponse(
            ok=True,
            command=command_name,
            message=f"Queued retry run {enqueue_result.run.run_id} for {run.issue_key}",
            data={"run_id": enqueue_result.run.run_id, "issue_key": run.issue_key},
        )

    if command_name == "reply":
        command_params = payload.command_params if isinstance(payload.command_params, dict) else {}
        issue_key = str(command_params.get("issue_key") or "").strip().upper()
        reply_text = str(command_params.get("reply_text") or "").strip()
        if not issue_key:
            if arguments:
                issue_key = arguments[0].strip().upper()
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Usage: !reply <ISSUE_KEY> <clarification text>",
                )
        if not reply_text:
            if len(arguments) < 2:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Usage: !reply <ISSUE_KEY> <clarification text>",
                )
            reply_text = " ".join(arguments[1:]).strip()
        if not issue_key or not reply_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Usage: !reply <ISSUE_KEY> <clarification text>",
            )
        run = session.execute(
            select(Run)
            .where(
                Run.tenant_id == tenant_id,
                Run.issue_key == issue_key,
                Run.status.in_(retryable_statuses),
            )
            .order_by(Run.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if run is None:
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=f"No retryable run was found for `{issue_key}`.",
                data={"issue_key": issue_key},
            )
        settings = settings_factory()
        try:
            oauth = tenant_jira_oauth_context(session=session, tenant=tenant, settings=settings)
            oauth_client = _oauth_context_value(oauth, "client")
            oauth_connection = _oauth_context_value(oauth, "connection")
            oauth_access_token = _oauth_context_value(oauth, "access_token")
            cloud_id = getattr(oauth_connection, "cloud_id", None)
            if oauth_client is None or oauth_access_token is None or not str(cloud_id or "").strip():
                raise RuntimeError("Tenant Jira OAuth context is incomplete")
            issue_detail = oauth_client.get_issue_detail(
                access_token=oauth_access_token,
                cloud_id=str(cloud_id),
                issue_id_or_key=issue_key,
            )
            runtime = build_codex_runtime(session=session, settings=settings)
            updated_summary, updated_description = _plan_decision_gate_jira_update(
                runtime=runtime,
                issue_key=issue_key,
                current_summary=issue_detail.summary,
                current_description=issue_detail.description,
                reply_text=reply_text,
            )
            oauth_client.update_issue_summary_and_description(
                access_token=oauth_access_token,
                cloud_id=str(cloud_id),
                issue_id_or_key=issue_key,
                summary=updated_summary,
                description=updated_description,
            )
        except (HTTPException, CodexRuntimeError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to update Jira context for `{issue_key}`: {exc}",
            ) from exc

        decision_gate = evaluate_decision_gate(
            issue_summary=updated_summary,
            issue_description=updated_description,
        )
        if decision_gate.triggered:
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=_decision_gate_remaining_questions_message(
                    issue_key=issue_key,
                    reason=str(decision_gate.reason or "").strip(),
                    questions=[question.strip() for question in decision_gate.questions if str(question).strip()],
                ),
                data={
                    "issue_key": issue_key,
                    "recheck_required": True,
                    "decision_gate_reason": decision_gate.reason,
                    "questions": [question.strip() for question in decision_gate.questions if str(question).strip()],
                },
            )

        return dispatch_run_control_command(
            session=session,
            tenant=tenant,
            tenant_id=tenant_id,
            payload=payload,
            command_name="retry",
            arguments=[issue_key],
            scope=scope,
            retryable_statuses=retryable_statuses,
            resolve_project_for_issue=resolve_project_for_issue,
            fetch_issue_preview=fetch_issue_preview,
            fetch_issue_detail=fetch_issue_detail,
            settings_factory=settings_factory,
            build_codex_runtime=build_codex_runtime,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            evaluate_decision_gate=evaluate_decision_gate,
            ensure_issue_is_executable=ensure_issue_is_executable,
        )

    return None
