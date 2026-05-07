from __future__ import annotations

from types import SimpleNamespace
from orchestrator.core.parent_feature_workflow.planning import ParentFeaturePlanningWorkflow, ParentFeaturePlanningWorkflowDeps
from orchestrator.core.projects.parent_planning_clarification_service import ClarificationPublishEffects, ParentPlanningClarificationService
from orchestrator.core.projects.parent_planning_fanout_service import ParentPlanningFanoutService
from orchestrator.core.runtime.payload_models import (
    PMDecisionRequest,
    PMDecisionResolution,
    StakeholderEscalation,
)
from orchestrator.core.workflow.execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    ensure_workflow_execution,
)
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import WorkflowOperation, WorkflowOperationAttempt
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class _FakeBriefPlanner:
    def __init__(self, *, planning_results) -> None:  # noqa: ANN001
        self.planning_results = list(planning_results)
        self.attempt_refs: list[object] = []
        self.product_briefs: list[dict[str, object]] = []
        self.pm_resolutions: list[object] = []

    def with_attempt(self, *, attempt_ref):  # noqa: ANN001
        self.attempt_refs.append(attempt_ref)
        return self

    def plan_backlog_parent(self, **kwargs):  # noqa: ANN003
        self.product_briefs.append(dict(kwargs.get("product_brief") or {}))
        if not self.planning_results:
            raise AssertionError("Unexpected backlog planning call")
        return self.planning_results.pop(0)

    def resolve_pm_decisions(self, **_kwargs):  # noqa: ANN003
        if not self.pm_resolutions:
            raise AssertionError("Unexpected PM decision resolution call")
        return self.pm_resolutions.pop(0)


class _FakeChildSyncGateway:
    def with_attempt(self, *, attempt_ref):  # noqa: ANN001
        raise AssertionError(f"Child fanout should not start while backlog planning is blocked: {attempt_ref}")

    def combined_child_updates(self, *, seed_data: dict[str, object]):
        return (
            list(seed_data.get("updated_children", [])),
            list(seed_data.get("created_children", [])),
            list(seed_data.get("changed_children", [])),
        )


class _FakeCompletingChildSyncGateway:
    def __init__(self) -> None:
        self.attempt_refs: list[object] = []

    def with_attempt(self, *, attempt_ref):  # noqa: ANN001
        self.attempt_refs.append(attempt_ref)
        return self

    def seed_parent_backlog_children(self, **_kwargs):  # noqa: ANN003
        return {
            "requires_input": False,
            "updated_children": ["MAB-242"],
            "created_children": [],
            "changed_children": ["MAB-242"],
        }

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
        self.start_development_links: list[tuple[str, str]] = []

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

    def publish_start_development_link(self, *, issue_key: str, action_url: str):  # noqa: ANN001
        self.start_development_links.append((issue_key, action_url))
        return {"id": "jira-start-comment-1"}, None


class ParentFeaturePlanningWorkflowTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="parent-feature-planning-workflow")
        self.session_factory = create_session_factory(self.database_url)

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def test_backlog_planning_waits_only_after_pm_escalates_and_records_jira_comment_id(self) -> None:
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
                planning_results=[
                    (
                        SimpleNamespace(
                            planning_state="planning_needs_clarification",
                            pm_decision_requests=(
                                PMDecisionRequest(
                                    request_id="pm-audit-window",
                                    question="What audit window should customers see?",
                                    why_it_matters="The answer changes product commitments.",
                                    related_decision_ids=("audit-window",),
                                ),
                            ),
                        ),
                        {"planning": "package"},
                    )
                ],
            )
            planner.pm_resolutions.append(
                SimpleNamespace(
                    resolved_decisions=(),
                    stakeholder_escalations=(
                        StakeholderEscalation(
                            escalation_id="stakeholder-audit-window",
                            question="What audit window should customers see?",
                            why_it_matters="The answer changes product commitments.",
                            business_impact_area="risk_compliance",
                            source_pm_decision_request_ids=("pm-audit-window",),
                        ),
                    ),
                    updated_planning_context={},
                )
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
                session=session,
                settings=SimpleNamespace(
                    admin_ui_base_url="https://mb.example.test",
                    jira_action_token_secret="test-action-secret",
                ),
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                lifecycle=lifecycle,
                parent_detail=SimpleNamespace(
                    key="MAB-241",
                    summary="Backlog question projection",
                    description="Planning needs product input",
                    labels=["pm-parent"],
                ),
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
            self.assertEqual(issue_gateway.published_questions[0][1][0].question, "What audit window should customers see?")
            self.assertIn("pm_decision_resolution", operations)
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

    def test_backlog_planning_with_technical_decision_completes_without_jira_comment(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            lifecycle = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-b",
                project_id="tenant-b-default",
                execution=WorkflowExecutionReference(
                    key="MAB-242",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-242",
                        display_name="Internal PM resolution",
                        description="Planning question can be answered internally",
                    ),
                ),
                display_name="Internal PM resolution",
                description="Planning question can be answered internally",
            )
            issue_gateway = _FakeIssueGateway()
            planner = _FakeBriefPlanner(
                planning_results=[
                    (
                        SimpleNamespace(
                            planning_state="planning_completed",
                            pm_decision_requests=(),
                            technical_decisions=(
                                {
                                    "decision_id": "audit-window-storage",
                                    "selected_option_id": "platform-policy",
                                    "rationale": "Use the existing product brief audit policy.",
                                },
                            ),
                        ),
                        {"planning": "completed-package"},
                    ),
                ],
            )
            workflow = ParentFeaturePlanningWorkflow(
                deps=ParentFeaturePlanningWorkflowDeps(
                    issue_gateway=issue_gateway,
                    brief_planner=planner,
                    child_sync_gateway=_FakeCompletingChildSyncGateway(),
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
                session=session,
                settings=SimpleNamespace(
                    admin_ui_base_url="https://mb.example.test",
                    jira_action_token_secret="test-action-secret",
                ),
                tenant_id="tenant-b",
                project_id="tenant-b-default",
                lifecycle=lifecycle,
                parent_detail=SimpleNamespace(
                    key="MAB-242",
                    summary="Internal PM resolution",
                    status="Backlog",
                    description="Planning question can be answered internally",
                    labels=["pm-parent"],
                ),
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

            self.assertTrue(fanout.completed)
            self.assertEqual(issue_gateway.published_questions, [])
            self.assertEqual(len(planner.product_briefs), 1)
            self.assertEqual(operations["pm_decision_resolution"].status, "pending")
            self.assertEqual(operations["backlog_planning"].status, "completed")
            self.assertEqual(operations["jira_child_fanout"].status, "completed")
            self.assertEqual(issue_gateway.start_development_links[0][0], "MAB-242")
            self.assertIn("/tenant-b/start/", issue_gateway.start_development_links[0][1])
            self.assertIn("startDevelopmentToken=", issue_gateway.start_development_links[0][1])

    def test_pm_decision_request_is_answered_internally_then_fanout_continues_without_jira(self) -> None:
        with self.session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            lifecycle = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-c",
                project_id="tenant-c-default",
                execution=WorkflowExecutionReference(
                    key="MAB-243",
                    source=WorkflowSourceReference(
                        source_system="jira",
                        source_ref="MAB-243",
                        display_name="PM decision resolution",
                        description="Planning question should be resolved internally",
                    ),
                ),
                display_name="PM decision resolution",
                description="Planning question should be resolved internally",
            )
            issue_gateway = _FakeIssueGateway()
            planner = _FakeBriefPlanner(
                planning_results=[
                    (
                        SimpleNamespace(
                            planning_state="planning_needs_clarification",
                            pm_decision_requests=(
                                PMDecisionRequest(
                                    request_id="pm-email-verification",
                                    question="What counts as verified email for acceptance?",
                                    why_it_matters="The PM owns acceptance semantics.",
                                    related_decision_ids=("email-verification",),
                                ),
                            ),
                        ),
                        {"planning": "needs-pm"},
                    ),
                    (
                        SimpleNamespace(
                            planning_state="planning_completed",
                            pm_decision_requests=(),
                            technical_decisions=(),
                        ),
                        {"planning": "completed-package"},
                    ),
                ],
            )
            planner.pm_resolutions.append(
                SimpleNamespace(
                    resolved_decisions=(
                        PMDecisionResolution(
                            request_id="pm-email-verification",
                            answer="Use in-product email verification before security-sensitive matching.",
                            rationale="This is acceptance interpretation, not a stakeholder business decision.",
                            evidence=("The parent brief requires safe account binding.",),
                            planning_context_delta={"verified_email_policy": "in_product_verification_required"},
                        ),
                    ),
                    stakeholder_escalations=(),
                    updated_planning_context={"verified_email_policy": "in_product_verification_required"},
                )
            )
            workflow = ParentFeaturePlanningWorkflow(
                deps=ParentFeaturePlanningWorkflowDeps(
                    issue_gateway=issue_gateway,
                    brief_planner=planner,
                    child_sync_gateway=_FakeCompletingChildSyncGateway(),
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
                session=session,
                settings=SimpleNamespace(
                    admin_ui_base_url="https://mb.example.test",
                    jira_action_token_secret="test-action-secret",
                ),
                tenant_id="tenant-c",
                project_id="tenant-c-default",
                lifecycle=lifecycle,
                parent_detail=SimpleNamespace(
                    key="MAB-243",
                    summary="PM decision resolution",
                    status="Backlog",
                    description="Planning question should be resolved internally",
                    labels=["pm-parent"],
                ),
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

            self.assertTrue(fanout.completed)
            self.assertEqual(issue_gateway.published_questions, [])
            self.assertEqual(len(planner.product_briefs), 2)
            self.assertEqual(
                planner.product_briefs[1]["planning_context"]["verified_email_policy"],
                "in_product_verification_required",
            )
            self.assertEqual(operations["pm_decision_resolution"].status, "completed")
            self.assertEqual(operations["backlog_planning"].status, "completed")
            self.assertEqual(operations["jira_child_fanout"].status, "completed")
