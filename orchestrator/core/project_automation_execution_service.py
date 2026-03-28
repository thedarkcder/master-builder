from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from orchestrator.core.communications import DiscordChannelMessageWithAttachmentAction
from orchestrator.core.project_automation_briefing_service import safe_build_project_automation_briefing
from orchestrator.core.project_automation_repository import (
    get_project_automation,
    get_project_automation_execution,
)
from orchestrator.core.project_automation_service import (
    PROJECT_AUTOMATION_EXECUTION_STATUS_SUCCEEDED,
    mark_execution_failed,
    mark_execution_running,
    mark_execution_success,
)
from orchestrator.core.voice.tts import synthesize_reply_audio
from orchestrator.storage.models import Project, Tenant


@dataclass(frozen=True)
class ProjectAutomationExecutionPlan:
    execution_id: str
    action: DiscordChannelMessageWithAttachmentAction | None
    already_succeeded: bool
    window_end_at: datetime


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def prepare_project_automation_execution(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project | None,
    request_id: str,
    payload_json: dict[str, object],
) -> ProjectAutomationExecutionPlan:
    execution_id = str(payload_json.get("execution_id") or "").strip()
    automation_id = str(payload_json.get("automation_id") or "").strip()
    if not execution_id or not automation_id:
        raise ValueError("Project automation job payload missing execution_id or automation_id")

    execution = get_project_automation_execution(session=session, execution_id=execution_id)
    if execution is None:
        raise ValueError(f"Project automation execution '{execution_id}' was not found")
    if execution.status == PROJECT_AUTOMATION_EXECUTION_STATUS_SUCCEEDED:
        return ProjectAutomationExecutionPlan(
            execution_id=execution.execution_id,
            action=None,
            already_succeeded=True,
            window_end_at=_to_utc(execution.window_end_at),
        )
    automation = get_project_automation(session=session, automation_id=automation_id)
    if automation is None:
        raise ValueError(f"Project automation '{automation_id}' was not found")
    if project is None:
        raise ValueError("Project automation project context is missing")
    mark_execution_running(session=session, execution_id=execution.execution_id)
    window_start_at = _to_utc(execution.window_start_at)
    window_end_at = _to_utc(execution.window_end_at)
    if window_end_at <= window_start_at:
        window_end_at = window_start_at + timedelta(seconds=1)
    briefing = safe_build_project_automation_briefing(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        automation=automation,
        window_start_at=window_start_at,
        window_end_at=window_end_at,
    )
    audio = synthesize_reply_audio(
        settings=settings,
        text=briefing.transcript,
        room_config={
            "persona_voices": {"default": automation.voice_id or ""},
        },
    )
    action = DiscordChannelMessageWithAttachmentAction(
        channel_id=automation.delivery_text_channel_id,
        content=f"{briefing.summary}\n\nAI-generated, source-derived briefing.",
        filename=audio.filename,
        file_bytes=audio.audio_bytes,
        content_type=audio.content_type,
        fallback_content_on_failure=briefing.summary,
    )
    return ProjectAutomationExecutionPlan(
        execution_id=execution.execution_id,
        action=action,
        already_succeeded=False,
        window_end_at=window_end_at,
    )


def mark_project_automation_execution_success(
    *,
    session: Session,
    execution_id: str,
    window_end_at: datetime,
    discord_message_id: str | None = None,
) -> None:
    mark_execution_success(
        session=session,
        execution_id=execution_id,
        window_end_at=window_end_at,
        discord_message_id=discord_message_id,
    )


def mark_project_automation_execution_failure(
    *,
    session: Session,
    execution_id: str,
    error: str,
) -> None:
    mark_execution_failed(
        session=session,
        execution_id=execution_id,
        error=error,
    )
