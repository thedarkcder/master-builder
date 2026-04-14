from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.discord.notifications import send_tenant_discord_message
from orchestrator.core.runs import enqueue_run
from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.storage.models import Project, Run, RunHumanInputRequest, Tenant


@dataclass(frozen=True)
class HumanInputResumeTarget:
    resume_stage: str
    resume_session_id: str
    resume_source_plan: dict[str, Any] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _coerce_resume_source_plan(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, dict) else None


def _extract_resume_source_plan(*, run: Run, request_context: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if isinstance(request_context, dict):
        for key in ("resume_source_plan", "plan"):
            candidate = _coerce_resume_source_plan(request_context.get(key))
            if candidate is not None:
                return candidate
    if isinstance(run.plan, dict):
        candidate = _coerce_resume_source_plan(run.plan.get("plan"))
        if candidate is not None:
            return candidate
        trigger_context = run.plan.get("trigger_context")
        if isinstance(trigger_context, dict):
            candidate = _coerce_resume_source_plan(trigger_context.get("resume_source_plan"))
            if candidate is not None:
                return candidate
    return None


def _resume_target_for_run(*, run: Run, stage: str) -> HumanInputResumeTarget:
    normalized_stage = str(stage or "").strip().lower()
    if normalized_stage == "pm":
        session_id = str(getattr(run, "pm_session_id", "") or "").strip()
        if not session_id:
            raise ValueError("No PM session is available for human-input resume")
        return HumanInputResumeTarget(resume_stage="pm", resume_session_id=session_id)
    if normalized_stage in {"dev", "test", "review"}:
        session_id = str(getattr(run, "dev_session_id", "") or "").strip()
        if not session_id:
            raise ValueError("No Dev session is available for human-input resume")
        return HumanInputResumeTarget(
            resume_stage="dev",
            resume_session_id=session_id,
            resume_source_plan=_extract_resume_source_plan(run=run),
        )
    session_id = str(getattr(run, "orchestrated_session_id", "") or "").strip()
    if not session_id:
        raise ValueError("No orchestrated session is available for human-input resume")
    return HumanInputResumeTarget(resume_stage="orchestrated", resume_session_id=session_id)


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
    if not normalized_prompt:
        raise ValueError("Human input prompt is required")
    if not normalized_request_type:
        raise ValueError("Human input request_type is required")
    normalized_request_context = dict(request_context or {})
    resume_source_plan = _extract_resume_source_plan(run=run, request_context=normalized_request_context)
    if resume_source_plan is not None and "resume_source_plan" not in normalized_request_context:
        normalized_request_context["resume_source_plan"] = dict(resume_source_plan)
    resume_target = _resume_target_for_run(run=run, stage=source_stage)
    now = _now()
    expires_at = now + timedelta(minutes=max(1, int(expires_in_minutes or 15)))
    request = RunHumanInputRequest(
        request_id=uuid4().hex,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        source_run_id=run.run_id,
        resumed_run_id=None,
        issue_key=str(issue_key or run.issue_key or "").strip(),
        source_stage=str(source_stage or "").strip().lower() or "orchestrator",
        resume_stage=resume_target.resume_stage,
        resume_session_id=resume_target.resume_session_id,
        request_type=normalized_request_type,
        prompt=normalized_prompt,
        instructions=str(instructions or "").strip() or None,
        expected_reply_format=str(expected_reply_format or "").strip() or None,
        status="pending",
        request_context_json=normalized_request_context,
        thread_channel_id=None,
        thread_message_id=None,
        answer_encrypted=None,
        answer_source_ref=None,
        answered_at=None,
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
    )
    message_lines = [
        f"Human input needed for `{request.issue_key}`",
        f"- Stage: {request.source_stage}",
        f"- Type: {request.request_type}",
        f"- Run: {run.run_id}",
        "",
        normalized_prompt,
    ]
    if request.instructions:
        message_lines.extend(["", f"Instructions: {request.instructions}"])
    if request.expected_reply_format:
        message_lines.extend(["", f"Reply format: {request.expected_reply_format}"])
    message_lines.extend(["", "Reply in this thread. The value is transient and will only be used to resume this run."])
    send_result = send_tenant_discord_message(
        session=session,
        tenant=tenant,
        project=project,
        message="\n".join(message_lines),
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
    session.add(request)
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
                RunHumanInputRequest.status == "pending",
            )
            .order_by(RunHumanInputRequest.created_at.desc())
        )
        .scalars()
        .first()
    )
    if request is None:
        return None
    if not isinstance(getattr(request, "request_id", None), str) or not str(request.request_id).strip():
        return None
    if str(getattr(request, "status", "") or "").strip().lower() != "pending":
        return None
    expires_at = getattr(request, "expires_at", None)
    if isinstance(expires_at, datetime) and expires_at <= _now():
        return None
    return request


def answered_human_inputs_for_request(
    *,
    session: Session,
    settings,
    request_ids: list[str],
) -> list[dict[str, str]]:
    normalized_ids = [str(value or "").strip() for value in request_ids if str(value or "").strip()]
    if not normalized_ids:
        return []
    rows = (
        session.execute(
            select(RunHumanInputRequest).where(
                RunHumanInputRequest.request_id.in_(normalized_ids),
                RunHumanInputRequest.status == "answered",
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


def resume_run_from_human_input_reply(
    *,
    session: Session,
    settings,
    request: RunHumanInputRequest,
    reply_text: str,
    source_ref: str | None,
) -> Run:
    from orchestrator.core.decision_engine import resolve_enqueue_precheck_outcome

    if request.status != "pending":
        raise ValueError("Human input request is not pending")
    source_run = session.get(Run, request.source_run_id)
    if source_run is None:
        raise ValueError("Source run for human input request was not found")
    encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()
    now = _now()
    request.answer_encrypted = encrypt_value(plaintext=str(reply_text or "").strip(), encryption_key=encryption_key)
    request.answer_source_ref = str(source_ref or "").strip() or None
    request.status = "answered"
    request.answered_at = now
    request.updated_at = now

    enqueue_result = enqueue_run(
        session,
        tenant_id=source_run.tenant_id,
        project_id=source_run.project_id,
        issue_key=source_run.issue_key,
        issue_summary=source_run.issue_summary,
        issue_description=source_run.issue_description,
        repo_url=source_run.repo_url,
        delivery_id=None,
        precheck_outcome=resolve_enqueue_precheck_outcome(
            source="admin_rerun",
            precheck_source_plan=source_run.plan,
            issue_summary=source_run.issue_summary,
            issue_description=source_run.issue_description,
        ),
        precheck_source_plan=source_run.plan,
    )
    if not enqueue_result.enqueued:
        raise ValueError(f"Unable to enqueue resumed run: {enqueue_result.reason}")

    next_plan = dict(enqueue_result.run.plan or {})
    source_plan = source_run.plan if isinstance(source_run.plan, dict) else {}
    trigger_context = dict(source_plan.get("trigger_context")) if isinstance(source_plan.get("trigger_context"), dict) else {}
    trigger_context["rerun_mode"] = "resume"
    trigger_context["resume_stage"] = request.resume_stage
    trigger_context["resume_session_id"] = request.resume_session_id
    trigger_context["resume_source_run_id"] = request.source_run_id
    trigger_context["human_input_request_ids"] = [request.request_id]
    if request.resume_stage == "dev":
        source_plan_payload = _extract_resume_source_plan(
            run=source_run,
            request_context=request.request_context_json if isinstance(request.request_context_json, dict) else None,
        )
        if isinstance(source_plan_payload, dict):
            trigger_context["resume_source_plan"] = dict(source_plan_payload)
        enqueue_result.run.dev_session_id = request.resume_session_id
    elif request.resume_stage == "pm":
        enqueue_result.run.pm_session_id = request.resume_session_id
    else:
        enqueue_result.run.orchestrated_session_id = request.resume_session_id
    next_plan["trigger_context"] = trigger_context
    enqueue_result.run.plan = next_plan
    request.resumed_run_id = enqueue_result.run.run_id
    request.updated_at = _now()
    session.commit()
    session.refresh(enqueue_result.run)
    session.refresh(request)
    return enqueue_result.run
