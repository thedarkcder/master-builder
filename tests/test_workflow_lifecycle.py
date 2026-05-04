from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import select

from orchestrator.core.workflow.execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow.advance import (
    DurableWorkflowLifecycle,
    WorkflowAdvanceLifecycle,
    WorkflowAdvanceOutcome,
    WorkflowAdvanceRequest,
    execute_workflow_advance,
)
from orchestrator.api.admin.workflows.type_read_model import workflow_operation_reads
from orchestrator.api.admin.workflows.operation_stale_recovery_service import recover_stale_workflow_operation_attempts
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt
from tests.test_support.db_harness import SqliteTemplateDbTestCase


@dataclass(frozen=True)
class _LifecycleCaseHandler:
    case: str

    def advance(
        self,
        *,
        session,
        settings,
        workflow_type,
        request: WorkflowAdvanceRequest,
        lifecycle: WorkflowAdvanceLifecycle,
    ) -> WorkflowAdvanceOutcome:
        _ = session, settings
        if self.case == "unhandled":
            return WorkflowAdvanceOutcome(handled=False)
        lifecycle.ensure_execution(
            display_name="Lifecycle matrix",
            description="Exercise durable lifecycle ownership",
        )
        if self.case == "waiting":
            operation, attempt = lifecycle.start_operation_attempt(operation_type="backlog_planning")
            lifecycle.wait_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Need product clarification.",
            )
            return WorkflowAdvanceOutcome(handled=True, reason="waiting")
        if self.case == "failed":
            operation, attempt = lifecycle.start_operation_attempt(operation_type="jira_child_fanout")
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category="external_failure",
                message="Jira rejected child fanout.",
            )
            return WorkflowAdvanceOutcome(handled=True, reason="failed")
        if self.case == "completed":
            for definition in workflow_type.steps:
                if definition.required:
                    operation, attempt = lifecycle.start_operation_attempt(operation_type=definition.key)
                    lifecycle.complete_started_operation(
                        operation=operation,
                        attempt=attempt,
                        summary=f"{definition.key} completed.",
                    )
            lifecycle.mark_completed_if_ready()
            return WorkflowAdvanceOutcome(handled=True, reason="completed")
        raise AssertionError(f"Unhandled lifecycle test case: {self.case}")


class WorkflowLifecycleTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="workflow-lifecycle")
        self.session_factory = create_session_factory(self.database_url)

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def _request(self, *, issue_key: str) -> WorkflowAdvanceRequest:
        return WorkflowAdvanceRequest(
            workflow_handler_key="jira_parent_feature",
            tenant_id="tenant-a",
            tenant=SimpleNamespace(tenant_id="tenant-a"),
            project_id="tenant-a-default",
            execution=WorkflowExecutionReference(
                key=issue_key,
                source=WorkflowSourceReference(
                    source_system="jira",
                    source_ref=issue_key,
                    attributes={"jira_issue_labels": ["pm-parent"]},
                ),
            ),
        )

    def test_unhandled_workflow_does_not_create_durable_lifecycle(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")

            result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(issue_key="MAB-300"),
                resolve_advance_handler_fn=lambda _key: _LifecycleCaseHandler(case="unhandled"),
            )

            assert result.handled is False
            assert session.execute(select(WorkflowExecution)).scalars().all() == []

    def test_lifecycle_state_matrix(self) -> None:
        cases = {
            "waiting": ("waiting_for_input", "backlog_planning", "waiting_for_input"),
            "failed": ("failed", "jira_child_fanout", "failed"),
            "completed": ("completed", "jira_child_fanout", "completed"),
        }
        for index, (case, expected) in enumerate(cases.items(), start=1):
            with self.session_factory() as session:
                workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")

                result = execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=self._request(issue_key=f"MAB-30{index}"),
                    resolve_advance_handler_fn=lambda _key, selected=case: _LifecycleCaseHandler(case=selected),
                )
                session.commit()

                workflow = session.execute(
                    select(WorkflowExecution).where(
                        WorkflowExecution.source_system == "jira",
                        WorkflowExecution.source_ref == f"MAB-30{index}",
                    )
                ).scalar_one()
                operation = session.execute(
                    select(WorkflowOperation).where(
                        WorkflowOperation.workflow_id == workflow.workflow_id,
                        WorkflowOperation.operation_type == expected[1],
                    )
                ).scalar_one()

                assert result.handled is True
                assert workflow.status == expected[0]
                assert operation.status == expected[2]

    def test_started_attempt_is_durable_before_external_work_runs(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            request = self._request(issue_key="MAB-399")
            lifecycle = DurableWorkflowLifecycle(
                session=session,
                workflow_type=workflow_type,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                execution=request.execution,
            )
            lifecycle.ensure_execution(
                display_name="Attempt durability",
                description="Runtime telemetry validates attempts from a separate writer session.",
            )

            operation, attempt = lifecycle.start_operation_attempt(operation_type="brief_normalization")

            with self.session_factory() as observer:
                persisted_attempt = observer.get(WorkflowOperationAttempt, attempt.attempt_id)
                assert persisted_attempt is not None
                assert persisted_attempt.operation_id == operation.operation_id
                assert persisted_attempt.status == "running"

    def test_completed_attempt_transition_survives_later_session_rollback(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            request = self._request(issue_key="MAB-398")
            lifecycle = DurableWorkflowLifecycle(
                session=session,
                workflow_type=workflow_type,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                execution=request.execution,
            )
            lifecycle.ensure_execution(
                display_name="Attempt completion durability",
                description="Completed work remains completed even if a later step rolls back.",
            )
            operation, attempt = lifecycle.start_operation_attempt(operation_type="brief_normalization")
            lifecycle.complete_started_operation(
                operation=operation,
                attempt=attempt,
                summary="Parent brief normalized.",
            )

            session.rollback()

            with self.session_factory() as observer:
                persisted_attempt = observer.get(WorkflowOperationAttempt, attempt.attempt_id)
                assert persisted_attempt is not None
                assert persisted_attempt.operation_id == operation.operation_id
                assert persisted_attempt.status == "completed"

    def test_failure_category_does_not_control_manual_retryability(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            request = self._request(issue_key="MAB-397")
            lifecycle = DurableWorkflowLifecycle(
                session=session,
                workflow_type=workflow_type,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                execution=request.execution,
            )
            lifecycle.ensure_execution(
                display_name="Failure category is diagnostic only",
                description="Workflow retry policy owns manual retryability.",
            )
            operation, attempt = lifecycle.start_operation_attempt(operation_type="jira_child_fanout")
            lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category="contract_violation",
                message="Jira rejected child fanout hierarchy.",
            )

            session.refresh(operation)
            session.refresh(attempt)
            workflow_type_read, operation_reads = workflow_operation_reads(
                session=session,
                workflow=lifecycle._ensure_projection().workflow,
                operations=[operation],
                operation_attempts={operation.operation_id: [attempt]},
                operation_events={},
            )

            fanout_read = next(item for item in operation_reads if item.operation_type == "jira_child_fanout")
            assert workflow_type_read.key == "parent_planning"
            assert attempt.retryable is True
            assert fanout_read.can_retry is True
            assert fanout_read.retry_unavailable_reason is None

    def test_stale_running_attempt_exposes_restart_not_retry(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            request = self._request(issue_key="MAB-398")
            lifecycle = DurableWorkflowLifecycle(
                session=session,
                workflow_type=workflow_type,
                tenant_id=request.tenant_id,
                project_id=request.project_id,
                execution=request.execution,
            )
            lifecycle.ensure_execution(
                display_name="Stale fanout restart",
                description="Running operation restart is separate from failed retry.",
            )
            operation, attempt = lifecycle.start_operation_attempt(operation_type="jira_child_fanout")
            attempt.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=20)
            attempt.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=15)
            session.flush()

            _, operation_reads = workflow_operation_reads(
                session=session,
                workflow=lifecycle._ensure_projection().workflow,
                operations=[operation],
                operation_attempts={operation.operation_id: [attempt]},
                operation_events={},
            )

            fanout_read = next(item for item in operation_reads if item.operation_type == "jira_child_fanout")
            assert fanout_read.can_retry is False
            assert fanout_read.can_restart is True
            assert fanout_read.restart_unavailable_reason is None

    def test_stale_recovery_calls_restart_use_case_and_ignores_waiting_attempts(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            running_request = self._request(issue_key="MAB-399")
            running_lifecycle = DurableWorkflowLifecycle(
                session=session,
                workflow_type=workflow_type,
                tenant_id=running_request.tenant_id,
                project_id=running_request.project_id,
                execution=running_request.execution,
            )
            running_lifecycle.ensure_execution(display_name="Running stale", description="Restart this attempt.")
            running_operation, running_attempt = running_lifecycle.start_operation_attempt(operation_type="jira_child_fanout")
            running_operation_id = running_operation.operation_id
            running_attempt.last_heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=20)
            running_attempt.lease_expires_at = datetime.now(timezone.utc) - timedelta(minutes=15)

            waiting_request = self._request(issue_key="MAB-400")
            waiting_lifecycle = DurableWorkflowLifecycle(
                session=session,
                workflow_type=workflow_type,
                tenant_id=waiting_request.tenant_id,
                project_id=waiting_request.project_id,
                execution=waiting_request.execution,
            )
            waiting_lifecycle.ensure_execution(display_name="Waiting stale", description="Do not restart waiting attempts.")
            waiting_operation, waiting_attempt = waiting_lifecycle.start_operation_attempt(operation_type="jira_child_fanout")
            waiting_lifecycle.wait_started_operation(
                operation=waiting_operation,
                attempt=waiting_attempt,
                summary="Waiting for stakeholder input.",
            )
            session.commit()

        restarted_operation_ids: list[str] = []

        def _restart_use_case(**kwargs):  # noqa: ANN001
            restarted_operation_ids.append(kwargs["operation_id"])
            return SimpleNamespace()

        recovered = recover_stale_workflow_operation_attempts(
            session_factory=self.session_factory,
            stale_timeout_seconds=300,
            actor="test-sweeper",
            restart_workflow_operation_fn=_restart_use_case,
        )

        assert recovered == 1
        assert restarted_operation_ids == [running_operation_id]
