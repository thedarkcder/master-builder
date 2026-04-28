from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator.core.workflow_step_runner import start_workflow_step_attempt
from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    ensure_workflow_execution,
    workflow_execution_id,
)
from orchestrator.storage.db import create_session_factory
from orchestrator.core.workflow_type_catalog import get_workflow_type
from orchestrator.storage.models import WorkflowOperation, WorkflowOperationAttempt
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class WorkflowExecutionProjectionTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="workflow-projection")
        self.session_factory = create_session_factory(self.database_url)
        self._event_store_patch = patch(
            "orchestrator.core.product_events.event_store",
            return_value=type("FakeEventStore", (), {"execute": lambda _self, _sql, **_kwargs: ""})(),
        )
        self._event_store_patch.start()

    def tearDown(self) -> None:
        self._event_store_patch.stop()
        self._cleanup_test_database()

    def test_workflow_waiting_projection_does_not_mutate_operation_state(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
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
            projection.mark_workflow_waiting_for_input()
            session.commit()

            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 0)
            operation = session.query(WorkflowOperation).filter(WorkflowOperation.operation_type == "backlog_planning").one()
            self.assertEqual(projection.workflow.status, "waiting_for_input")
            self.assertEqual(operation.status, "pending")

    def test_started_waiting_operation_records_attempt_history(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
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
            operation, attempt = projection.start_operation_attempt(operation_type="backlog_planning")
            projection.wait_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Need PM clarification on the rollout order.",
            )
            session.commit()

            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 1)
            self.assertEqual(operation.status, "waiting_for_input")
            self.assertEqual(attempt.status, "waiting_for_input")

    def test_started_completed_operation_records_attempt_history(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
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
            operation, attempt = projection.start_operation_attempt(operation_type="brief_normalization")
            projection.complete_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Parent brief normalized from the source issue.",
            )
            session.commit()

            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 1)
            self.assertEqual(operation.status, "completed")
            self.assertEqual(attempt.status, "completed")
            self.assertEqual(operation.summary, "Parent brief normalized from the source issue.")

    def test_started_failed_operation_records_attempt_history(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
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
            operation, attempt = projection.start_operation_attempt(operation_type="jira_child_fanout")
            projection.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category="external_failure",
                message="Jira rejected child fanout.",
            )
            session.commit()

            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 1)
            self.assertEqual(operation.status, "failed")
            self.assertEqual(attempt.status, "failed")
            self.assertEqual(operation.summary, "Jira rejected child fanout.")

    def test_step_runner_rejects_operation_not_registered_in_code_graph(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-231",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-231",
                        display_name="Unknown operation",
                        description="Unknown operation should fail hard",
                    ),
                ),
                display_name="Unknown operation",
                description="Unknown operation should fail hard",
            )

            with pytest.raises(LookupError, match="has no step"):
                start_workflow_step_attempt(lifecycle=projection, operation_type="legacy_catalog_only_step")

            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 0)
            self.assertEqual(
                session.query(WorkflowOperation).filter(WorkflowOperation.operation_type == "legacy_catalog_only_step").count(),
                0,
            )

    def test_workflow_execution_id_uses_generic_execution_key(self) -> None:
        self.assertEqual(
            workflow_execution_id(workflow_type_key="nightly_maintenance", execution_key="tenant-a:daily"),
            "nightly_maintenance:tenant-a:daily",
        )
