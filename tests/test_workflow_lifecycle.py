from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from sqlalchemy import select

from orchestrator.core.workflow_execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow_advance import (
    WorkflowAdvanceLifecycle,
    WorkflowAdvanceOutcome,
    WorkflowAdvanceRequest,
    execute_workflow_advance,
)
from orchestrator.core.workflow_type_catalog import list_workflow_type_operations
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowType
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
            lifecycle.mark_waiting_for_input(
                operation_type="backlog_planning",
                summary="Need product clarification.",
            )
            return WorkflowAdvanceOutcome(handled=True, reason="waiting")
        if self.case == "failed":
            lifecycle.mark_operation_failed(
                operation_type="jira_child_fanout",
                category="external_failure",
                message="Jira rejected child fanout.",
            )
            return WorkflowAdvanceOutcome(handled=True, reason="failed")
        if self.case == "completed":
            for definition in list_workflow_type_operations(session, workflow_type_key=workflow_type.workflow_type_key):
                if definition.required:
                    lifecycle.set_operation_completed(
                        operation_type=definition.operation_type,
                        summary=f"{definition.operation_type} completed.",
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
            workflow_type = session.get(WorkflowType, "parent_planning")
            assert workflow_type is not None

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
                workflow_type = session.get(WorkflowType, "parent_planning")
                assert workflow_type is not None

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
