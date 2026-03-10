from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.api.schemas import DiscordCommandRequest, DiscordCommandResponse
from orchestrator.core.codex_invocation import CodexInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.communications.command_pipeline import CommandScope
from orchestrator.core.communications.enqueue_reason_contract import (
    enqueue_reason_guidance,
    format_enqueue_conflict_detail,
)
from orchestrator.core.decision_engine import (
    DecisionEngineResult,
    DecisionEventInput,
    DecisionSource,
    evaluate_decision_event,
)
from orchestrator.core.decision_reply_service import (
    capture_decision_reply,
)
from orchestrator.core.decision_effect_service import publish_decision_effects
from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.precheck_decision import build_precheck_message
from orchestrator.core.precheck_question_lock import (
    build_precheck_questions_block,
    remove_precheck_questions_block,
    upsert_precheck_questions_block,
)
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.run_gate_service import enqueue_issue_run_with_precheck, resolve_run_gate_block
from orchestrator.core.runs import cancel_run
from orchestrator.storage.models import Run, Tenant

logger = logging.getLogger(__name__)

def _oauth_context_value(oauth_context: Any, field: str) -> Any:
    if isinstance(oauth_context, dict):
        return oauth_context.get(field)
    return getattr(oauth_context, field, None)


def _evaluate_precheck_decision_with_labels(
    *,
    session: Session,
    tenant: Tenant,
    project: Any,  # noqa: ANN401
    source: DecisionSource,
    issue_key: str,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    settings_factory: Callable[[], Any],
    tenant_jira_oauth_context: Callable[..., Any],
    oauth_context: Any | None = None,
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None = None,
) -> DecisionEngineResult:
    settings = settings_factory()
    return evaluate_decision_event(
        session=session,
        tenant=tenant,
        project=project,
        event=DecisionEventInput(
            source=source,
            event_type=f"discord_{source}",
            idempotency_key=None,
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
        ),
        settings=settings,
        tenant_jira_oauth_context_fn=tenant_jira_oauth_context,
        oauth_context=oauth_context,
        publish_jira_comment_fn=publish_jira_comment_fn,
        evaluate_pre_run_check_fn=evaluate_pre_run_check,
    )


def _decision_gate_remaining_questions_message(*, issue_key: str, reason: str, questions: list[str]) -> str:
    lines = [
        f"Decision Gate still needs clarification for `{issue_key}`.",
        f"Reason: {reason}",
    ]
    if questions:
        lines.append("Please reply with:")
        lines.extend(f"- {question}" for question in questions[:5])
    return "\n".join(lines)


def _decision_gate_unresolved_feedback_message(
    *,
    issue_key: str,
    reason: str,
    question_feedback: list[dict[str, str]],
) -> tuple[str, list[str]]:
    lines = [
        f"Decision Gate still needs clarification for `{issue_key}`.",
        f"Reason: {reason}",
    ]
    questions: list[str] = []
    if question_feedback:
        lines.append("Please reply with:")
    for item in question_feedback[:5]:
        question_text = str(item.get("question_text") or "").strip()
        note = str(item.get("note") or "").strip()
        if question_text:
            lines.append(f"- {question_text}")
            questions.append(question_text)
        if note:
            lines.append(f"  Missing detail: {note}")
    return "\n".join(lines), questions


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


def _persist_precheck_questions_block(
    *,
    oauth_client,  # noqa: ANN001
    oauth_access_token: str,
    cloud_id: str,
    issue_key: str,
    issue_summary: str,
    current_description: str,
    decision_gate_reason: str | None,
    decision_gate_questions: list[str],
    gtd_questions: list[str],
) -> str:
    block = build_precheck_questions_block(
        decision_gate_reason=decision_gate_reason,
        decision_gate_questions=decision_gate_questions,
        gtd_questions=gtd_questions,
    )
    next_description = (
        upsert_precheck_questions_block(current_description=current_description, block=block)
        if block
        else remove_precheck_questions_block(current_description=current_description)
    )
    if next_description.strip() == str(current_description or "").strip():
        return current_description
    oauth_client.update_issue_summary_and_description(
        access_token=oauth_access_token,
        cloud_id=cloud_id,
        issue_id_or_key=issue_key,
        summary=issue_summary,
        description=next_description,
    )
    return next_description


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
    settings_factory: Callable[[], Any],
    tenant_jira_oauth_context: Callable[..., Any],
    conflict_prefix: str,
    success_message: str,
) -> DiscordCommandResponse:
    decision_result = _evaluate_precheck_decision_with_labels(
        session=session,
        tenant=tenant,
        project=project,
        source=source,
        issue_key=issue_key,
        issue_summary=issue_summary,
        issue_description=issue_description,
        issue_labels=issue_labels,
        settings_factory=settings_factory,
        tenant_jira_oauth_context=tenant_jira_oauth_context,
    )
    precheck_decision = decision_result.decision
    gate_block = resolve_run_gate_block(decision_result=decision_result)
    if gate_block is not None and gate_block.reason == "policy_eval_failed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=enqueue_reason_guidance("policy_eval_failed"),
        )
    if gate_block is not None and gate_block.reason == "decision_gate_required":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_decision_gate_remaining_questions_message(
                issue_key=issue_key,
                reason=str(gate_block.decision_gate_reason or "").strip(),
                questions=list(gate_block.decision_gate_questions),
            ),
        )
    if gate_block is not None and gate_block.reason == "gtd_required":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_gtd_missing_message(
                issue_key=issue_key,
                missing_criteria=gate_block.gtd_missing_criteria,
                questions=gate_block.gtd_questions,
            ),
        )
    if gate_block is not None and gate_block.reason == "missing_ready_label":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{enqueue_reason_guidance('missing_ready_label')} ({str(gate_block.ready_label or '').strip()})",
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
        precheck_outcome=precheck_decision.pre_check.outcome if precheck_decision.pre_check is not None else None,
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


def _locked_decision_gate_reason(*, classification: str, decision_gate: Any | None) -> str | None:
    if classification not in {"decision_gate", "both"}:
        return None
    return str(getattr(decision_gate, "reason", "") or "").strip() or None


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
            settings_factory=settings_factory,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            conflict_prefix="Retry could not be queued",
            success_message="Queued retry run {run_id} for {issue_key}",
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
            capture = capture_decision_reply(
                session=session,
                settings=settings,
                tenant=tenant,
                project=project,
                issue_key=issue_key,
                reply_text=reply_text,
                source_transport="discord",
                actor_ref=payload.user_id,
                metadata={
                    "channel_id": payload.channel_id,
                    "ingress": "discord",
                },
            )
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

        decision_result = _evaluate_precheck_decision_with_labels(
            session=session,
            tenant=tenant,
            project=project,
            source="discord_reply",
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
            settings_factory=settings_factory,
            tenant_jira_oauth_context=tenant_jira_oauth_context,
            oauth_context=oauth,
            publish_jira_comment_fn=_publish_jira_comment,
        )
        if capture.effect_ids:
            publish_decision_effects(
                session=session,
                effect_ids=capture.effect_ids,
                occurred_at=datetime.now(timezone.utc),
            )
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
        classification = decision_result.classification
        missing_slots = decision_result.missing_slots
        auto_resolved_slots = list(decision_result.auto_resolved_slots)

        if classification != "clear":
            decision_gate = getattr(pre_check, "decision_gate", None)
            locked_decision_gate_reason = _locked_decision_gate_reason(
                classification=classification,
                decision_gate=decision_gate,
            )
            decision_gate_questions = [
                question.strip()
                for question in getattr(decision_gate, "questions", ())
                if str(question).strip()
            ]
            gtd_questions = [
                question.strip()
                for question in getattr(pre_check, "gtd_clarification_questions", ())
                if str(question).strip()
            ]
            gtd_missing = [
                item.strip()
                for item in getattr(pre_check, "gtd_missing_criteria", ())
                if str(item).strip()
            ]
            unresolved_question_feedback = [
                item
                for item in getattr(capture, "unresolved_question_feedback", ())
                if isinstance(item, dict)
            ]
            if unresolved_question_feedback and classification in {"decision_gate", "both"}:
                message, generated_questions = _decision_gate_unresolved_feedback_message(
                    issue_key=issue_key,
                    reason=locked_decision_gate_reason or "clarification required",
                    question_feedback=unresolved_question_feedback,
                )
            else:
                message, generated_questions = build_precheck_message(
                    runtime=runtime,
                    invocation_context=CodexInvocationContext(
                        channel="discord",
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                        command="reply",
                        stage="precheck_message",
                        working_dir=str(codex_working_dir or "."),
                        issue_key=issue_key,
                    ),
                    issue_key=issue_key,
                    classification=classification,
                    decision_gate_reason=locked_decision_gate_reason,
                    decision_gate_questions=decision_gate_questions,
                    gtd_missing_criteria=gtd_missing,
                    gtd_questions=gtd_questions,
                    missing_slots=missing_slots,
                )
            return DiscordCommandResponse(
                ok=True,
                command=command_name,
                message=message,
                data={
                    "issue_key": issue_key,
                    "recheck_required": True,
                    "classification": classification,
                    "decision_gate_reason": locked_decision_gate_reason,
                    "gtd_missing_criteria": gtd_missing,
                    "questions": generated_questions or decision_gate_questions or gtd_questions,
                    "question_feedback": unresolved_question_feedback,
                    "missing_slots": missing_slots,
                    "auto_resolved_slots": auto_resolved_slots,
                    "knowledge_mode": None,
                },
            )

        issue_preview = fetch_issue_preview(session=session, tenant=tenant, issue_key=issue_key)
        ensure_issue_is_executable(
            issue_status=issue_preview.status,
            tenant=tenant,
            extra_executable_statuses=("In Progress",),
        )
        return _queue_run_from_issue_context(
            session=session,
            tenant=tenant,
            tenant_id=tenant_id,
            project=project,
            source="discord_retry" if has_retryable_run else "discord_run",
            issue_key=issue_key,
            issue_summary=issue_preview.summary,
            issue_description=issue_description,
            issue_labels=issue_labels,
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
