from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.core.project_automation_repository import (
    get_execution_by_dedupe_key,
    get_project_automation,
    get_project_automation_by_kind,
    get_project_automation_execution,
    list_due_project_automations,
    list_project_automation_executions,
    list_project_automations,
)
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_PROJECT_AUTOMATION,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.storage.models import ProjectAutomation, ProjectAutomationExecution
from orchestrator.storage.run_queue_events import notify_webhook_job_enqueued

PROJECT_AUTOMATION_KIND_STANDUP = "standup_voice_brief"
PROJECT_AUTOMATION_KIND_RETRO = "retro_voice_brief"
PROJECT_AUTOMATION_KINDS = (
    PROJECT_AUTOMATION_KIND_STANDUP,
    PROJECT_AUTOMATION_KIND_RETRO,
)
PROJECT_AUTOMATION_EXECUTION_STATUS_QUEUED = "queued"
PROJECT_AUTOMATION_EXECUTION_STATUS_RUNNING = "running"
PROJECT_AUTOMATION_EXECUTION_STATUS_SUCCEEDED = "succeeded"
PROJECT_AUTOMATION_EXECUTION_STATUS_FAILED = "failed"

_WEEKDAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


@dataclass(frozen=True)
class ProjectAutomationWrite:
    kind: str
    enabled: bool
    timezone: str
    days_of_week: tuple[int, ...]
    local_time: str
    delivery_text_channel_id: str
    voice_id: str | None
    fallback_lookback_hours: int


@dataclass(frozen=True)
class ScheduledProjectAutomation:
    automation: ProjectAutomation
    execution: ProjectAutomationExecution
    job_id: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _normalize_timezone(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("timezone is required")
    try:
        ZoneInfo(normalized)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Invalid timezone '{normalized}'") from exc
    return normalized


def _normalize_day(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError("days_of_week must not include boolean values")
    if isinstance(value, int):
        day = value
    else:
        normalized = str(value or "").strip().lower()
        if not normalized:
            raise ValueError("days_of_week contains empty value")
        if normalized.isdigit():
            day = int(normalized)
        else:
            if normalized not in _WEEKDAY_ALIASES:
                raise ValueError(f"Invalid day of week '{value}'")
            day = _WEEKDAY_ALIASES[normalized]
    if day < 0 or day > 6:
        raise ValueError(f"Invalid day of week '{value}'")
    return day


def _normalize_days(values: list[int | str]) -> tuple[int, ...]:
    normalized = sorted({_normalize_day(value) for value in values})
    if not normalized:
        raise ValueError("days_of_week is required")
    return tuple(normalized)


def _normalize_local_time(value: str) -> str:
    normalized = str(value or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            parsed = datetime.strptime(normalized, fmt).time()
            return parsed.strftime("%H:%M")
        except ValueError:
            continue
    raise ValueError(f"Invalid local_time '{value}'")


def _parse_local_time(value: str) -> time:
    return datetime.strptime(_normalize_local_time(value), "%H:%M").time()


def _normalize_kind(value: str) -> str:
    normalized = str(value or "").strip()
    if normalized not in PROJECT_AUTOMATION_KINDS:
        raise ValueError(f"Unsupported automation kind '{value}'")
    return normalized


def _normalize_write(payload: ProjectAutomationWrite) -> ProjectAutomationWrite:
    kind = _normalize_kind(payload.kind)
    timezone_name = _normalize_timezone(payload.timezone)
    days = _normalize_days(list(payload.days_of_week))
    local_time = _normalize_local_time(payload.local_time)
    channel_id = str(payload.delivery_text_channel_id or "").strip()
    if not channel_id:
        raise ValueError("delivery_text_channel_id is required")
    voice_id = str(payload.voice_id or "").strip() or None
    fallback = int(payload.fallback_lookback_hours)
    if fallback <= 0:
        raise ValueError("fallback_lookback_hours must be > 0")
    return ProjectAutomationWrite(
        kind=kind,
        enabled=bool(payload.enabled),
        timezone=timezone_name,
        days_of_week=days,
        local_time=local_time,
        delivery_text_channel_id=channel_id,
        voice_id=voice_id,
        fallback_lookback_hours=fallback,
    )


def compute_next_run_at(
    *,
    timezone_name: str,
    days_of_week: tuple[int, ...],
    local_time: str,
    after: datetime,
) -> datetime:
    tz = ZoneInfo(timezone_name)
    local_after = after.astimezone(tz)
    at_time = _parse_local_time(local_time)
    for offset in range(0, 15):
        day = local_after.date() + timedelta(days=offset)
        if day.weekday() not in days_of_week:
            continue
        candidate_local = datetime.combine(day, at_time, tzinfo=tz)
        if candidate_local <= local_after:
            continue
        return candidate_local.astimezone(UTC)
    raise ValueError("Unable to compute next run slot from schedule")


def upsert_project_automation(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    payload: ProjectAutomationWrite,
    now: datetime | None = None,
) -> ProjectAutomation:
    timestamp = now or _utc_now()
    normalized = _normalize_write(payload)
    existing = get_project_automation_by_kind(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        kind=normalized.kind,
    )
    if existing is None:
        existing = ProjectAutomation(
            automation_id=uuid4().hex,
            tenant_id=tenant_id,
            project_id=project_id,
            kind=normalized.kind,
            enabled=normalized.enabled,
            timezone=normalized.timezone,
            days_of_week=list(normalized.days_of_week),
            local_time=normalized.local_time,
            delivery_text_channel_id=normalized.delivery_text_channel_id,
            voice_id=normalized.voice_id,
            fallback_lookback_hours=normalized.fallback_lookback_hours,
            last_successful_window_end_at=None,
            next_run_at=compute_next_run_at(
                timezone_name=normalized.timezone,
                days_of_week=normalized.days_of_week,
                local_time=normalized.local_time,
                after=timestamp - timedelta(seconds=1),
            ),
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(existing)
    else:
        existing.enabled = normalized.enabled
        existing.timezone = normalized.timezone
        existing.days_of_week = list(normalized.days_of_week)
        existing.local_time = normalized.local_time
        existing.delivery_text_channel_id = normalized.delivery_text_channel_id
        existing.voice_id = normalized.voice_id
        existing.fallback_lookback_hours = normalized.fallback_lookback_hours
        if existing.next_run_at <= timestamp:
            existing.next_run_at = compute_next_run_at(
                timezone_name=normalized.timezone,
                days_of_week=normalized.days_of_week,
                local_time=normalized.local_time,
                after=timestamp - timedelta(seconds=1),
            )
        existing.updated_at = timestamp
    session.flush()
    session.commit()
    session.refresh(existing)
    return existing


def list_project_automation_definitions(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
) -> tuple[ProjectAutomation, ...]:
    return list_project_automations(session=session, tenant_id=tenant_id, project_id=project_id)


def enqueue_due_project_automation_runs(
    *,
    session: Session,
    now: datetime | None = None,
) -> tuple[ScheduledProjectAutomation, ...]:
    timestamp = now or _utc_now()
    due = list_due_project_automations(session=session, now=timestamp)
    scheduled: list[ScheduledProjectAutomation] = []
    for automation in due:
        scheduled_for = automation.next_run_at
        dedupe_key = f"{automation.automation_id}:{scheduled_for.isoformat()}"
        existing = get_execution_by_dedupe_key(session=session, dedupe_key=dedupe_key)
        if existing is not None:
            automation.next_run_at = compute_next_run_at(
                timezone_name=automation.timezone,
                days_of_week=tuple(int(day) for day in (automation.days_of_week or [])),
                local_time=automation.local_time,
                after=scheduled_for,
            )
            automation.updated_at = timestamp
            continue
        execution = ProjectAutomationExecution(
            execution_id=uuid4().hex,
            automation_id=automation.automation_id,
            scheduled_for=scheduled_for,
            window_start_at=automation.last_successful_window_end_at or (
                scheduled_for - timedelta(hours=max(1, int(automation.fallback_lookback_hours or 24)))
            ),
            window_end_at=scheduled_for,
            status=PROJECT_AUTOMATION_EXECUTION_STATUS_QUEUED,
            dedupe_key=dedupe_key,
            started_at=None,
            completed_at=None,
            discord_message_id=None,
            last_error=None,
            created_at=timestamp,
            updated_at=timestamp,
        )
        session.add(execution)
        session.flush()
        request_id = uuid4().hex
        enqueue_result = enqueue_webhook_job(
            session,
            request=WebhookJobEnqueueRequest(
                transport=WEBHOOK_TRANSPORT_PROJECT_AUTOMATION,
                request_id=request_id,
                tenant_id=automation.tenant_id,
                project_id=automation.project_id,
                subject_key=f"project_automation:{automation.tenant_id}:{automation.project_id}:{automation.kind}",
                dedupe_key=dedupe_key,
                event_type=automation.kind,
                payload_json={
                    "execution_id": execution.execution_id,
                    "automation_id": automation.automation_id,
                },
                context_json={},
            ),
            now=timestamp,
        )
        job = enqueue_result.job
        notify_webhook_job_enqueued(
            session=session,
            transport=job.transport,
            tenant_id=job.tenant_id,
            project_id=job.project_id,
            subject_key=job.subject_key,
            job_id=job.job_id,
            dedupe_key=job.dedupe_key,
        )
        automation.next_run_at = compute_next_run_at(
            timezone_name=automation.timezone,
            days_of_week=tuple(int(day) for day in (automation.days_of_week or [])),
            local_time=automation.local_time,
            after=scheduled_for,
        )
        automation.updated_at = timestamp
        scheduled.append(
            ScheduledProjectAutomation(
                automation=automation,
                execution=execution,
                job_id=job.job_id,
            )
        )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return ()
    return tuple(scheduled)


def mark_execution_running(
    *,
    session: Session,
    execution_id: str,
    now: datetime | None = None,
) -> ProjectAutomationExecution | None:
    execution = get_project_automation_execution(session=session, execution_id=execution_id)
    if execution is None:
        return None
    if execution.status not in {PROJECT_AUTOMATION_EXECUTION_STATUS_QUEUED, PROJECT_AUTOMATION_EXECUTION_STATUS_RUNNING}:
        return execution
    timestamp = now or _utc_now()
    execution.status = PROJECT_AUTOMATION_EXECUTION_STATUS_RUNNING
    execution.started_at = execution.started_at or timestamp
    execution.updated_at = timestamp
    session.flush()
    session.commit()
    session.refresh(execution)
    return execution


def mark_execution_success(
    *,
    session: Session,
    execution_id: str,
    window_end_at: datetime,
    discord_message_id: str | None = None,
    now: datetime | None = None,
) -> ProjectAutomationExecution | None:
    execution = get_project_automation_execution(session=session, execution_id=execution_id)
    if execution is None:
        return None
    timestamp = now or _utc_now()
    execution.status = PROJECT_AUTOMATION_EXECUTION_STATUS_SUCCEEDED
    execution.completed_at = timestamp
    execution.discord_message_id = str(discord_message_id or "").strip() or None
    execution.last_error = None
    execution.window_end_at = window_end_at
    execution.updated_at = timestamp
    automation = get_project_automation(session=session, automation_id=execution.automation_id)
    if automation is not None:
        automation.last_successful_window_end_at = window_end_at
        automation.updated_at = timestamp
    session.flush()
    session.commit()
    session.refresh(execution)
    return execution


def mark_execution_failed(
    *,
    session: Session,
    execution_id: str,
    error: str,
    now: datetime | None = None,
) -> ProjectAutomationExecution | None:
    execution = get_project_automation_execution(session=session, execution_id=execution_id)
    if execution is None:
        return None
    timestamp = now or _utc_now()
    execution.status = PROJECT_AUTOMATION_EXECUTION_STATUS_FAILED
    execution.completed_at = timestamp
    execution.last_error = str(error or "").strip()[:4000]
    execution.updated_at = timestamp
    session.flush()
    session.commit()
    session.refresh(execution)
    return execution


def list_execution_history(
    *,
    session: Session,
    automation_id: str,
    limit: int = 20,
) -> tuple[ProjectAutomationExecution, ...]:
    return list_project_automation_executions(session=session, automation_id=automation_id, limit=limit)
