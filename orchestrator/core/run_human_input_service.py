from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.core.followup_context_service import (
    CLOSED_FOLLOWUP_CONTEXT_STATUS,
    FOLLOWUP_CONTEXT_HUMAN_INPUT,
    close_followup_contexts,
    upsert_followup_context,
)
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.run_enqueue_types import EnqueueFailureReason
from orchestrator.core.runs import (
    NON_TERMINAL_RUN_STATUSES,
    RUN_STATUS_BLOCKED,
    RUN_STATUS_WAITING_FOR_INPUT,
    RunBootstrap,
    enqueue_attempt_for_workflow_uncommitted,
    resolve_required_worker_capability_from_plan,
    resolve_precheck_outcome_from_plan,
)
from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.core.workflow.checkpoints import (
    checkpoint_kind_for_stage,
    normalize_checkpoint_stage,
    snapshot_checkpoint_for_run,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import WorkflowStageCheckpoint
from orchestrator.storage.models import Project, Run, RunHumanInputRequest, Tenant, WorkflowCheckpoint, WorkflowExecution

INPUT_STATUS_PENDING = "pending"
INPUT_STATUS_ANSWERED = "answered"
INPUT_STATUS_CONSUMED = "consumed"
INPUT_STATUS_EXPIRED = "expired"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _request_questions(request_context: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(request_context, dict):
        return []
    raw_questions = request_context.get("questions")
    if not isinstance(raw_questions, list):
        return []
    questions: list[dict[str, Any]] = []
    for item in raw_questions:
        if not isinstance(item, dict):
            continue
        question_text = str(item.get("question") or "").strip()
        if not question_text:
            continue
        options = [str(option).strip() for option in item.get("options", []) if str(option).strip()]
        question: dict[str, Any] = {
            "id": str(item.get("id") or "").strip() or None,
            "question": question_text,
        }
        if options:
            question["options"] = options
        questions.append(question)
    return questions


def _build_human_input_message(
    *,
    issue_key: str,
    source_stage: str,
    request_type: str,
    run_id: str,
    prompt: str,
    instructions: str | None,
    expected_reply_format: str | None,
    request_context: dict[str, Any] | None,
) -> str:
    message_lines = [
        f"Human input needed for `{issue_key}`",
        f"- Stage: {source_stage}",
        f"- Type: {request_type}",
        f"- Run: {run_id}",
        "",
        prompt,
    ]
    questions = _request_questions(request_context)
    if questions:
        message_lines.extend(["", "Please answer these items:"])
        for index, question in enumerate(questions, start=1):
            message_lines.append(f"{index}. {question['question']}")
            options = question.get("options")
            if isinstance(options, list):
                for option_index, option in enumerate(options, start=1):
                    message_lines.append(f"   {index}.{option_index} {option}")
    if instructions:
        message_lines.extend(["", f"Instructions: {instructions}"])
    elif questions:
        message_lines.extend(["", "Instructions: reply in this thread and answer each numbered item in order."])
    if expected_reply_format:
        message_lines.extend(["", f"Reply format: {expected_reply_format}"])
    message_lines.extend(["", "Reply in this thread. The value is transient and will only be used to resume this workflow."])
    return "\n".join(message_lines)


def create_human_input_request(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project,
    run: Run,
    issue_key: str,
    source_stage: str,
    request_type: str,
    prompt: str,
    instructions: str | None = None,
    expected_reply_format: str | None = None,
    request_context: dict[str, Any] | None = None,
    expires_in_minutes: int | None = None,
) -> RunHumanInputRequest:
    normalized_prompt = str(prompt or "").strip()
    normalized_request_type = str(request_type or "").strip().lower()
    normalized_stage = str(source_stage or "").strip().lower() or "orchestrated"
    if not normalized_prompt:
        raise ValueError("Human input prompt is required")
    if not normalized_request_type:
        raise ValueError("Human input request_type is required")

    workflow = (
        session.execute(
            select(WorkflowExecution)
            .where(WorkflowExecution.workflow_id == run.workflow_id)
            .with_for_update()
        )
        .scalars()
        .one_or_none()
    )
    if workflow is None:
        raise ValueError(f"Workflow for run {run.run_id} was not found")
    existing_request = session.execute(
        select(RunHumanInputRequest)
        .where(
            RunHumanInputRequest.workflow_id == run.workflow_id,
            RunHumanInputRequest.status == "pending",
        )
        .with_for_update()
        .order_by(RunHumanInputRequest.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing_request is not None:
        if str(existing_request.thread_channel_id or "").strip():
            raise ValueError("A pending human input request already exists for this workflow")
        _dispatch_human_input_request(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            run=run,
            request=existing_request,
        )
        return existing_request

    snapshot = ExecutionSnapshot.require(getattr(run, "plan", None), allow_empty=True)
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage=normalized_stage,
            attempt=max(1, int(getattr(run, "attempt_number", 1) or 1)),
            status="waiting_for_input",
            summary=f"Awaiting human input ({normalized_request_type})",
        )
    )
    checkpoint_kind = checkpoint_kind_for_stage(normalized_stage)
    checkpoint = snapshot_checkpoint_for_run(
        session,
        run=run,
        stage=normalized_stage,
        checkpoint_kind=checkpoint_kind,
        payload=snapshot.dump(),
    )
    now = _now()
    expires_at = now + timedelta(minutes=max(1, int(expires_in_minutes or 15)))
    request = RunHumanInputRequest(
        request_id=uuid4().hex,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        workflow_id=run.workflow_id,
        checkpoint_id=checkpoint.checkpoint_id,
        source_run_id=run.run_id,
        consumed_by_run_id=None,
        issue_key=str(issue_key or run.issue_key or "").strip(),
        source_stage=normalized_stage,
        request_type=normalized_request_type,
        prompt=normalized_prompt,
        instructions=str(instructions or "").strip() or None,
        expected_reply_format=str(expected_reply_format or "").strip() or None,
        status=INPUT_STATUS_PENDING,
        request_context_json=dict(request_context or {}),
        thread_channel_id=None,
        thread_message_id=None,
        answer_encrypted=None,
        answer_source_ref=None,
        answered_at=None,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    run.status = RUN_STATUS_WAITING_FOR_INPUT
    run.dispatch_claimed_at = None
    run.last_heartbeat_at = None
    run.worker_service_instance_id = None
    workflow.status = RUN_STATUS_WAITING_FOR_INPUT
    workflow.active_run_id = run.run_id
    workflow.latest_checkpoint_id = checkpoint.checkpoint_id
    workflow.blocked_reason = None
    workflow.updated_at = now
    session.add(request)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        existing_request = session.execute(
            select(RunHumanInputRequest)
            .where(
                RunHumanInputRequest.workflow_id == run.workflow_id,
                RunHumanInputRequest.status == INPUT_STATUS_PENDING,
            )
            .order_by(RunHumanInputRequest.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if existing_request is None:
            raise
        if str(existing_request.thread_channel_id or "").strip():
            raise ValueError("A pending human input request already exists for this workflow")
        _dispatch_human_input_request(
            session=session,
            settings=settings,
            tenant=tenant,
            project=project,
            run=run,
            request=existing_request,
        )
        return existing_request
    session.refresh(request)
    _dispatch_human_input_request(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        run=run,
        request=request,
    )
    session.refresh(request)
    return request


def pending_human_input_for_thread(
    *,
    session: Session,
    tenant_id: str,
    thread_channel_id: str,
) -> RunHumanInputRequest | None:
    normalized_thread_channel_id = str(thread_channel_id or "").strip()
    if not normalized_thread_channel_id:
        return None
    request = (
        session.execute(
            select(RunHumanInputRequest)
            .where(
                RunHumanInputRequest.tenant_id == tenant_id,
                RunHumanInputRequest.thread_channel_id == normalized_thread_channel_id,
                RunHumanInputRequest.status == INPUT_STATUS_PENDING,
            )
            .order_by(RunHumanInputRequest.created_at.desc())
        )
        .scalars()
        .first()
    )
    if request is None:
        return None
    expires_at = request.expires_at
    if isinstance(expires_at, datetime) and expires_at <= _now():
        _expire_human_input_request(session=session, request=request)
        return None
    return request


def pending_human_input_for_request_id(
    *,
    session: Session,
    tenant_id: str,
    request_id: str,
) -> RunHumanInputRequest | None:
    normalized_request_id = str(request_id or "").strip()
    if not normalized_request_id:
        return None
    request = session.get(RunHumanInputRequest, normalized_request_id)
    if request is None or str(request.tenant_id or "").strip() != str(tenant_id or "").strip():
        return None
    if str(request.status or "").strip().lower() != INPUT_STATUS_PENDING:
        return None
    expires_at = request.expires_at
    if isinstance(expires_at, datetime) and expires_at <= _now():
        _expire_human_input_request(session=session, request=request)
        return None
    return request


def answered_human_inputs_for_attempt(
    *,
    session: Session,
    settings,
    workflow_id: str,
    consumed_by_run_id: str,
) -> list[dict[str, str]]:
    rows = (
        session.execute(
            select(RunHumanInputRequest).where(
                RunHumanInputRequest.workflow_id == workflow_id,
                RunHumanInputRequest.consumed_by_run_id == consumed_by_run_id,
                RunHumanInputRequest.status == INPUT_STATUS_CONSUMED,
            )
        )
        .scalars()
        .all()
    )
    encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()
    values: list[dict[str, str]] = []
    for row in rows:
        if not row.answer_encrypted:
            continue
        values.append(
            {
                "request_id": row.request_id,
                "request_type": row.request_type,
                "prompt": row.prompt,
                "value": decrypt_value(ciphertext=row.answer_encrypted, encryption_key=encryption_key),
            }
        )
    return values


def _expire_human_input_request(*, session: Session, request: RunHumanInputRequest) -> None:
    if request.status == INPUT_STATUS_EXPIRED:
        return
    now = _now()
    request.status = INPUT_STATUS_EXPIRED
    request.updated_at = now
    run = session.get(Run, request.source_run_id)
    if run is not None and run.status == RUN_STATUS_WAITING_FOR_INPUT:
        run.status = RUN_STATUS_BLOCKED
        run.last_error = "human_input_expired"
        run.last_heartbeat_at = None
        run.worker_service_instance_id = None
        run.finished_at = run.finished_at or now
    workflow = session.get(WorkflowExecution, request.workflow_id)
    if workflow is not None and workflow.status == RUN_STATUS_WAITING_FOR_INPUT:
        workflow.status = RUN_STATUS_BLOCKED
        workflow.blocked_reason = "human_input_expired"
        workflow.last_error = "human_input_expired"
        workflow.updated_at = now
    close_followup_contexts(
        session=session,
        tenant_id=request.tenant_id,
        context_type=FOLLOWUP_CONTEXT_HUMAN_INPUT,
        request_id=request.request_id,
        status=CLOSED_FOLLOWUP_CONTEXT_STATUS,
    )
    session.commit()
    session.refresh(request)


def answer_human_input_request(
    *,
    session: Session,
    settings,
    request: RunHumanInputRequest,
    reply_text: str,
    source_ref: str | None,
) -> RunHumanInputRequest:
    if request.status == INPUT_STATUS_CONSUMED:
        return request
    if request.status == INPUT_STATUS_EXPIRED:
        raise ValueError("Human input request has expired")
    expires_at = request.expires_at
    if isinstance(expires_at, datetime) and expires_at <= _now():
        _expire_human_input_request(session=session, request=request)
        raise ValueError("Human input request has expired")
    if request.status == INPUT_STATUS_ANSWERED:
        return request
    if request.status != INPUT_STATUS_PENDING:
        raise ValueError("Human input request is not pending")

    encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()
    now = _now()
    request.answer_encrypted = encrypt_value(plaintext=str(reply_text or "").strip(), encryption_key=encryption_key)
    request.answer_source_ref = str(source_ref or "").strip() or None
    request.status = INPUT_STATUS_ANSWERED
    request.answered_at = now
    request.updated_at = now
    session.commit()
    session.refresh(request)
    return request


def resume_workflow_from_human_input_answer(
    *,
    session: Session,
    settings,
    request: RunHumanInputRequest,
) -> Run:
    request = (
        session.execute(
            select(RunHumanInputRequest)
            .where(RunHumanInputRequest.request_id == str(request.request_id))
            .with_for_update()
        )
        .scalars()
        .one_or_none()
    )
    if request is None:
        raise ValueError("Human input request was not found")
    if request.status == INPUT_STATUS_CONSUMED and request.consumed_by_run_id:
        existing_run = session.get(Run, request.consumed_by_run_id)
        if existing_run is not None:
            return existing_run
    if request.status != INPUT_STATUS_ANSWERED:
        raise ValueError("Human input request is not answered")
    existing_resume_run = _existing_resume_run_for_request(session=session, request=request)
    if existing_resume_run is not None:
        request.status = INPUT_STATUS_CONSUMED
        request.consumed_by_run_id = existing_resume_run.run_id
        request.updated_at = _now()
        close_followup_contexts(
            session=session,
            tenant_id=request.tenant_id,
            context_type=FOLLOWUP_CONTEXT_HUMAN_INPUT,
            request_id=request.request_id,
            status=CLOSED_FOLLOWUP_CONTEXT_STATUS,
        )
        session.commit()
        session.refresh(request)
        session.refresh(existing_resume_run)
        return existing_resume_run
    source_run = session.get(Run, request.source_run_id)
    if source_run is None:
        raise ValueError("Source run for human input request was not found")
    checkpoint = session.get(WorkflowCheckpoint, request.checkpoint_id)
    if checkpoint is None:
        raise ValueError("Checkpoint for human input request was not found")
    checkpoint_plan_snapshot = ExecutionSnapshot.require(checkpoint.payload_json, allow_empty=True)
    persisted_precheck_outcome = (
        str(getattr(source_run, "pre_check_outcome", "") or "").strip()
        or resolve_precheck_outcome_from_plan(checkpoint.payload_json)
        or resolve_precheck_outcome_from_plan(getattr(source_run, "plan", None))
    )
    persisted_required_worker_capability = (
        str(getattr(source_run, "required_worker_capability", "") or "").strip()
        or resolve_required_worker_capability_from_plan(checkpoint.payload_json)
        or resolve_required_worker_capability_from_plan(getattr(source_run, "plan", None))
    )
    if persisted_precheck_outcome is not None:
        checkpoint_plan_snapshot.context.execution_context["pre_check_outcome"] = persisted_precheck_outcome
    checkpoint_plan_snapshot.context.execution_context["human_input_request_id"] = request.request_id
    checkpoint_plan = checkpoint_plan_snapshot.dump()

    enqueue_result = enqueue_attempt_for_workflow_uncommitted(
        session,
        workflow_id=request.workflow_id,
        bootstrap=RunBootstrap(
            workflow_id=request.workflow_id,
            parent_run_id=request.source_run_id,
            entry_mode="resume",
            entry_stage=normalize_checkpoint_stage(request.source_stage),
            entry_checkpoint_id=request.checkpoint_id,
            plan=checkpoint_plan,
            branch=source_run.branch,
            pr_url=source_run.pr_url,
            precheck_outcome=persisted_precheck_outcome,
            required_worker_capability=persisted_required_worker_capability,
        ),
    )
    if not enqueue_result.enqueued:
        if str(getattr(enqueue_result.reason, "value", enqueue_result.reason) or "").strip() == EnqueueFailureReason.RUN_ALREADY_ACTIVE.value:
            if _run_matches_human_input_request(run=enqueue_result.run, request_id=request.request_id):
                request.status = INPUT_STATUS_CONSUMED
                request.consumed_by_run_id = enqueue_result.run.run_id
                request.updated_at = _now()
                close_followup_contexts(
                    session=session,
                    tenant_id=request.tenant_id,
                    context_type=FOLLOWUP_CONTEXT_HUMAN_INPUT,
                    request_id=request.request_id,
                    status=CLOSED_FOLLOWUP_CONTEXT_STATUS,
                )
                session.commit()
                session.refresh(request)
                session.refresh(enqueue_result.run)
                return enqueue_result.run
            raise ValueError("Workflow already has an active resume run that is unrelated to this human-input request")
        raise ValueError(f"Unable to enqueue resumed run: {enqueue_result.reason}")

    request.status = INPUT_STATUS_CONSUMED
    request.consumed_by_run_id = enqueue_result.run.run_id
    request.updated_at = _now()
    close_followup_contexts(
        session=session,
        tenant_id=request.tenant_id,
        context_type=FOLLOWUP_CONTEXT_HUMAN_INPUT,
        request_id=request.request_id,
        status=CLOSED_FOLLOWUP_CONTEXT_STATUS,
    )
    session.commit()
    session.refresh(enqueue_result.run)
    session.refresh(request)
    return enqueue_result.run


def _existing_resume_run_for_request(*, session: Session, request: RunHumanInputRequest) -> Run | None:
    normalized_source_run_id = str(request.source_run_id or "").strip()
    normalized_checkpoint_id = str(request.checkpoint_id or "").strip()
    if not normalized_source_run_id or not normalized_checkpoint_id:
        return None
    candidates = (
        session.execute(
            select(Run)
            .where(
                Run.workflow_id == request.workflow_id,
                Run.parent_run_id == normalized_source_run_id,
                Run.entry_checkpoint_id == normalized_checkpoint_id,
                Run.status.in_(NON_TERMINAL_RUN_STATUSES),
            )
            .order_by(Run.attempt_number.desc())
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        if _run_matches_human_input_request(run=candidate, request_id=request.request_id):
            return candidate
    return None


def _run_matches_human_input_request(*, run: Run, request_id: str) -> bool:
    if not str(request_id or "").strip():
        return False
    snapshot = ExecutionSnapshot.load(getattr(run, "plan", None))
    if snapshot is None:
        return False
    value = str(snapshot.context.execution_context.get("human_input_request_id") or "").strip()
    return value == str(request_id).strip()


def _dispatch_human_input_request(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project,
    run: Run,
    request: RunHumanInputRequest,
) -> None:
    send_result = send_tenant_discord_message(
        session=session,
        tenant=tenant,
        project=project,
        message=_build_human_input_message(
            issue_key=request.issue_key,
            source_stage=request.source_stage,
            request_type=request.request_type,
            run_id=run.run_id,
            prompt=request.prompt,
            instructions=request.instructions,
            expected_reply_format=request.expected_reply_format,
            request_context=request.request_context_json,
        ),
        settings=settings,
        event=None,
        open_thread=True,
        thread_name=f"{tenant.tenant_id}-{request.issue_key}-input",
        thread_intro="Continue here with the requested input.",
    )
    if not send_result.sent or not str(send_result.thread_channel_id or "").strip():
        request.updated_at = _now()
        session.commit()
        raise ValueError(f"Unable to send human-input request to Discord: {send_result.reason}")

    request.thread_channel_id = str(send_result.thread_channel_id or "").strip()
    request.thread_message_id = str(send_result.message_id or "").strip() or None
    request.updated_at = _now()
    upsert_followup_context(
        session=session,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        context_type=FOLLOWUP_CONTEXT_HUMAN_INPUT,
        channel_id=str(send_result.channel_id or "").strip() or None,
        thread_channel_id=request.thread_channel_id,
        root_message_id=request.thread_message_id,
        origin_command="human_input",
        issue_key=request.issue_key,
        request_id=request.request_id,
        run_id=run.run_id,
        metadata={
            "request_id": request.request_id,
            "issue_key": request.issue_key,
            "source_stage": request.source_stage,
            "workflow_id": request.workflow_id,
        },
    )
    session.commit()
