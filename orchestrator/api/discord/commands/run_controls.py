from __future__ import annotations

from collections.abc import Callable
import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.core.communications.enqueue_reason_contract import (
    enqueue_reason_guidance,
    format_enqueue_conflict_detail,
)
from orchestrator.core.communications.decision_clarification_presentation import (
    build_decision_clarification_presentation,
    load_cycle_question_feedback,
    render_decision_gate_feedback_message,
    render_decision_gate_remaining_questions_message,
)
from orchestrator.core.communications.execution_admission_format import (
    format_discord_admission_conflict_detail,
)
from orchestrator.core.decision_engine import DecisionEventInput, DecisionSource
from orchestrator.core.decision_clarification_port import DecisionClarificationPort
from orchestrator.core.decision_state_machine import resolve_execution_admission
from orchestrator.core.followup_context_service import (
    FOLLOWUP_CONTEXT_DECISION_GATE,
    close_followup_contexts,
)
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.precheck_decision import build_precheck_message
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.run_gate_service import enqueue_issue_run_with_precheck
from orchestrator.core.runs import cancel_run
from orchestrator.storage.models import Run, Tenant

logger = logging.getLogger(__name__)


def _oauth_context_value(oauth_context: Any, field: str) -> Any:
    if isinstance(oauth_context, dict):
        return oauth_context.get(field)
    return getattr(oauth_context, field, None)


def _is_knowledge_enabled_for_project(*, tenant_policy: dict, project_overrides: dict) -> tuple[bool, str]:
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant_policy,
        project_overrides=project_overrides,
    )
    enabled = bool(effective_policy.get("knowledge_base_enabled", True))
    mode = str(effective_policy.get("knowledge_auto_answer_mode") or "").strip().lower()
    if mode not in {"safe", "balanced", "aggressive"}:
        mode = "aggressive"
    return enabled, mode


def _queue_run_from_issue_context(
    *,
    session: Session,
    tenant: Tenant,
    tenant_id: str,
    project: Any,  # noqa: ANN401
    source: DecisionSource,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    decision_clarification_port: DecisionClarificationPort,
    settings_factory: Callable[[], Any],
    tenant_jira_oauth_context: Callable[..., Any],
    conflict_prefix: str,
    success_message: str,
) -> DiscordCommandResponse:
    settings = settings_factory()
    decision_result = decision_clarification_port.evaluate_issue_clarification_state(
        session=session,
        tenant=tenant,
        project=project,
        event=DecisionEventInput(
            source=source,
            event_type="discord_run_control",
            idempotency_key=None,
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
        ),
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
        evaluate_pre_run_check_fn=evaluate_pre_run_check,
        oauth_context=None,
        publish_jira_comment_fn=None,
    )
    admission = resolve_execution_admission(decision_result=decision_result)
    if admission.blocked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=format_discord_admission_conflict_detail(admission=admission),
        )
    enqueue_result = enqueue_issue_run_with_precheck(
        session,
        tenant_id=tenant_id,
        project_id=project.project_id,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        repo_url=project.github_repository,
        delivery_id=None,
        precheck_outcome=admission.precheck_outcome,
        max_concurrent_runs=resolve_effective_policy(
            tenant_policy=tenant.policy_config,
            project_overrides=project.policy_overrides,
        ).get("max_concurrent_runs"),
    )
    if not enqueue_result.enqueued:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=format_enqueue_conflict_detail(
                prefix=conflict_prefix,
                enqueue_reason=str(enqueue_result.reason),
                enqueue_run_obj=enqueue_result.run,
            ),
        )
    return DiscordCommandResponse(
        ok=True,
        command="retry" if source == "discord_retry" else "run",
        message=success_message.format(run_id=enqueue_result.run.run_id, issue_key=issue_key),
        data={"run_id": enqueue_result.run.run_id, "issue_key": issue_key},
    )


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
    decision_clarification_port: DecisionClarificationPort,
    resolve_project_for_issue: Callable[..., Any],
    fetch_issue_preview: Callable[..., Any],
    fetch_issue_detail: Callable[..., Any],
    settings_factory: Callable[[], Any],
    build_codex_runtime: Callable[..., Any],
    tenant_jira_oauth_context: Callable[..., Any],
    ensure_issue_is_executable: Callable[..., Any],
    resolve_codex_working_dir: Callable[..., str],
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
        return _queue_run_from_issue_context(
            session=session,
            tenant=tenant,
            tenant_id=tenant_id,
            project=project,
            source="discord_run",
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
            decision_clarification_port=decision_clarification_port,
            settings_factory=settings_factory,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            conflict_prefix="Run could not be queued",
            success_message="Queued run {run_id} for {issue_key}",
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
        return _queue_run_from_issue_context(
            session=session,
            tenant=tenant,
            tenant_id=tenant_id,
            project=project,
            source="discord_retry",
            issue_key=run.issue_key,
            issue_summary=issue_preview.summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
            decision_clarification_port=decision_clarification_port,
            settings_factory=settings_factory,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            conflict_prefix="Retry could not be queued",
            success_message="Queued retry run {run_id} for {issue_key}",
        )

    if command_name == "reply":
        command_params = payload.command_params if isinstance(payload.command_params, dict) else {}
        issue_key = str(command_params.get("issue_key") or "").strip().upper()
        reply_text = str(command_params.get("reply_text") or "").strip()
        source_ref = str(command_params.get("source_ref") or "").strip() or None
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
        issue_summary: str | None = None
        issue_description: str | None = None
        codex_working_dir: str | None = None
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
            issue_summary = str(getattr(issue_detail, "summary", "") or "").strip() or None
            issue_description = str(getattr(issue_detail, "description", "") or "").strip() or None
            labels_raw = getattr(issue_detail, "labels", None)
            issue_labels = [str(label).strip() for label in labels_raw if str(label).strip()] if isinstance(labels_raw, list) else None
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"No active Decision Gate cycle exists for `{issue_key}`. "
                    f"Run `!run {issue_key}` or `!retry {issue_key}` to reopen clarification first."
                ),
            ) from exc
        except (HTTPException, CodexRuntimeError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"Failed to update Jira context for `{issue_key}`: {exc}",
            ) from exc

        def _publish_jira_comment(comment: str) -> tuple[bool, str | None]:
            try:
                oauth_client.add_issue_comment(
                    access_token=oauth_access_token,
                    cloud_id=str(cloud_id),
                    issue_id_or_key=issue_key,
                    comment=comment,
                )
                return True, None
            except Exception as exc:  # noqa: BLE001
                return False, str(exc)

        try:
            reply_result = decision_clarification_port.capture_decision_reply_and_recheck(
                session=session,
                settings=settings,
                tenant=tenant,
                project=project,
                issue_key=issue_key,
                reply_text=reply_text,
                source_transport="discord",
                source_ref=source_ref,
                actor_ref=payload.user_id,
                metadata={
                    "channel_id": payload.channel_id,
                    "ingress": "discord",
                },
                decision_event_factory=lambda capture: DecisionEventInput(
                    source="discord_reply",
                    event_type="discord_discord_reply",
                    idempotency_key=f"decision-reply:{capture.evidence_id}",
                    issue_key=issue_key,
                    issue_summary=issue_summary,
                    issue_description=issue_description,
                    issue_labels=issue_labels,
                ),
                tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
                evaluate_pre_run_check_fn=evaluate_pre_run_check,
                oauth_context=oauth,
                publish_jira_comment_fn=_publish_jira_comment,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"No active Decision Gate cycle exists for `{issue_key}`. "
                    f"Run `!run {issue_key}` or `!retry {issue_key}` to reopen clarification first."
                ),
            ) from exc
        decision_result = reply_result.decision_result
        precheck_decision = decision_result.decision
        if precheck_decision.pre_check is None:
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=enqueue_reason_guidance("policy_eval_failed"),
                data={
                    "issue_key": issue_key,
                    "recheck_required": True,
                    "policy_error": True,
                },
            )
        pre_check = precheck_decision.pre_check
        clarification_presentation = build_decision_clarification_presentation(
            decision_result=decision_result,
            question_feedback=load_cycle_question_feedback(
                session=session,
                cycle_id=str(decision_result.cycle_id or ""),
            ),
        )
        if clarification_presentation.recheck_required:
            if (
                clarification_presentation.requires_decision_gate_feedback
                and clarification_presentation.question_feedback
            ):
                message = render_decision_gate_feedback_message(
                    issue_key=issue_key,
                    reason=clarification_presentation.decision_gate_reason or "clarification required",
                    question_feedback=clarification_presentation.question_feedback,
                )
                generated_questions = list(clarification_presentation.questions)
            elif clarification_presentation.requires_decision_gate_feedback:
                message = render_decision_gate_remaining_questions_message(
                    issue_key=issue_key,
                    reason=clarification_presentation.decision_gate_reason or "clarification required",
                    questions=clarification_presentation.decision_gate_questions,
                )
                generated_questions = list(clarification_presentation.questions)
            else:
                message, generated_questions = build_precheck_message(
                    runtime=runtime,
                    invocation_context=AgentInvocationContext(
                        channel="discord",
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                        command="reply",
                        stage="precheck_message",
                        working_dir=str(codex_working_dir or "."),
                        issue_key=issue_key,
                    ),
                    issue_key=issue_key,
                    classification=clarification_presentation.mode.value,
                    decision_gate_reason=clarification_presentation.decision_gate_reason or "",
                    decision_gate_questions=list(clarification_presentation.decision_gate_questions),
                    gtd_missing_criteria=list(clarification_presentation.gtd_missing_criteria),
                    gtd_questions=list(clarification_presentation.gtd_questions),
                    missing_slots=list(clarification_presentation.missing_slots),
                )
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=message,
                data={
                    "issue_key": issue_key,
                    "recheck_required": True,
                    "classification": clarification_presentation.mode.value,
                    "decision_gate_reason": clarification_presentation.decision_gate_reason,
                    "gtd_missing_criteria": list(clarification_presentation.gtd_missing_criteria),
                    "questions": generated_questions or list(clarification_presentation.questions),
                    "question_feedback": list(clarification_presentation.question_feedback),
                    "missing_slots": list(clarification_presentation.missing_slots),
                    "auto_resolved_slots": list(clarification_presentation.auto_resolved_slots),
                    "knowledge_mode": None,
                },
            )

        thread_channel_id = str(payload.channel_id or "").strip()
        close_followup_contexts(
            session=session,
            tenant_id=tenant.tenant_id,
            context_type=FOLLOWUP_CONTEXT_DECISION_GATE,
            issue_key=issue_key,
            thread_channel_id=thread_channel_id or None,
        )

        issue_preview = fetch_issue_preview(session=session, tenant=tenant, issue_key=issue_key)
        ensure_issue_is_executable(
            issue_status=issue_preview.status,
            tenant=tenant,
            extra_executable_statuses=("In Progress",),
        )
        rechecked_issue_labels = getattr(decision_result, "issue_labels", None)
        effective_issue_labels = list(rechecked_issue_labels or issue_labels or [])
        pre_check = getattr(getattr(decision_result, "decision", None), "pre_check", None)
        ready_label = str(getattr(pre_check, "ready_label", "") or "").strip()
        ready_label_present = bool(getattr(pre_check, "ready_label_present", False))
        if ready_label and ready_label_present and ready_label not in effective_issue_labels:
            effective_issue_labels.append(ready_label)
        return _queue_run_from_issue_context(
            session=session,
            tenant=tenant,
            tenant_id=tenant_id,
            project=project,
            source="discord_retry" if has_retryable_run else "discord_run",
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=issue_description,
            issue_labels=effective_issue_labels or None,
            decision_clarification_port=decision_clarification_port,
            settings_factory=settings_factory,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            conflict_prefix="Retry could not be queued" if has_retryable_run else "Run could not be queued",
            success_message=(
                "Queued retry run {run_id} for {issue_key}"
                if has_retryable_run
                else "Queued run {run_id} for {issue_key}"
            ),
        )

    return None
