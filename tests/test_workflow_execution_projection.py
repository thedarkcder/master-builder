from __future__ import annotations

from orchestrator.core.workflow_execution_projection import ensure_issue_workflow_execution
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import AuditEvent, WorkflowOperationAttempt, WorkflowType
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class WorkflowExecutionProjectionTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="workflow-projection")
        self.session_factory = create_session_factory(self.database_url)

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def test_mark_waiting_for_input_uses_status_detail_not_error_message(self) -> None:
        with self.session_factory() as session:
            workflow_type = session.get(WorkflowType, "parent_planning")
            self.assertIsNotNone(workflow_type)
            assert workflow_type is not None

            projection = ensure_issue_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="MAB-215",
                issue_summary="Identity and authorization v1 contract",
                issue_description="Need PM clarification",
            )
            projection.mark_waiting_for_input(
                operation_type="backlog_planning",
                summary="Need PM clarification on the rollout order.",
            )
            session.commit()

            attempt = session.query(WorkflowOperationAttempt).one()
            self.assertEqual(attempt.status, "waiting_for_input")
            self.assertIsNone(attempt.error_message)
            self.assertEqual(attempt.status_detail, "Need PM clarification on the rollout order.")
            self.assertIsNotNone(attempt.created_at)

    def test_mark_waiting_for_input_records_audit_event(self) -> None:
        with self.session_factory() as session:
            workflow_type = session.get(WorkflowType, "parent_planning")
            self.assertIsNotNone(workflow_type)
            assert workflow_type is not None

            projection = ensure_issue_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="MAB-215",
                issue_summary="Identity and authorization v1 contract",
                issue_description="Need PM clarification",
            )
            projection.mark_waiting_for_input(
                operation_type="backlog_planning",
                summary="Need PM clarification on the rollout order.",
            )
            session.commit()

            event = (
                session.query(AuditEvent)
                .filter(AuditEvent.event_kind == "waiting_for_input")
                .one()
            )
            self.assertEqual(event.level, "info")
            self.assertEqual(event.event_kind, "waiting_for_input")
            self.assertEqual(event.message, "Need PM clarification on the rollout order.")
            self.assertEqual(event.source_component, "workflow_operation_service")
            self.assertIsNotNone(event.recorded_at)
