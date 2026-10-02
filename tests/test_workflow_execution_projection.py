from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator.core.workflow.step_runner import start_workflow_step_attempt
from orchestrator.core.workflow.execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    ensure_workflow_execution,
    workflow_execution_id,
)
from orchestrator.storage.db import create_session_factory
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.api.admin.workflows.type_read_model import workflow_operation_reads
from orchestrator.storage.models import (
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
)
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class WorkflowExecutionProjectionTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(
            name_prefix="workflow-projection"
        )
        self.session_factory = create_session_factory(self.database_url)

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def test_workflow_waiting_projection_does_not_mutate_operation_state(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
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
            operation = (
                session.query(WorkflowOperation)
                .filter(WorkflowOperation.operation_type == "backlog_planning")
                .one()
            )
            self.assertEqual(projection.workflow.status, "waiting_for_input")
            self.assertEqual(operation.status, "pending")

    def test_rehydrating_waiting_workflow_does_not_mark_it_running(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            self.assertIsNotNone(workflow_type)
            assert workflow_type is not None

            execution = WorkflowExecutionReference(
                key="MAB-236",
                source=WorkflowSourceReference(
                    source_system="jira",
                    source_ref="MAB-236",
                    display_name="Waiting execution should stay waiting",
                    description="Need product input",
                ),
            )
            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=execution,
                display_name="Waiting execution should stay waiting",
                description="Need product input",
            )
            operation, attempt = projection.start_operation_attempt(
                operation_type="backlog_planning"
            )
            projection.wait_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Backlog planning is waiting for product clarification.",
            )
            session.commit()

            rehydrated = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=execution,
                display_name="Waiting execution should stay waiting",
                description="Need product input",
            )
            session.flush()

            self.assertEqual(rehydrated.workflow.status, "waiting_for_input")
            self.assertEqual(operation.status, "waiting_for_input")
            self.assertEqual(attempt.status, "waiting_for_input")

    def test_starting_real_attempt_moves_waiting_workflow_to_running(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            self.assertIsNotNone(workflow_type)
            assert workflow_type is not None

            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-237",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-237",
                        display_name="Real attempts resume workflow",
                        description="Need product input",
                    ),
                ),
                display_name="Real attempts resume workflow",
                description="Need product input",
            )
            first_operation, first_attempt = projection.start_operation_attempt(
                operation_type="backlog_planning"
            )
            projection.wait_started_operation(
                operation=first_operation,
                attempt=first_attempt,
                summary="Backlog planning is waiting for product clarification.",
            )

            projection.start_operation_attempt(operation_type="jira_comment_projection")
            session.flush()

            self.assertEqual(projection.workflow.status, "running")

    def test_started_waiting_operation_records_attempt_history(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
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
            operation, attempt = projection.start_operation_attempt(
                operation_type="backlog_planning"
            )
            projection.wait_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Need PM clarification on the rollout order.",
            )
            session.commit()

            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 1)
            self.assertEqual(operation.status, "waiting_for_input")
            self.assertEqual(attempt.status, "waiting_for_input")

    def test_complete_waiting_operation_attempt_resumes_existing_attempt(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            self.assertIsNotNone(workflow_type)
            assert workflow_type is not None

            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-243",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-243",
                        display_name="Resume waiting brief",
                        description="Need PM clarification",
                    ),
                ),
                display_name="Resume waiting brief",
                description="Need PM clarification",
            )
            operation, attempt = projection.start_operation_attempt(
                operation_type="brief_normalization"
            )
            projection.wait_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Need PM clarification.",
            )

            resumed_operation, resumed_attempt = (
                projection.complete_waiting_operation_attempt(
                    operation_type="brief_normalization",
                    summary="Parent brief normalized from product clarification.",
                )
            )

            self.assertEqual(resumed_operation.operation_id, operation.operation_id)
            self.assertEqual(resumed_attempt.attempt_id, attempt.attempt_id)
            self.assertEqual(operation.status, "completed")
            self.assertEqual(attempt.status, "completed")
            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 1)

    def test_complete_waiting_operation_attempt_is_idempotent_after_completion(
        self,
    ) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            self.assertIsNotNone(workflow_type)
            assert workflow_type is not None

            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-244",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-244",
                        display_name="Replay waiting brief",
                        description="Need PM clarification",
                    ),
                ),
                display_name="Replay waiting brief",
                description="Need PM clarification",
            )
            operation, attempt = projection.start_operation_attempt(
                operation_type="brief_normalization"
            )
            projection.wait_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Need PM clarification.",
            )
            projection.complete_waiting_operation_attempt(
                operation_type="brief_normalization",
                summary="Parent brief normalized from product clarification.",
            )

            replayed_operation, replayed_attempt = (
                projection.complete_waiting_operation_attempt(
                    operation_type="brief_normalization",
                    summary="Parent brief normalized from duplicate clarification webhook.",
                )
            )

            self.assertEqual(replayed_operation.operation_id, operation.operation_id)
            self.assertEqual(replayed_attempt.attempt_id, attempt.attempt_id)
            self.assertEqual(
                operation.summary, "Parent brief normalized from product clarification."
            )
            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 1)

    def test_waiting_operation_emits_attempt_scoped_waiting_event(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            self.assertIsNotNone(workflow_type)
            assert workflow_type is not None

            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-242",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-242",
                        display_name="Waiting event kind",
                        description="Waiting operation should emit attempt lifecycle event",
                    ),
                ),
                display_name="Waiting event kind",
                description="Waiting operation should emit attempt lifecycle event",
            )
            operation, attempt = projection.start_operation_attempt(
                operation_type="backlog_planning"
            )
            with patch(
                "orchestrator.core.workflow.operation_service.emit_workflow_operation_log"
            ) as emit_log:
                projection.wait_started_operation(
                    operation=operation,
                    attempt=attempt,
                    summary="Need product clarification.",
                )

            self.assertEqual(
                emit_log.call_args.kwargs["event_type"],
                "workflow_operation_attempt_waiting_for_input",
            )

    def test_started_completed_operation_records_attempt_history(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
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
            operation, attempt = projection.start_operation_attempt(
                operation_type="brief_normalization"
            )
            projection.complete_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Parent brief normalized from the source issue.",
            )
            session.commit()

            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 1)
            self.assertEqual(operation.status, "completed")
            self.assertEqual(attempt.status, "completed")
            self.assertEqual(
                operation.summary, "Parent brief normalized from the source issue."
            )

    def test_restarting_completed_operation_clears_terminal_timestamp(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-245",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-245",
                        display_name="Restart operation after completion",
                        description="A replay should not expose running plus finished state.",
                    ),
                ),
                display_name="Restart operation after completion",
                description="A replay should not expose running plus finished state.",
            )
            operation, first_attempt = projection.start_operation_attempt(
                operation_type="brief_normalization"
            )
            projection.complete_started_operation(
                operation=operation,
                attempt=first_attempt,
                summary="Parent brief normalized.",
            )
            self.assertEqual(operation.status, "completed")
            self.assertIsNotNone(operation.finished_at)

            restarted_operation, restarted_attempt = projection.start_operation_attempt(
                operation_type="brief_normalization"
            )

            self.assertEqual(restarted_operation.operation_id, operation.operation_id)
            self.assertEqual(restarted_operation.status, "running")
            self.assertIsNone(restarted_operation.finished_at)
            self.assertEqual(restarted_attempt.attempt_number, 2)

    def test_started_failed_operation_records_attempt_history(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
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
            operation, attempt = projection.start_operation_attempt(
                operation_type="jira_child_fanout"
            )
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

    def test_terminal_operation_transitions_survive_outer_job_rollback(self) -> None:
        cases = [
            ("complete", "brief_normalization", "completed", "completed", None),
            ("fail", "jira_child_fanout", "failed", "failed", "failed"),
            (
                "wait",
                "backlog_planning",
                "waiting_for_input",
                "waiting_for_input",
                "waiting_for_input",
            ),
        ]

        for (
            transition,
            operation_type,
            expected_operation_status,
            expected_attempt_status,
            expected_workflow_status,
        ) in cases:
            with self.subTest(transition=transition):
                issue_key = f"MAB-260-{transition}"
                with self.session_factory() as session:
                    workflow_type = get_workflow_type(
                        session, workflow_type_key="parent_planning"
                    )
                    self.assertIsNotNone(workflow_type)
                    assert workflow_type is not None

                    projection = ensure_workflow_execution(
                        session=session,
                        workflow_type=workflow_type,
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        execution=WorkflowExecutionReference(
                            key=issue_key,
                            source=WorkflowSourceReference(
                                source_system="jira",
                                source_ref=issue_key,
                                display_name=f"Rollback proof {transition}",
                                description="Terminal transition must survive the webhook job rollback path.",
                            ),
                        ),
                        display_name=f"Rollback proof {transition}",
                        description="Terminal transition must survive the webhook job rollback path.",
                    )
                    operation, attempt = projection.start_operation_attempt(
                        operation_type=operation_type
                    )
                    operation_id = operation.operation_id
                    attempt_id = attempt.attempt_id
                    workflow_id = projection.workflow.workflow_id

                    if transition == "complete":
                        projection.complete_started_operation(
                            operation=operation,
                            attempt=attempt,
                            summary="Operation completed before the outer job failed.",
                        )
                    elif transition == "fail":
                        projection.fail_started_operation(
                            operation=operation,
                            attempt=attempt,
                            category="external_failure",
                            message="Operation failed before the outer job handler rolled back.",
                        )
                    else:
                        projection.wait_started_operation(
                            operation=operation,
                            attempt=attempt,
                            summary="Operation is waiting for input before the outer job handler rolled back.",
                        )

                    session.rollback()

                with self.session_factory() as observer_session:
                    persisted_operation = observer_session.get(
                        WorkflowOperation, operation_id
                    )
                    persisted_attempt = observer_session.get(
                        WorkflowOperationAttempt, attempt_id
                    )
                    persisted_workflow = observer_session.get(
                        WorkflowExecution, workflow_id
                    )
                    self.assertIsNotNone(persisted_operation)
                    self.assertIsNotNone(persisted_attempt)
                    self.assertIsNotNone(persisted_workflow)
                    assert persisted_operation is not None
                    assert persisted_attempt is not None
                    assert persisted_workflow is not None
                    self.assertEqual(
                        persisted_operation.status, expected_operation_status
                    )
                    self.assertEqual(persisted_attempt.status, expected_attempt_status)
                    if expected_workflow_status is not None:
                        self.assertEqual(
                            persisted_workflow.status, expected_workflow_status
                        )

    def test_starting_operation_attempt_rejects_existing_running_attempt(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-232",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-232",
                        display_name="Duplicate running attempts",
                        description="Retry should not create overlapping attempts",
                    ),
                ),
                display_name="Duplicate running attempts",
                description="Retry should not create overlapping attempts",
            )
            operation, first_attempt = projection.start_operation_attempt(
                operation_type="backlog_planning"
            )

            with pytest.raises(RuntimeError, match="already has active attempt 1"):
                projection.start_operation_attempt(operation_type="backlog_planning")

            attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )
            self.assertEqual(
                [attempt.attempt_id for attempt in attempts], [first_attempt.attempt_id]
            )

    def test_starting_upstream_attempt_invalidates_waiting_downstream_operation(
        self,
    ) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-235",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-235",
                        display_name="Fanout waits after backlog",
                        description="Downstream waiting state must not survive upstream re-planning",
                    ),
                ),
                display_name="Fanout waits after backlog",
                description="Downstream waiting state must not survive upstream re-planning",
            )
            planning_operation, planning_attempt = projection.start_operation_attempt(
                operation_type="backlog_planning"
            )
            projection.complete_started_operation(
                operation=planning_operation,
                attempt=planning_attempt,
                summary="Backlog planning completed.",
            )
            fanout_operation, fanout_attempt = projection.start_operation_attempt(
                operation_type="jira_child_fanout"
            )
            projection.wait_started_operation(
                operation=fanout_operation,
                attempt=fanout_attempt,
                summary="Engineering child fanout is waiting for product clarification.",
            )

            next_planning_operation, next_planning_attempt = (
                projection.start_operation_attempt(operation_type="backlog_planning")
            )
            projection.wait_started_operation(
                operation=next_planning_operation,
                attempt=next_planning_attempt,
                summary="Backlog planning is waiting for product clarification.",
            )
            session.flush()

            self.assertEqual(next_planning_operation.status, "waiting_for_input")
            self.assertEqual(fanout_operation.status, "pending")
            self.assertEqual(
                fanout_operation.summary,
                "Create or refresh the engineering child tickets implied by the confirmed parent brief.",
            )
            self.assertIsNone(fanout_operation.started_at)
            self.assertIsNone(fanout_operation.finished_at)

    def test_step_runner_rejects_operation_not_registered_in_code_graph(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="parent_planning"
            )
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
                start_workflow_step_attempt(
                    lifecycle=projection, operation_type="legacy_catalog_only_step"
                )

            self.assertEqual(session.query(WorkflowOperationAttempt).count(), 0)
            self.assertEqual(
                session.query(WorkflowOperation)
                .filter(WorkflowOperation.operation_type == "legacy_catalog_only_step")
                .count(),
                0,
            )

    def test_workflow_execution_id_uses_generic_execution_key(self) -> None:
        self.assertEqual(
            workflow_execution_id(
                workflow_type_key="nightly_maintenance", execution_key="tenant-a:daily"
            ),
            "nightly_maintenance:tenant-a:daily",
        )

    def test_read_model_does_not_expose_retry_without_executable_handler_capability(
        self,
    ) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(
                session, workflow_type_key="issue_execution"
            )
            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-240",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-240",
                        display_name="Run execution",
                        description="Run execution failed",
                    ),
                ),
                display_name="Run execution",
                description="Run execution failed",
            )
            operation, attempt = projection.start_operation_attempt(
                operation_type="run_attempt_execution"
            )
            projection.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category="run_execution_failed",
                message="Runner failed.",
            )
            session.commit()

            _, operations = workflow_operation_reads(
                session=session,
                workflow=projection.workflow,
                operations=session.query(WorkflowOperation)
                .filter(
                    WorkflowOperation.workflow_id == projection.workflow.workflow_id
                )
                .all(),
                operation_attempts={operation.operation_id: [attempt]},
                operation_events={},
            )

            run_operation = next(
                item
                for item in operations
                if item.operation_type == "run_attempt_execution"
            )
            self.assertFalse(run_operation.can_retry)
            self.assertEqual(
                run_operation.retry_unavailable_reason,
                "Manual retry is disabled by the workflow definition.",
            )
