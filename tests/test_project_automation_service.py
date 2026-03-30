from __future__ import annotations

from datetime import UTC, datetime
from tempfile import TemporaryDirectory
import unittest

from orchestrator.core.project_automation_service import (
    PROJECT_AUTOMATION_KIND_STANDUP,
    ProjectAutomationWrite,
    enqueue_due_project_automation_runs,
    list_project_automation_definitions,
    upsert_project_automation,
)
from orchestrator.storage.models import Project

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


class ProjectAutomationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="project_automation_service.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(self.session_factory)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def test_upsert_and_list_project_automations(self) -> None:
        now = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        with self.session_factory() as session:
            project = session.get(Project, "example-default")
            assert project is not None
            upsert_project_automation(
                session=session,
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                payload=ProjectAutomationWrite(
                    kind=PROJECT_AUTOMATION_KIND_STANDUP,
                    enabled=True,
                    timezone="Europe/London",
                    days_of_week=(0, 1, 2, 3, 4),
                    local_time="09:30",
                    delivery_text_channel_id="123",
                    voice_id="alloy",
                    fallback_lookback_hours=24,
                ),
                now=now,
            )
            rows = list_project_automation_definitions(
                session=session,
                tenant_id=project.tenant_id,
                project_id=project.project_id,
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].kind, PROJECT_AUTOMATION_KIND_STANDUP)
        self.assertEqual(rows[0].delivery_text_channel_id, "123")

    def test_upsert_allows_null_delivery_text_channel_id(self) -> None:
        now = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        with self.session_factory() as session:
            project = session.get(Project, "example-default")
            assert project is not None
            upsert_project_automation(
                session=session,
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                payload=ProjectAutomationWrite(
                    kind=PROJECT_AUTOMATION_KIND_STANDUP,
                    enabled=True,
                    timezone="Europe/London",
                    days_of_week=(0, 1, 2, 3, 4),
                    local_time="09:30",
                    delivery_text_channel_id=None,
                    voice_id=None,
                    fallback_lookback_hours=24,
                ),
                now=now,
            )
            rows = list_project_automation_definitions(
                session=session,
                tenant_id=project.tenant_id,
                project_id=project.project_id,
            )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].delivery_text_channel_id)

    def test_enqueue_due_slots_creates_execution_and_job_once(self) -> None:
        now = datetime(2026, 3, 28, 9, 0, tzinfo=UTC)
        with self.session_factory() as session:
            project = session.get(Project, "example-default")
            assert project is not None
            automation = upsert_project_automation(
                session=session,
                tenant_id=project.tenant_id,
                project_id=project.project_id,
                payload=ProjectAutomationWrite(
                    kind=PROJECT_AUTOMATION_KIND_STANDUP,
                    enabled=True,
                    timezone="UTC",
                    days_of_week=(5,),
                    local_time="09:00",
                    delivery_text_channel_id="456",
                    voice_id=None,
                    fallback_lookback_hours=24,
                ),
                now=now,
            )
            automation.next_run_at = now
            session.commit()
            first = enqueue_due_project_automation_runs(session=session, now=now)
            second = enqueue_due_project_automation_runs(session=session, now=now)
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].automation.automation_id, automation.automation_id)
        self.assertEqual(len(second), 0)
