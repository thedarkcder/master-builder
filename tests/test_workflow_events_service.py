from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.api.admin.workflows.events_service import list_workflow_telemetry_events
from orchestrator.core.observability.events import ProductEvent
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class WorkflowEventsServiceTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="workflow-events-service")

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def test_workflow_telemetry_uses_product_event_store_cursor(self) -> None:
        now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-777",
                execution_id="wfexec-mab-777",
                workflow_type_key="parent_planning",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                source_system="jira",
                source_ref="MAB-777",
                display_name="Telemetry pagination",
                source_description="Pagination contract",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="temporal",
                dedupe_scope="parent_planning",
                status="running",
                last_error=None,
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            operation = WorkflowOperation(
                operation_id="operation-telemetry-page",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="jira_child_fanout",
                idempotency_key="jira-child-fanout:MAB-777",
                status="running",
                target_system="jira",
                target_ref="MAB-777",
                summary="Fanout",
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            session.add_all([workflow, operation])
            session.commit()

            events = [
                ProductEvent(
                    event_sequence=5,
                    event_id="event-5",
                    event_class="execution_log",
                    tenant_id="tenant-a",
                    project_id="tenant-a-default",
                    workflow_id=workflow.workflow_id,
                    run_id=None,
                    operation_id=operation.operation_id,
                    attempt_id="attempt-page",
                    issue_key="MAB-777",
                    event_kind="runtime_log",
                    level="info",
                    source_component="logging_pane",
                    message="line-5",
                    payload_json={"attempt": 1, "stream": "stdout"},
                    recorded_at=now,
                )
            ]

            import orchestrator.api.admin.workflows.events_service as service

            original = service.list_workflow_observability_events
            service.list_workflow_observability_events = lambda **kwargs: events
            try:
                page = list_workflow_telemetry_events(
                    session=session,
                    execution_id=workflow.execution_id,
                    operation_id=operation.operation_id,
                    limit=2,
                    before_recorded_at=now,
                    before_event_id="telemetry:4",
                )
            finally:
                service.list_workflow_observability_events = original

        assert [event.message for event in page] == ["line-5"]
        assert page[0].event_id == "telemetry:5"
