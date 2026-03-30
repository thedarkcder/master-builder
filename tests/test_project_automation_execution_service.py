from __future__ import annotations

import io
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from orchestrator.core.project_automation_execution_service import (
    mark_project_automation_execution_failure,
    mark_project_automation_execution_success,
    prepare_project_automation_execution,
)
from orchestrator.core.project_automation_service import (
    PROJECT_AUTOMATION_KIND_STANDUP,
    ProjectAutomationWrite,
    upsert_project_automation,
)
from orchestrator.core.voice.tts import VoiceReplyAudio
from orchestrator.storage.models import Project, ProjectAutomationExecution, Tenant

try:
    from tests.production_path_support import (
        clear_runtime_environment,
        configure_runtime_environment,
        seed_core_runtime_state,
        session_factory_for,
    )
except ModuleNotFoundError:  # pragma: no cover
    from production_path_support import (  # type: ignore[no-redef]
        clear_runtime_environment,
        configure_runtime_environment,
        seed_core_runtime_state,
        session_factory_for,
    )


class ProjectAutomationExecutionServiceTests(unittest.TestCase):
    @staticmethod
    def _wav_bytes(sample: bytes = b"\x00\x00") -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_out:
            wav_out.setnchannels(1)
            wav_out.setsampwidth(2)
            wav_out.setframerate(24000)
            wav_out.writeframes(sample)
        return output.getvalue()

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="project_automation_execution_service.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(self.session_factory)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def test_prepare_execution_builds_attachment_action(self) -> None:
        now = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        with self.session_factory() as session:
            project = session.get(Project, "example-default")
            tenant = session.get(Tenant, "example")
            assert project is not None
            assert tenant is not None
            project.discord_config = {"channel_id": "999"}
            automation = upsert_project_automation(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                payload=ProjectAutomationWrite(
                    kind=PROJECT_AUTOMATION_KIND_STANDUP,
                    enabled=True,
                    timezone="UTC",
                    days_of_week=(5,),
                    local_time="09:00",
                    fallback_lookback_hours=24,
                ),
                now=now,
            )
            execution = ProjectAutomationExecution(
                execution_id="exec-1",
                automation_id=automation.automation_id,
                scheduled_for=now,
                window_start_at=now.replace(hour=8),
                window_end_at=now,
                status="queued",
                dedupe_key="dedupe-1",
                started_at=None,
                completed_at=None,
                discord_message_id=None,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)
            session.commit()
            with (
                patch(
                    "orchestrator.core.project_automation_execution_service.safe_build_project_automation_briefing",
                    return_value=type("Briefing", (), {"transcript": "hello world", "summary": "brief summary"})(),
                ),
                patch(
                    "orchestrator.core.project_automation_execution_service.synthesize_reply_audio",
                    return_value=VoiceReplyAudio(
                        audio_bytes=self._wav_bytes(b"\x01\x00"),
                        filename="voice.wav",
                        content_type="audio/wav",
                    ),
                ),
            ):
                plan = prepare_project_automation_execution(
                    session=session,
                    settings=type("S", (), {"secrets_encryption_key": ""})(),
                    tenant=tenant,
                    project=project,
                    request_id="req-1",
                    payload_json={"execution_id": "exec-1", "automation_id": automation.automation_id},
                )
        self.assertFalse(plan.already_succeeded)
        self.assertIsNotNone(plan.action)
        assert plan.action is not None
        self.assertEqual(plan.action.channel_id, "999")
        self.assertIn("Here is today's standup as of 2026-03-28.", plan.action.content)
        self.assertIn("brief summary", plan.action.content)

    def test_prepare_execution_requires_project_channel_id(self) -> None:
        now = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        with self.session_factory() as session:
            project = session.get(Project, "example-default")
            tenant = session.get(Tenant, "example")
            assert project is not None
            assert tenant is not None
            project.discord_config = {}
            automation = upsert_project_automation(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                payload=ProjectAutomationWrite(
                    kind=PROJECT_AUTOMATION_KIND_STANDUP,
                    enabled=True,
                    timezone="UTC",
                    days_of_week=(5,),
                    local_time="09:00",
                    fallback_lookback_hours=24,
                ),
                now=now,
            )
            execution = ProjectAutomationExecution(
                execution_id="exec-no-channel",
                automation_id=automation.automation_id,
                scheduled_for=now,
                window_start_at=now.replace(hour=8),
                window_end_at=now,
                status="queued",
                dedupe_key="dedupe-no-channel",
                started_at=None,
                completed_at=None,
                discord_message_id=None,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)
            session.commit()
            with self.assertRaisesRegex(ValueError, "channel_id"):
                prepare_project_automation_execution(
                    session=session,
                    settings=type("S", (), {"secrets_encryption_key": ""})(),
                    tenant=tenant,
                    project=project,
                    request_id="req-nc",
                    payload_json={"execution_id": "exec-no-channel", "automation_id": automation.automation_id},
                )

    def test_mark_failure_is_safe_for_missing_execution(self) -> None:
        with self.session_factory() as session:
            mark_project_automation_execution_failure(
                session=session,
                execution_id="missing",
                error="boom",
            )

    def test_prepare_execution_stitches_persona_segments_with_persona_voices(self) -> None:
        now = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        with self.session_factory() as session:
            project = session.get(Project, "example-default")
            tenant = session.get(Tenant, "example")
            assert project is not None
            assert tenant is not None
            project.discord_config = {
                "channel_id": "999",
                "persona_names": {
                    "pm": "Andy",
                    "engineer": "Bill",
                    "qa": "Quinn",
                    "reviewer": "Mira",
                },
                "persona_voices": {
                    "pm": "marius",
                    "engineer": "eponine",
                    "qa": "cosette",
                    "reviewer": "azelma",
                },
            }
            automation = upsert_project_automation(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                payload=ProjectAutomationWrite(
                    kind=PROJECT_AUTOMATION_KIND_STANDUP,
                    enabled=True,
                    timezone="UTC",
                    days_of_week=(5,),
                    local_time="09:00",
                    fallback_lookback_hours=24,
                ),
                now=now,
            )
            execution = ProjectAutomationExecution(
                execution_id="exec-stitch-1",
                automation_id=automation.automation_id,
                scheduled_for=now,
                window_start_at=now.replace(hour=8),
                window_end_at=now,
                status="queued",
                dedupe_key="dedupe-stitch-1",
                started_at=None,
                completed_at=None,
                discord_message_id=None,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)
            session.commit()
            segments = (
                type("Segment", (), {"persona_id": "pm", "text": "PM update"})(),
                type("Segment", (), {"persona_id": "engineer", "text": "Dev update"})(),
                type("Segment", (), {"persona_id": "qa", "text": "QA update"})(),
                type("Segment", (), {"persona_id": "reviewer", "text": "Review update"})(),
            )
            with (
                patch(
                    "orchestrator.core.project_automation_execution_service.safe_build_project_automation_briefing",
                    return_value=type(
                        "Briefing",
                        (),
                        {"transcript": "joined", "summary": "summary", "persona_segments": segments},
                    )(),
                ),
                patch(
                    "orchestrator.core.project_automation_execution_service.synthesize_reply_audio",
                    side_effect=[
                        VoiceReplyAudio(audio_bytes=self._wav_bytes(b"\x01\x00"), filename="a.wav", content_type="audio/wav"),
                        VoiceReplyAudio(audio_bytes=self._wav_bytes(b"\x02\x00"), filename="b.wav", content_type="audio/wav"),
                        VoiceReplyAudio(audio_bytes=self._wav_bytes(b"\x03\x00"), filename="c.wav", content_type="audio/wav"),
                        VoiceReplyAudio(audio_bytes=self._wav_bytes(b"\x04\x00"), filename="d.wav", content_type="audio/wav"),
                    ],
                ) as synth_mock,
            ):
                plan = prepare_project_automation_execution(
                    session=session,
                    settings=type("S", (), {"secrets_encryption_key": ""})(),
                    tenant=tenant,
                    project=project,
                    request_id="req-stitch",
                    payload_json={"execution_id": "exec-stitch-1", "automation_id": automation.automation_id},
                )

        assert plan.action is not None
        self.assertEqual(plan.action.channel_id, "999")
        self.assertEqual(synth_mock.call_count, 4)
        self.assertEqual([call.kwargs["persona_id"] for call in synth_mock.call_args_list], ["pm", "engineer", "qa", "reviewer"])
        self.assertEqual(
            [call.kwargs["text"] for call in synth_mock.call_args_list],
            [
                "Andy from Product. PM update",
                "Bill from Engineering. Dev update",
                "Quinn from QA. QA update",
                "Mira from Review. Review update",
            ],
        )
        self.assertEqual(plan.action.content_type, "audio/wav")
        self.assertEqual(plan.action.filename, "voice_reply.wav")
        self.assertIn("Here is today's standup as of 2026-03-28.", plan.action.content)
        self.assertIn("summary", plan.action.content)

    def test_mark_success_persists_discord_message_id(self) -> None:
        now = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        with self.session_factory() as session:
            project = session.get(Project, "example-default")
            tenant = session.get(Tenant, "example")
            assert project is not None
            assert tenant is not None
            project.discord_config = {"channel_id": "999"}
            automation = upsert_project_automation(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                payload=ProjectAutomationWrite(
                    kind=PROJECT_AUTOMATION_KIND_STANDUP,
                    enabled=True,
                    timezone="UTC",
                    days_of_week=(5,),
                    local_time="09:00",
                    fallback_lookback_hours=24,
                ),
                now=now,
            )
            execution = ProjectAutomationExecution(
                execution_id="exec-success-1",
                automation_id=automation.automation_id,
                scheduled_for=now,
                window_start_at=now.replace(hour=8),
                window_end_at=now,
                status="running",
                dedupe_key="dedupe-success-1",
                started_at=now,
                completed_at=None,
                discord_message_id=None,
                last_error=None,
                created_at=now,
                updated_at=now,
            )
            session.add(execution)
            session.commit()

            mark_project_automation_execution_success(
                session=session,
                execution_id=execution.execution_id,
                window_end_at=now,
                discord_message_id="discord-msg-123",
            )

            refreshed = session.get(ProjectAutomationExecution, execution.execution_id)

        assert refreshed is not None
        self.assertEqual(refreshed.status, "succeeded")
        self.assertEqual(refreshed.discord_message_id, "discord-msg-123")
