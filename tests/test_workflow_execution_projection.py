from __future__ import annotations

from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    ensure_workflow_execution,
    workflow_execution_id,
)
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import AuditEvent, WorkflowOperation, WorkflowOperationAttempt, WorkflowType
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

            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-215",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-215",
                        display_name="Identity and authorization v1 contract",
                        description="Need PM clarification",
                    ),
                ),
                display_name="Identity and authorization v1 contract",
                description="Need PM clarification",
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

            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-215",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-215",
                        display_name="Identity and authorization v1 contract",
                        description="Need PM clarification",
                    ),
                ),
                display_name="Identity and authorization v1 contract",
                description="Need PM clarification",
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

    def test_set_operation_completed_updates_state_without_creating_attempt(self) -> None:
        with self.session_factory() as session:
            workflow_type = session.get(WorkflowType, "parent_planning")
            self.assertIsNotNone(workflow_type)
            assert workflow_type is not None

            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-230",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-230",
                        display_name="Repeated transition projection should not duplicate attempts",
                        description="Parent sync replay",
                    ),
                ),
                display_name="Repeated transition projection should not duplicate attempts",
                description="Parent sync replay",
            )
            projection.set_operation_completed(
                operation_type="brief_normalization",
                summary="Parent brief normalized from the source issue.",
            )
            session.commit()

            attempts = (
                session.query(WorkflowOperationAttempt)
                .all()
            )
            self.assertEqual(len(attempts), 0)
            operation = session.query(WorkflowOperation).filter(WorkflowOperation.operation_type == "brief_normalization").one()
            self.assertEqual(operation.status, "completed")
            self.assertEqual(operation.summary, "Parent brief normalized from the source issue.")

    def test_set_operation_waiting_for_input_updates_state_without_creating_attempt(self) -> None:
        with self.session_factory() as session:
            workflow_type = session.get(WorkflowType, "parent_planning")
            self.assertIsNotNone(workflow_type)
            assert workflow_type is not None

            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-230",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-230",
                        display_name="Repeated wait projection should not duplicate attempts",
                        description="Backlog planning blocked",
                    ),
                ),
                display_name="Repeated wait projection should not duplicate attempts",
                description="Backlog planning blocked",
            )
            projection.set_operation_waiting_for_input(
                operation_type="backlog_planning",
                summary="Need PM clarification on the rollout order.",
            )
            session.commit()

            attempts = (
                session.query(WorkflowOperationAttempt)
                .all()
            )
            self.assertEqual(len(attempts), 0)
            operation = session.query(WorkflowOperation).filter(WorkflowOperation.operation_type == "backlog_planning").one()
            self.assertEqual(operation.status, "waiting_for_input")
            self.assertEqual(operation.summary, "Need PM clarification on the rollout order.")

    def test_workflow_execution_id_uses_generic_execution_key(self) -> None:
        self.assertEqual(
            workflow_execution_id(workflow_type_key="nightly_maintenance", execution_key="tenant-a:daily"),
            "nightly_maintenance:tenant-a:daily",
        )
