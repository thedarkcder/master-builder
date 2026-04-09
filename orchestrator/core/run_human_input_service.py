from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.followup_context_service import (
    CLOSED_FOLLOWUP_CONTEXT_STATUS,
    FOLLOWUP_CONTEXT_HUMAN_INPUT,
    close_followup_contexts,
    upsert_followup_context,
)
from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.runs import RUN_STATUS_BLOCKED, RUN_STATUS_WAITING_FOR_INPUT
from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.core.workflow.checkpoints import (
    checkpoint_kind_for_stage,
    checkpoint_payload_for_plan,
    snapshot_checkpoint_for_run,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.temporal.human_input_orchestration import resume_human_input_request_via_temporal
from orchestrator.storage.models import Project, Run, RunHumanInputRequest, Tenant, WorkflowExecution

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

    existing_request = session.execute(
        select(RunHumanInputRequest)
        .where(
            RunHumanInputRequest.workflow_id == run.workflow_id,
            RunHumanInputRequest.status == "pending",
        )
        .order_by(RunHumanInputRequest.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing_request is not None:
        raise ValueError("A pending human input request already exists for this workflow")

    checkpoint_kind = checkpoint_kind_for_stage(normalized_stage)
    checkpoint = snapshot_checkpoint_for_run(
        session,
        run=run,
        stage=normalized_stage,
        checkpoint_kind=checkpoint_kind,
        payload=checkpoint_payload_for_plan(checkpoint_kind=checkpoint_kind or "orchestrated", plan=run.plan),
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
    send_result = send_tenant_discord_message(
        session=session,
        tenant=tenant,
        project=project,
        message=_build_human_input_message(
            issue_key=request.issue_key,
            source_stage=request.source_stage,
            request_type=request.request_type,
            run_id=run.run_id,
            prompt=normalized_prompt,
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
        raise ValueError(f"Unable to send human-input request to Discord: {send_result.reason}")

    request.thread_channel_id = str(send_result.thread_channel_id or "").strip()
    request.thread_message_id = str(send_result.message_id or "").strip() or None
    run.status = RUN_STATUS_WAITING_FOR_INPUT
    run.last_heartbeat_at = None
    run.worker_service_instance_id = None
    workflow = session.get(WorkflowExecution, run.workflow_id)
    if workflow is not None:
        workflow.status = RUN_STATUS_WAITING_FOR_INPUT
        workflow.active_run_id = run.run_id
        workflow.latest_checkpoint_id = checkpoint.checkpoint_id
        workflow.blocked_reason = None
        workflow.updated_at = now
    session.add(request)
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
    if request.status == INPUT_STATUS_CONSUMED and request.consumed_by_run_id:
        existing_run = session.get(Run, request.consumed_by_run_id)
        if existing_run is not None:
            return existing_run

    source_run = session.get(Run, request.source_run_id)
    source_snapshot = ExecutionSnapshot.load(getattr(source_run, "plan", None)) if source_run is not None else None
    temporal_owned_run = (
        source_snapshot is not None
        and isinstance(source_snapshot.context.execution_context.get("team_run"), dict)
    )
    if not temporal_owned_run:
        raise ValueError("Legacy workflow resume is no longer supported; historical workflows are read-only")

    resume_result = resume_human_input_request_via_temporal(settings=settings, request=request)
    session.expire_all()
    resumed_run_id = str(resume_result.resumed_run_id or request.consumed_by_run_id or "").strip()
    if resumed_run_id:
        resumed_run = session.get(Run, resumed_run_id)
        if resumed_run is not None:
            return resumed_run
    refreshed_request = session.get(RunHumanInputRequest, request.request_id)
    if refreshed_request is not None and refreshed_request.consumed_by_run_id:
        resumed_run = session.get(Run, refreshed_request.consumed_by_run_id)
        if resumed_run is not None:
            return resumed_run
    raise ValueError("Temporal workflow did not produce a resumed run")
