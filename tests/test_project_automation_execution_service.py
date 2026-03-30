from __future__ import annotations

from datetime import UTC, datetime
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

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
                    delivery_text_channel_id="999",
                    voice_id=None,
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
                    return_value=VoiceReplyAudio(audio_bytes=b"wav", filename="voice.wav", content_type="audio/wav"),
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

    def test_prepare_execution_requires_delivery_text_channel_id(self) -> None:
        now = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        with self.session_factory() as session:
            project = session.get(Project, "example-default")
            tenant = session.get(Tenant, "example")
            assert project is not None
            assert tenant is not None
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
                    delivery_text_channel_id=None,
                    voice_id=None,
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
            with self.assertRaisesRegex(ValueError, "delivery_text_channel_id"):
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

    def test_mark_success_persists_discord_message_id(self) -> None:
        now = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        with self.session_factory() as session:
            project = session.get(Project, "example-default")
            tenant = session.get(Tenant, "example")
            assert project is not None
            assert tenant is not None
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
                    delivery_text_channel_id="999",
                    voice_id=None,
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
