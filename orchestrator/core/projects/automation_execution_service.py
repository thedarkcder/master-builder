from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import wave

from sqlalchemy.orm import Session

from orchestrator.core.communications import DiscordChannelMessageWithAttachmentAction
from orchestrator.core.discord.personas import build_voice_room_spoken_reply_text
from orchestrator.core.projects.automation_briefing_service import (
    safe_build_project_automation_briefing,
)
from orchestrator.core.projects.automation_repository import (
    get_project_automation,
    get_project_automation_execution,
)
from orchestrator.core.projects.automation_service import (
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


def _automation_label(kind: str) -> str:
    normalized = str(kind or "").strip().lower()
    if normalized == "standup_voice_brief":
        return "standup"
    if normalized == "retro_voice_brief":
        return "retro"
    return normalized.replace("_", " ") or "automation"


def _automation_intro(*, kind: str, as_of: datetime) -> str:
    as_of_date = _to_utc(as_of).date().isoformat()
    return f"Here is today's {_automation_label(kind)} as of {as_of_date}."


def _to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _project_channel_id(project: Project) -> str:
    config = project.discord_config if isinstance(project.discord_config, dict) else {}
    channel_id = str(config.get("channel_id") or "").strip()
    if not channel_id:
        raise ValueError(
            "Project automation requires project.discord_config.channel_id to deliver briefings."
        )
    return channel_id


def _stitch_wav_segments(*, segments: list[bytes]) -> bytes:
    if not segments:
        raise ValueError("No audio segments to stitch")
    params = None
    frames: list[bytes] = []
    for segment in segments:
        with wave.open(io.BytesIO(segment), "rb") as source:
            source_params = source.getparams()
            if params is None:
                params = source_params
            else:
                if (
                    source_params.nchannels != params.nchannels
                    or source_params.sampwidth != params.sampwidth
                    or source_params.framerate != params.framerate
                ):
                    raise ValueError(
                        "Cannot stitch voice segments with incompatible WAV parameters"
                    )
            frames.append(source.readframes(source.getnframes()))
    assert params is not None
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(params.nchannels)
        target.setsampwidth(params.sampwidth)
        target.setframerate(params.framerate)
        for segment_frames in frames:
            target.writeframes(segment_frames)
    return output.getvalue()


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
        raise ValueError(
            "Project automation job payload missing execution_id or automation_id"
        )

    execution = get_project_automation_execution(
        session=session, execution_id=execution_id
    )
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
    delivery_channel = _project_channel_id(project)
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
    room_config = (
        project.discord_config if isinstance(project.discord_config, dict) else {}
    )
    persona_segments = tuple(getattr(briefing, "persona_segments", ()) or ())
    if not persona_segments:
        persona_segments = (
            type("Segment", (), {"persona_id": "pm", "text": briefing.transcript})(),
        )
    audio_segments: list[bytes] = []
    for segment in persona_segments:
        persona_name = str(
            room_config.get("persona_names", {}).get(
                str(segment.persona_id or "").strip().lower()
            )
            or ""
        ).strip()
        spoken_text = build_voice_room_spoken_reply_text(
            message=segment.text,
            persona_id=segment.persona_id,
            persona_name=persona_name,
        )
        audio = synthesize_reply_audio(
            settings=settings,
            text=spoken_text,
            persona_id=segment.persona_id,
            room_config=room_config,
        )
        audio_segments.append(audio.audio_bytes)
    stitched_audio = _stitch_wav_segments(segments=audio_segments)
    intro = _automation_intro(kind=automation.kind, as_of=window_end_at)
    action = DiscordChannelMessageWithAttachmentAction(
        channel_id=delivery_channel,
        content=f"{intro}\n\n{briefing.summary}",
        filename="voice_reply.wav",
        file_bytes=stitched_audio,
        content_type="audio/wav",
        fallback_content_on_failure=f"{intro}\n\n{briefing.summary}",
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
