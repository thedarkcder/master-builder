from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.parent_feature_workflow.planning import ParentFeaturePlanningWorkflow, ParentFeaturePlanningWorkflowDeps
from orchestrator.core.parent_planning_clarification_service import ClarificationPublishEffects, ParentPlanningClarificationService
from orchestrator.core.parent_planning_fanout_service import ParentPlanningFanoutService
from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    ensure_workflow_execution,
)
from orchestrator.core.workflow_type_catalog import get_workflow_type
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import WorkflowOperation, WorkflowOperationAttempt
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class _FakeBriefPlanner:
    def __init__(self, *, planning_result, planning_package: dict[str, object]) -> None:
        self.planning_result = planning_result
        self.planning_package = planning_package
        self.attempt_refs: list[object] = []

    def with_attempt(self, *, attempt_ref):  # noqa: ANN001
        self.attempt_refs.append(attempt_ref)
        return self

    def plan_backlog_parent(self, **_kwargs):  # noqa: ANN003
        return self.planning_result, self.planning_package


class _FakeChildSyncGateway:
    def with_attempt(self, *, attempt_ref):  # noqa: ANN001
        raise AssertionError(f"Child fanout should not start while backlog planning is blocked: {attempt_ref}")

    def combined_child_updates(self, *, seed_data: dict[str, object]):
        return (
            list(seed_data.get("updated_children", [])),
            list(seed_data.get("created_children", [])),
            list(seed_data.get("changed_children", [])),
        )


class _FakeIssueGateway:
    def __init__(self) -> None:
        self.label_updates: list[str] = []
        self.published_questions: list[tuple[str, tuple[object, ...]]] = []

    def update_issue_sync_label(self, *, issue_detail, target_label: str) -> None:  # noqa: ANN001
        self.label_updates.append(f"{issue_detail.key}:{target_label}")

    def active_clarification_effects(self, *, issue_key: str, questions):  # noqa: ANN001
        return None

    def publish_clarification(self, *, issue_key: str, questions):  # noqa: ANN001
        self.published_questions.append((issue_key, tuple(questions)))
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=True,
            discord_followup_created=True,
            jira_comment_id="jira-comment-1",
        )


class ParentFeaturePlanningWorkflowTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="parent-feature-planning-workflow")
        self.session_factory = create_session_factory(self.database_url)
        self._event_store_patch = patch(
            "orchestrator.core.product_events.event_store",
            return_value=type("FakeEventStore", (), {"execute": lambda _self, _sql, **_kwargs: ""})(),
        )
        self._event_notify_patch = patch(
            "orchestrator.core.product_events.publish_product_event_notification",
            lambda **_kwargs: None,
        )
        self._event_store_patch.start()
        self._event_notify_patch.start()

    def tearDown(self) -> None:
        self._event_notify_patch.stop()
        self._event_store_patch.stop()
        self._cleanup_test_database()

    def test_backlog_planning_waits_only_after_jira_projection_records_comment_id(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            lifecycle = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-241",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-241",
                        display_name="Backlog question projection",
                        description="Planning needs product input",
                    ),
                ),
                display_name="Backlog question projection",
                description="Planning needs product input",
            )
            issue_gateway = _FakeIssueGateway()
            planner = _FakeBriefPlanner(
                planning_result=SimpleNamespace(
                    planning_state="planning_needs_clarification",
                    open_behavior_questions=("What audit window should customers see?",),
                ),
                planning_package={"planning": "package"},
            )
            workflow = ParentFeaturePlanningWorkflow(
                deps=ParentFeaturePlanningWorkflowDeps(
                    issue_gateway=issue_gateway,
                    brief_planner=planner,
                    child_sync_gateway=_FakeChildSyncGateway(),
                    clarification_service=ParentPlanningClarificationService(),
                    fanout_service=ParentPlanningFanoutService(),
                    workflow_type=workflow_type,
                    project_key_for_issue_fn=lambda _issue_key: "MAB",
                    extract_changed_fields_fn=lambda **_kwargs: [],
                    extract_status_transition_fn=lambda **_kwargs: (None, None),
                    material_parent_changed_fields_fn=lambda **_kwargs: [],
                    parent_board_entry_target_status_fn=lambda **_kwargs: None,
                )
            )

            fanout = workflow._plan_and_seed_with_attempts(
                lifecycle=lifecycle,
                parent_detail=SimpleNamespace(key="MAB-241", labels=["pm-parent"]),
                product_brief={"objective": "Plan backlog"},
                project_key="MAB",
                planning_summary="Backlog planning completed.",
                fanout_summary="Child fanout completed.",
            )
            session.commit()

            operations = {
                operation.operation_type: operation
                for operation in session.query(WorkflowOperation)
                .filter(WorkflowOperation.workflow_id == lifecycle.workflow.workflow_id)
                .all()
            }
            attempts = {
                operation_type: session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
                for operation_type, operation in operations.items()
            }

            self.assertFalse(fanout.completed)
            self.assertEqual(issue_gateway.label_updates, ["MAB-241:sync-blocked"])
            self.assertEqual(issue_gateway.published_questions[0][0], "MAB-241")
            self.assertEqual(operations["jira_comment_projection"].status, "completed")
            self.assertIn("jira-comment-1", operations["jira_comment_projection"].summary or "")
            self.assertEqual(operations["backlog_planning"].status, "waiting_for_input")
            self.assertIn("What audit window should customers see?", operations["backlog_planning"].summary or "")
            self.assertEqual(operations["jira_child_fanout"].status, "pending")
            self.assertEqual(attempts["jira_comment_projection"][-1].status, "completed")
            self.assertEqual(attempts["backlog_planning"][-1].status, "waiting_for_input")
            self.assertLessEqual(
                attempts["jira_comment_projection"][-1].finished_at,
                attempts["backlog_planning"][-1].finished_at,
            )
