from __future__ import annotations

from collections.abc import Callable
from difflib import SequenceMatcher
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.core.communications.enqueue_reason_contract import (
    enqueue_reason_guidance,
    format_enqueue_conflict_detail,
)
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.runs import cancel_run, enqueue_run
from orchestrator.storage.models import Run, Tenant

DECISION_GATE_BLOCK_START = "<!-- decision-gate-clarifications:start -->"
DECISION_GATE_BLOCK_END = "<!-- decision-gate-clarifications:end -->"
PRECHECK_BOARD_BLOCK_START = "<!-- precheck-board-context:start -->"
PRECHECK_BOARD_BLOCK_END = "<!-- precheck-board-context:end -->"


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


def _gtd_missing_message(*, issue_key: str, missing_criteria: tuple[str, ...], questions: tuple[str, ...]) -> str:
    lines = [f"Good To Do still needs clarification for `{issue_key}`."]
    cleaned_missing = [item.strip() for item in missing_criteria if item.strip()]
    cleaned_questions = [question.strip() for question in questions if question.strip()]
    if cleaned_missing:
        lines.append("Missing criteria: " + ", ".join(cleaned_missing))
    if cleaned_questions:
        lines.append("Please reply with:")
        lines.extend(f"- {question}" for question in cleaned_questions)
    return "\n".join(lines)


def _choose_updated_summary(*, issue_key: str, current_summary: str, suggested_summary: str) -> str:
    current = str(current_summary or "").strip()
    suggested = str(suggested_summary or "").strip()[:255]
    if not current:
        return suggested or f"{issue_key} - Decision Gate clarified"
    if not suggested or suggested.lower() == current.lower():
        return current
    similarity = SequenceMatcher(None, current.lower(), suggested.lower()).ratio()
    if similarity >= 0.7 or current.lower() in suggested.lower() or suggested.lower() in current.lower():
        return suggested
    if "DG clarified" in current:
        return current
    return f"{current} | DG clarified"[:255]


def _upsert_decision_gate_clarifications_block(*, current_description: str, block: str) -> str:
    current = str(current_description or "").strip()
    if not current:
        return block
    start_idx = current.find(DECISION_GATE_BLOCK_START)
    end_idx = current.find(DECISION_GATE_BLOCK_END)
    if start_idx >= 0 and end_idx > start_idx:
        end_of_marker = end_idx + len(DECISION_GATE_BLOCK_END)
        prefix = current[:start_idx].rstrip()
        suffix = current[end_of_marker:].lstrip()
        if prefix and suffix:
            return f"{prefix}\n\n{block}\n\n{suffix}"
        if prefix:
            return f"{prefix}\n\n{block}"
        if suffix:
            return f"{block}\n\n{suffix}"
        return block
    return f"{current}\n\n{block}"


def _upsert_block(*, current_description: str, block: str, start_marker: str, end_marker: str) -> str:
    current = str(current_description or "").strip()
    if not current:
        return block
    start_idx = current.find(start_marker)
    end_idx = current.find(end_marker)
    if start_idx >= 0 and end_idx > start_idx:
        end_of_marker = end_idx + len(end_marker)
        prefix = current[:start_idx].rstrip()
        suffix = current[end_of_marker:].lstrip()
        if prefix and suffix:
            return f"{prefix}\n\n{block}\n\n{suffix}"
        if prefix:
            return f"{prefix}\n\n{block}"
        if suffix:
            return f"{block}\n\n{suffix}"
        return block
    return f"{current}\n\n{block}"


def _build_board_context_block(
    *,
    issue_key: str,
    project_key: str,
    issues: list[Any],
) -> str:
    status_counts: dict[str, int] = {}
    lines: list[str] = []
    for issue in issues:
        key = str(getattr(issue, "key", "") or "").strip().upper()
        if not key or key == issue_key:
            continue
        summary = str(getattr(issue, "summary", "") or "").strip()
        status_name = str(getattr(issue, "status", "") or "").strip() or "Unknown"
        status_counts[status_name] = status_counts.get(status_name, 0) + 1
        lines.append(f"- {key} [{status_name}] {summary}")
        if len(lines) >= 12:
            break
    if not lines:
        return ""
    counts = ", ".join(f"{name}={count}" for name, count in sorted(status_counts.items(), key=lambda item: item[0].lower()))
    block_lines = [
        PRECHECK_BOARD_BLOCK_START,
        f"## Board Context ({project_key})",
        "Recent related tickets in this project:",
        *lines,
        f"Status counts: {counts}",
        PRECHECK_BOARD_BLOCK_END,
    ]
    return "\n".join(block_lines)


def _build_precheck_description(
    *,
    issue_description: str | None,
    issue_key: str,
    project_key: str,
    search_issues_for_tenant: Callable[..., Any],
    session: Session,
    tenant: Tenant,
) -> str | None:
    jql = f'project = "{project_key}" ORDER BY updated DESC'
    try:
        issues = search_issues_for_tenant(
            session=session,
            tenant=tenant,
            jql=jql,
            max_results=20,
        )
    except HTTPException:
        return issue_description
    block = _build_board_context_block(
        issue_key=issue_key,
        project_key=project_key,
        issues=list(issues or []),
    )
    if not block:
        return issue_description
    return _upsert_block(
        current_description=str(issue_description or ""),
        block=block,
        start_marker=PRECHECK_BOARD_BLOCK_START,
        end_marker=PRECHECK_BOARD_BLOCK_END,
    )


def _plan_decision_gate_jira_update(
    *,
    runtime,  # noqa: ANN001
    invocation_context: CodexInvocationContext,
    issue_key: str,
    current_summary: str,
    current_description: str,
    reply_text: str,
) -> tuple[str, str]:
    def _normalize_dependencies_and_risks(value: object) -> str:
        if isinstance(value, list):
            normalized = [str(item).strip() for item in value if str(item).strip()]
            return "; ".join(normalized)
        text = str(value or "").strip()
        return text

    payload = invoke_codex_json(
        runtime=runtime,
        context=invocation_context,
        system_prompt=(
            "You extract Decision Gate clarification fields from a user reply. "
            "Return strict JSON only with keys: "
            "summary, objective, scope, acceptance_criteria, how_to_test, nfr_intent, "
            "reliability_security_constraints, out_of_scope, rollout_constraints, decision_owner, "
            "dependencies_and_risks. "
            "Do not rewrite the full ticket body."
        ),
        user_prompt=(
            "Stage: decision-gate-reply-normalization\n"
            f"Issue key: {issue_key}\n"
            f"Current summary: {current_summary}\n"
            f"Current description:\n{current_description}\n\n"
            f"User reply text:\n{reply_text}\n\n"
            "Constraints:\n"
            "- Preserve existing ticket format by outputting only extracted field values.\n"
            "- Include field text only when supported by user reply.\n"
            "- Keep summary concise (<=255 chars), close to current summary wording.\n"
        ),
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return JSON object for Decision Gate update")
    updated_summary = _choose_updated_summary(
        issue_key=issue_key,
        current_summary=current_summary,
        suggested_summary=str(payload.get("summary") or "").strip(),
    )
    objective = str(payload.get("objective") or "").strip() or "Provided in thread reply."
    scope = str(payload.get("scope") or "").strip() or "Provided in thread reply."
    acceptance_criteria = str(payload.get("acceptance_criteria") or "").strip() or "Provided in thread reply."
    how_to_test = str(payload.get("how_to_test") or "").strip() or "Provided in thread reply."
    nfr_intent = str(payload.get("nfr_intent") or "").strip() or "Provided in thread reply."
    reliability_security = str(payload.get("reliability_security_constraints") or "").strip() or "Not specified."
    out_of_scope = str(payload.get("out_of_scope") or "").strip() or "Not specified."
    rollout_constraints = str(payload.get("rollout_constraints") or "").strip() or "Not specified."
    decision_owner = str(payload.get("decision_owner") or "").strip() or "Not specified."
    dependencies_and_risks = _normalize_dependencies_and_risks(
        payload.get("dependencies_and_risks")
    ) or "Not specified."
    clarification_block = "\n".join(
        [
            DECISION_GATE_BLOCK_START,
            "## Decision Gate Clarifications",
            f"Objective: {objective}",
            f"Scope: {scope}",
            f"Acceptance Criteria: {acceptance_criteria}",
            f"How to test: {how_to_test}",
            f"NFR intent (MVP vs scale-ready): {nfr_intent}",
            f"Mandatory reliability/security constraints: {reliability_security}",
            f"Explicitly out of scope: {out_of_scope}",
            f"Rollout/migration constraints: {rollout_constraints}",
            f"Dependencies / Risks: {dependencies_and_risks}",
            f"Decision owner: {decision_owner}",
            DECISION_GATE_BLOCK_END,
        ]
    )
    updated_description = _upsert_decision_gate_clarifications_block(
        current_description=current_description,
        block=clarification_block,
    )
    return updated_summary, updated_description


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
    resolve_codex_working_dir: Callable[..., str],
    search_issues_for_tenant: Callable[..., Any],
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
        ensure_issue_is_executable(
            issue_status=issue_preview.status,
            tenant=tenant,
            extra_executable_statuses=("In Progress",),
        )
        issue_description: str | None = None
        issue_labels: list[str] | None = None
        try:
            issue_detail = fetch_issue_detail(session=session, tenant=tenant, issue_key=issue_key)
            refreshed_description = str(getattr(issue_detail, "description", "") or "").strip()
            if refreshed_description:
                issue_description = refreshed_description
            labels_raw = getattr(issue_detail, "labels", None)
            if isinstance(labels_raw, list):
                issue_labels = [str(label).strip() for label in labels_raw if str(label).strip()]
        except HTTPException:
            issue_description = None
            issue_labels = None
        precheck_description = _build_precheck_description(
            issue_description=issue_description,
            issue_key=issue_key,
            project_key=project.jira_project_key,
            search_issues_for_tenant=search_issues_for_tenant,
            session=session,
            tenant=tenant,
        )
        pre_check = evaluate_pre_run_check(
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=precheck_description,
            issue_labels=issue_labels,
            ready_label=(tenant.jira_config or {}).get("ready_label"),
        )
        if pre_check.decision_gate_triggered:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_decision_gate_remaining_questions_message(
                    issue_key=issue_key,
                    reason=str(pre_check.decision_gate.reason or "").strip(),
                    questions=[question.strip() for question in pre_check.decision_gate.questions if question.strip()],
                ),
            )
        if not pre_check.gtd_valid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_gtd_missing_message(
                    issue_key=issue_key,
                    missing_criteria=pre_check.gtd_missing_criteria,
                    questions=pre_check.gtd_clarification_questions,
                ),
            )
        if pre_check.outcome == "missing_ready_label":
            ready_label = str(pre_check.ready_label or "").strip()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{enqueue_reason_guidance('missing_ready_label')} ({ready_label})",
            )
        enqueue_result = enqueue_run(
            session,
            tenant_id=tenant_id,
            project_id=project.project_id,
            issue_key=issue_key,
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
        ensure_issue_is_executable(
            issue_status=issue_preview.status,
            tenant=tenant,
            extra_executable_statuses=("In Progress",),
        )
        issue_description = run.issue_description
        issue_labels: list[str] | None = None
        try:
            issue_detail = fetch_issue_detail(session=session, tenant=tenant, issue_key=run.issue_key)
            refreshed_description = str(getattr(issue_detail, "description", "") or "").strip()
            if refreshed_description:
                issue_description = refreshed_description
            labels_raw = getattr(issue_detail, "labels", None)
            if isinstance(labels_raw, list):
                issue_labels = [str(label).strip() for label in labels_raw if str(label).strip()]
        except HTTPException:
            issue_description = run.issue_description
            issue_labels = None
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
        precheck_description = _build_precheck_description(
            issue_description=issue_description,
            issue_key=run.issue_key,
            project_key=project.jira_project_key,
            search_issues_for_tenant=search_issues_for_tenant,
            session=session,
            tenant=tenant,
        )
        pre_check = evaluate_pre_run_check(
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=run.issue_key,
            issue_summary=issue_preview.summary,
            issue_description=precheck_description,
            issue_labels=issue_labels,
            ready_label=(tenant.jira_config or {}).get("ready_label"),
        )
        if pre_check.decision_gate_triggered:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_decision_gate_remaining_questions_message(
                    issue_key=run.issue_key,
                    reason=str(pre_check.decision_gate.reason or "").strip(),
                    questions=[question.strip() for question in pre_check.decision_gate.questions if question.strip()],
                ),
            )
        if not pre_check.gtd_valid:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_gtd_missing_message(
                    issue_key=run.issue_key,
                    missing_criteria=pre_check.gtd_missing_criteria,
                    questions=pre_check.gtd_clarification_questions,
                ),
            )
        if pre_check.outcome == "missing_ready_label":
            ready_label = str(pre_check.ready_label or "").strip()
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{enqueue_reason_guidance('missing_ready_label')} ({ready_label})",
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
        has_retryable_run = run is not None
        settings = settings_factory()
        issue_labels: list[str] | None = None
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
            project = resolve_project_for_issue(
                session=session,
                tenant=tenant,
                issue_key=issue_key,
            )
            codex_working_dir = resolve_codex_working_dir(
                session=session,
                tenant=tenant,
                settings=settings,
                project_id=project.project_id,
                project_keys=[project.jira_project_key],
            )
            updated_summary, updated_description = _plan_decision_gate_jira_update(
                runtime=runtime,
                invocation_context=CodexInvocationContext(
                    channel="discord",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    command="reply",
                    stage="decision_gate_normalize",
                    working_dir=codex_working_dir,
                    issue_key=issue_key,
                ),
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
            labels_raw = getattr(issue_detail, "labels", None)
            issue_labels = [str(label).strip() for label in labels_raw if str(label).strip()] if isinstance(labels_raw, list) else None
        except (HTTPException, CodexRuntimeError, RuntimeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to update Jira context for `{issue_key}`: {exc}",
            ) from exc

        precheck_description = _build_precheck_description(
            issue_description=updated_description,
            issue_key=issue_key,
            project_key=project.jira_project_key,
            search_issues_for_tenant=search_issues_for_tenant,
            session=session,
            tenant=tenant,
        )
        pre_check = evaluate_pre_run_check(
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=issue_key,
            issue_summary=updated_summary,
            issue_description=precheck_description,
            issue_labels=issue_labels,
            ready_label=None,
        )
        if pre_check.decision_gate_triggered:
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=_decision_gate_remaining_questions_message(
                    issue_key=issue_key,
                    reason=str(pre_check.decision_gate.reason or "").strip(),
                    questions=[
                        question.strip()
                        for question in pre_check.decision_gate.questions
                        if str(question).strip()
                    ],
                ),
                data={
                    "issue_key": issue_key,
                    "recheck_required": True,
                    "decision_gate_reason": pre_check.decision_gate.reason,
                    "questions": [
                        question.strip()
                        for question in pre_check.decision_gate.questions
                        if str(question).strip()
                    ],
                },
            )
        if not pre_check.gtd_valid:
            questions = [question.strip() for question in pre_check.gtd_clarification_questions if question.strip()]
            missing = [item.strip() for item in pre_check.gtd_missing_criteria if item.strip()]
            lines = [f"Good To Do still needs clarification for `{issue_key}`."]
            if missing:
                lines.append("Missing criteria: " + ", ".join(missing))
            if questions:
                lines.append("Please reply with:")
                lines.extend(f"- {question}" for question in questions)
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message="\n".join(lines),
                data={
                    "issue_key": issue_key,
                    "recheck_required": True,
                    "gtd_missing_criteria": missing,
                    "questions": questions,
                },
            )

        if has_retryable_run:
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
                resolve_codex_working_dir=resolve_codex_working_dir,
                search_issues_for_tenant=search_issues_for_tenant,
            )

        # Pre-run clarification path: no retryable run exists yet, so queue initial run.
        return dispatch_run_control_command(
            session=session,
            tenant=tenant,
            tenant_id=tenant_id,
            payload=payload,
            command_name="run",
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
            resolve_codex_working_dir=resolve_codex_working_dir,
            search_issues_for_tenant=search_issues_for_tenant,
        )

    return None
