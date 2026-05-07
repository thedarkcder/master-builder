from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.core.clarification.projection_service import ClarificationProjectionSpec, upsert_clarification_projection
from orchestrator.core.clarification.questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.config import Settings
from orchestrator.core.pm.followup_context_service import FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION
from orchestrator.core.parent_feature_workflow.retry_support import (
    ParentWorkflowPlanningClarificationPublisher,
)
from orchestrator.core.planning.specialist import PLANNING_STATE_COMPLETED, RetryableSpecialistPlanningContractError
from orchestrator.core.runtime.payload_models import PMDecisionRequest, StakeholderEscalation
from orchestrator.core.workflow.handler_composition import build_installed_workflow_handler_registry
from orchestrator.core.workflow.operation_service import WorkflowOperationAttemptAlreadyRunningError
from orchestrator.core.workflow.operation_retry_use_case import retry_workflow_operation_with_registered_handler
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import FollowupContext, Project, Tenant, WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class ParentWorkflowOperationRetryTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="parent-workflow-operation-retry")

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def _resolver(self, *, fake_router: object):
        return build_installed_workflow_handler_registry(
            integration_router=fake_router,
            extract_changed_fields_fn=lambda *_args, **_kwargs: [],
            extract_status_transition_fn=lambda *_args, **_kwargs: (None, None),
            build_runtime_for_selector_fn=lambda *_args, **_kwargs: object(),
            seed_issues_with_runtime_fn=lambda *_args, **_kwargs: ("seeded", {}),
            post_jira_comment_fn=lambda *_args, **_kwargs: (True, None),
            create_jira_comment_fn=lambda *_args, **_kwargs: ({"id": "comment-123"}, None),
        )

    def test_backlog_planning_retry_uses_registered_workflow_step_contract(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        settings = Settings(database_url=self.database_url)

        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-backlog",
                name="Tenant Backlog",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-backlog",
                tenant_id="tenant-backlog",
                name="Project Backlog",
                github_repository="example/project-backlog",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config=None,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-233",
                execution_id="wfexec-mab-233",
                workflow_type_key="parent_planning",
                tenant_id="tenant-backlog",
                project_id="project-backlog",
                source_system="jira",
                source_ref="MAB-233",
                display_name="Backlog planning retry",
                source_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="legacy",
                dedupe_scope="parent_planning",
                status="failed",
                last_error="test_planning payload has invalid findings item",
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            backlog_operation = WorkflowOperation(
                operation_id="operation-backlog-planning",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="backlog_planning",
                idempotency_key="workflow-definition:backlog_planning",
                status="failed",
                target_system="jira",
                target_ref="MAB-233",
                summary="test_planning payload has invalid findings item",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project, workflow, backlog_operation])
            session.commit()

            fake_jira_adapter = SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    key="MAB-233",
                    summary="Backlog planning retry",
                    description="Parent planning",
                    labels=["pm-parent"],
                    status="To Do",
                ),
            )
            fake_router = SimpleNamespace(jira=lambda **_kwargs: fake_jira_adapter)
            planner_result = SimpleNamespace(planning_state=PLANNING_STATE_COMPLETED, pm_decision_requests=())

            with (
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_handlers.backlog_planning.resolve_parent_feature_brief",
                    return_value=SimpleNamespace(to_payload=lambda: {"objective": "Retry backlog planning"}),
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                    return_value=(planner_result, {"planning": "package"}),
                ) as planner_mock,
            ):
                handle = retry_workflow_operation_with_registered_handler(
                    session=session,
                    settings=settings,
                    session_factory=session_factory,
                    workflow=workflow,
                    operation=backlog_operation,
                    handler_registry=self._resolver(fake_router=fake_router),
                )
                session.commit()

            session.refresh(backlog_operation)
            attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == backlog_operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )

            fanout_operation = (
                session.query(WorkflowOperation)
                .filter(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "jira_child_fanout",
                )
                .one()
            )
            fanout_attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == fanout_operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )

            assert handle.operation_type == "jira_child_fanout"
            assert handle.status == "completed"
            assert backlog_operation.status == "completed"
            assert backlog_operation.summary == "Backlog planning completed from the confirmed parent brief."
            assert fanout_operation.status == "completed"
            assert fanout_operation.summary == "Engineering child fanout completed from the confirmed parent brief."
            assert len(attempts) == 1
            assert attempts[0].status == "completed"
            assert len(fanout_attempts) == 1
            assert fanout_attempts[0].status == "completed"
            planner_mock.assert_called_once()

    def test_jira_child_fanout_retry_starts_selected_operation_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        settings = Settings(database_url=self.database_url)

        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-fanout-retry",
                name="Tenant Fanout Retry",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-fanout-retry",
                tenant_id="tenant-fanout-retry",
                name="Project Fanout Retry",
                github_repository="example/project-fanout-retry",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config=None,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-245",
                execution_id="wfexec-mab-245",
                workflow_type_key="parent_planning",
                tenant_id="tenant-fanout-retry",
                project_id="project-fanout-retry",
                source_system="jira",
                source_ref="MAB-245",
                display_name="Fanout retry",
                source_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="legacy",
                dedupe_scope="parent_planning",
                status="failed",
                last_error="Fanout failed",
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            backlog_operation = WorkflowOperation(
                operation_id="operation-backlog-completed",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="backlog_planning",
                idempotency_key="workflow-definition:backlog_planning",
                status="completed",
                target_system="jira",
                target_ref="MAB-245",
                summary="Backlog planning completed.",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            fanout_operation = WorkflowOperation(
                operation_id="operation-fanout-failed",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="jira_child_fanout",
                idempotency_key="workflow-definition:jira_child_fanout",
                status="failed",
                target_system="jira",
                target_ref="MAB-245",
                summary="Fanout failed.",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project, workflow, backlog_operation, fanout_operation])
            session.add(
                WorkflowOperationAttempt(
                    attempt_id="attempt-backlog-1",
                    operation_id=backlog_operation.operation_id,
                    attempt_number=1,
                    status="completed",
                    retryable=False,
                    next_retry_at=None,
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                )
            )
            session.add(
                WorkflowOperationAttempt(
                    attempt_id="attempt-fanout-1",
                    operation_id=fanout_operation.operation_id,
                    attempt_number=1,
                    status="failed",
                    error_category="external_failure",
                    error_message="Fanout failed.",
                    retryable=True,
                    next_retry_at=None,
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                )
            )
            session.commit()

            fake_jira_adapter = SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    key="MAB-245",
                    summary="Fanout retry",
                    description="Parent planning",
                    labels=["pm-parent"],
                    status="To Do",
                ),
            )
            fake_router = SimpleNamespace(jira=lambda **_kwargs: fake_jira_adapter)
            planner_result = SimpleNamespace(planning_state=PLANNING_STATE_COMPLETED, pm_decision_requests=())

            with (
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_handlers.jira_child_fanout.resolve_parent_feature_brief",
                    return_value=SimpleNamespace(to_payload=lambda: {"objective": "Retry fanout"}),
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                    return_value=(planner_result, {"planning": "package"}),
                ),
            ):
                handle = retry_workflow_operation_with_registered_handler(
                    session=session,
                    settings=settings,
                    session_factory=session_factory,
                    workflow=workflow,
                    operation=fanout_operation,
                    handler_registry=self._resolver(fake_router=fake_router),
                )
                session.commit()

            backlog_attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == backlog_operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )
            fanout_attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == fanout_operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )

            assert handle.operation_type == "jira_child_fanout"
            assert fanout_operation.status == "completed"
            assert [attempt.attempt_number for attempt in backlog_attempts] == [1]
            assert [(attempt.attempt_number, attempt.status) for attempt in fanout_attempts] == [
                (1, "failed"),
                (2, "completed"),
            ]

    def test_backlog_planning_retry_propagates_retryable_model_contract_errors(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        settings = Settings(database_url=self.database_url)

        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-backlog-contract",
                name="Tenant Backlog Contract",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-backlog-contract",
                tenant_id="tenant-backlog-contract",
                name="Project Backlog Contract",
                github_repository="example/project-backlog-contract",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config=None,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-237",
                execution_id="wfexec-mab-237",
                workflow_type_key="parent_planning",
                tenant_id="tenant-backlog-contract",
                project_id="project-backlog-contract",
                source_system="jira",
                source_ref="MAB-237",
                display_name="Backlog planning retry contract",
                source_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="legacy",
                dedupe_scope="parent_planning",
                status="failed",
                last_error="Codex returned engineering_planning child_ticket_specs[1] without done_means",
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            backlog_operation = WorkflowOperation(
                operation_id="operation-backlog-planning-contract",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="backlog_planning",
                idempotency_key="workflow-definition:backlog_planning",
                status="failed",
                target_system="jira",
                target_ref="MAB-237",
                summary="Codex returned engineering_planning child_ticket_specs[1] without done_means",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project, workflow, backlog_operation])
            session.commit()

            fake_jira_adapter = SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    key="MAB-237",
                    summary="Backlog planning retry contract",
                    description="Parent planning",
                    labels=["pm-parent"],
                    status="To Do",
                ),
            )
            fake_router = SimpleNamespace(jira=lambda **_kwargs: fake_jira_adapter)

            with (
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_handlers.backlog_planning.resolve_parent_feature_brief",
                    return_value=SimpleNamespace(to_payload=lambda: {"objective": "Retry backlog planning"}),
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                    side_effect=RetryableSpecialistPlanningContractError(
                        "Codex returned engineering_planning child_ticket_specs[1] without done_means"
                    ),
                ),
                pytest.raises(RetryableSpecialistPlanningContractError),
            ):
                retry_workflow_operation_with_registered_handler(
                    session=session,
                    settings=settings,
                    session_factory=session_factory,
                    workflow=workflow,
                    operation=backlog_operation,
                    handler_registry=self._resolver(fake_router=fake_router),
                )

            session.commit()
            session.refresh(backlog_operation)
            attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == backlog_operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )

            assert backlog_operation.status == "failed"
            assert len(attempts) == 1
            assert attempts[0].status == "failed"
            assert attempts[0].error_category == "invalid_model_output"

    def test_backlog_planning_retry_publishes_questions_before_waiting_for_input(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        settings = Settings(database_url=self.database_url)

        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-backlog-blocked",
                name="Tenant Backlog Blocked",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-backlog-blocked",
                tenant_id="tenant-backlog-blocked",
                name="Project Backlog Blocked",
                github_repository="example/project-backlog-blocked",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config=None,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-235",
                execution_id="wfexec-mab-235",
                workflow_type_key="parent_planning",
                tenant_id="tenant-backlog-blocked",
                project_id="project-backlog-blocked",
                source_system="jira",
                source_ref="MAB-235",
                display_name="Backlog planning blocked retry",
                source_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="legacy",
                dedupe_scope="parent_planning",
                status="failed",
                last_error="Backlog planning needs clarification",
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            backlog_operation = WorkflowOperation(
                operation_id="operation-backlog-planning-blocked",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="backlog_planning",
                idempotency_key="workflow-definition:backlog_planning",
                status="failed",
                target_system="jira",
                target_ref="MAB-235",
                summary="Backlog planning needs clarification",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            comment_operation = WorkflowOperation(
                operation_id="operation-backlog-comment-projection",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="jira_comment_projection",
                idempotency_key="workflow-definition:jira_comment_projection",
                status="pending",
                target_system="jira",
                target_ref="MAB-235",
                summary=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                updated_at=now,
            )
            session.add_all([tenant, project, workflow, backlog_operation, comment_operation])
            session.commit()

            fake_jira_adapter = SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    key="MAB-235",
                    summary="Backlog planning blocked retry",
                    description="Parent planning",
                    labels=["pm-parent"],
                    status="To Do",
                ),
            )
            fake_router = SimpleNamespace(jira=lambda **_kwargs: fake_jira_adapter)
            planner_result = SimpleNamespace(
                planning_state="planning_blocked",
                pm_decision_requests=(
                    PMDecisionRequest(
                        request_id="pm-session-revocation",
                        question="Which broken-link reasons require immediate session revocation?",
                        why_it_matters="This changes session safety coverage.",
                        related_decision_ids=("session-revocation",),
                    ),
                ),
            )

            with (
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_handlers.backlog_planning.resolve_parent_feature_brief",
                    return_value=SimpleNamespace(to_payload=lambda: {"objective": "Retry backlog planning"}),
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                    return_value=(planner_result, {"planning": "package"}),
                ) as planner_mock,
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.resolve_pm_decisions",
                    return_value=SimpleNamespace(
                        resolved_decisions=(),
                        stakeholder_escalations=(
                            StakeholderEscalation(
                                escalation_id="stakeholder-session-revocation",
                                question="Which broken-link reasons require immediate session revocation?",
                                why_it_matters="This changes session safety coverage.",
                                business_impact_area="risk_compliance",
                                source_pm_decision_request_ids=("pm-session-revocation",),
                            ),
                        ),
                        updated_planning_context={},
                    ),
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_support.post_pm_product_clarification_questions_to_jira",
                    return_value=({"id": "comment-456"}, None),
                ) as post_comment_mock,
            ):
                handle = retry_workflow_operation_with_registered_handler(
                    session=session,
                    settings=settings,
                    session_factory=session_factory,
                    workflow=workflow,
                    operation=backlog_operation,
                    handler_registry=self._resolver(fake_router=fake_router),
                )
                session.commit()

            session.refresh(backlog_operation)
            session.refresh(comment_operation)

            assert handle.status == "waiting_for_input"
            assert backlog_operation.status == "waiting_for_input"
            assert "Which broken-link reasons require immediate session revocation?" in (
                backlog_operation.summary or ""
            )
            assert comment_operation.status == "completed"
            assert "comment-456" in (comment_operation.summary or "")
            planner_mock.assert_called_once()
            post_comment_mock.assert_called_once()

    def test_backlog_planning_retry_fails_contract_when_blocked_without_questions(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        settings = Settings(database_url=self.database_url)

        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-backlog-no-questions",
                name="Tenant Backlog No Questions",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-backlog-no-questions",
                tenant_id="tenant-backlog-no-questions",
                name="Project Backlog No Questions",
                github_repository="example/project-backlog-no-questions",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config=None,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-236",
                execution_id="wfexec-mab-236",
                workflow_type_key="parent_planning",
                tenant_id="tenant-backlog-no-questions",
                project_id="project-backlog-no-questions",
                source_system="jira",
                source_ref="MAB-236",
                display_name="Backlog planning blocked without questions",
                source_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="legacy",
                dedupe_scope="parent_planning",
                status="failed",
                last_error="Backlog planning needs clarification",
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            backlog_operation = WorkflowOperation(
                operation_id="operation-backlog-planning-no-questions",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="backlog_planning",
                idempotency_key="workflow-definition:backlog_planning",
                status="failed",
                target_system="jira",
                target_ref="MAB-236",
                summary="Backlog planning needs clarification",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project, workflow, backlog_operation])
            session.commit()

            fake_jira_adapter = SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    key="MAB-236",
                    summary="Backlog planning blocked without questions",
                    description="Parent planning",
                    labels=["pm-parent"],
                    status="To Do",
                ),
            )
            fake_router = SimpleNamespace(jira=lambda **_kwargs: fake_jira_adapter)
            planner_result = SimpleNamespace(
                planning_state="planning_blocked",
                pm_decision_requests=(),
            )

            with (
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_handlers.backlog_planning.resolve_parent_feature_brief",
                    return_value=SimpleNamespace(to_payload=lambda: {"objective": "Retry backlog planning"}),
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                    return_value=(planner_result, {"planning": "package"}),
                ),
            ):
                handle = retry_workflow_operation_with_registered_handler(
                    session=session,
                    settings=settings,
                    session_factory=session_factory,
                    workflow=workflow,
                    operation=backlog_operation,
                    handler_registry=self._resolver(fake_router=fake_router),
                )
                session.commit()

            session.refresh(backlog_operation)
            attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == backlog_operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )

            assert handle.status == "failed"
            assert backlog_operation.status == "failed"
            assert "blocked without PM decision requests" in (backlog_operation.summary or "")
            assert len(attempts) == 1
            assert attempts[0].status == "failed"
            assert attempts[0].error_category == "invalid_model_output"

    def test_engineering_clarification_projection_fails_without_jira_comment_id(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        settings = Settings(database_url=self.database_url)

        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-no-comment-id",
                name="Tenant No Comment ID",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-no-comment-id",
                tenant_id="tenant-no-comment-id",
                name="Project No Comment ID",
                github_repository="example/project-no-comment-id",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config=None,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-238",
                execution_id="wfexec-mab-238",
                workflow_type_key="parent_planning",
                tenant_id="tenant-no-comment-id",
                project_id="project-no-comment-id",
                source_system="jira",
                source_ref="MAB-238",
                display_name="Missing Jira comment id",
                source_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="legacy",
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
            comment_operation = WorkflowOperation(
                operation_id="operation-missing-comment-id",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="jira_comment_projection",
                idempotency_key="workflow-definition:jira_comment_projection",
                status="pending",
                target_system="jira",
                target_ref="MAB-238",
                summary=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                updated_at=now,
            )
            session.add_all([tenant, project, workflow, comment_operation])
            session.commit()

            def create_comment_without_id(
                *,
                session,
                tenant,
                issue_key: str,
                comment: dict,
                settings: Settings,
            ) -> tuple[dict, None]:
                del session, tenant, issue_key, comment, settings
                return {}, None

            publisher = ParentWorkflowPlanningClarificationPublisher(
                session=session,
                settings=settings,
                tenant=tenant,
                project=project,
                workflow_type=get_workflow_type(session, workflow_type_key="parent_planning"),
                workflow=workflow,
                blocked_operation_type="backlog_planning",
                create_jira_comment_fn=create_comment_without_id,
                jira_adapter=SimpleNamespace(list_issue_comments=lambda **_kwargs: []),
            )

            with pytest.raises(RuntimeError, match="did not return a comment id"):
                publisher.publish_clarification(
                    issue_key="MAB-238",
                    questions=(ClarificationQuestion(question="Which audit window should v1 support?"),),
                )
            session.flush()

            session.refresh(comment_operation)
            attempt = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == comment_operation.operation_id)
                .one()
            )
            assert comment_operation.status == "failed"
            assert attempt.status == "failed"
            assert "did not return a comment id" in (comment_operation.summary or "")

    def test_jira_child_fanout_retry_persists_actionable_missing_input_questions(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        settings = Settings(database_url=self.database_url)

        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="example/project-a",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config=None,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-215",
                execution_id="wfexec-mab-215",
                workflow_type_key="parent_planning",
                tenant_id="tenant-a",
                project_id="project-a",
                source_system="jira",
                source_ref="MAB-215",
                display_name="Identity redesign",
                source_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="legacy",
                dedupe_scope="parent_planning",
                status="failed",
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
            fanout_operation = WorkflowOperation(
                operation_id="operation-jira-child-fanout",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="jira_child_fanout",
                idempotency_key="workflow-definition:jira_child_fanout",
                status="failed",
                target_system="jira",
                target_ref="MAB-215",
                summary=None,
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            comment_operation = WorkflowOperation(
                operation_id="operation-jira-comment-projection",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="jira_comment_projection",
                idempotency_key="workflow-definition:jira_comment_projection",
                status="pending",
                target_system="jira",
                target_ref="MAB-215",
                summary=None,
                created_at=now,
                started_at=None,
                finished_at=None,
                updated_at=now,
            )
            session.add_all([tenant, project, workflow, fanout_operation, comment_operation])
            session.commit()

            planner_result = SimpleNamespace(
                planning_state="planning_blocked",
                pm_decision_requests=(
                    PMDecisionRequest(
                        request_id="pm-invite-ttl",
                        question="What invitation TTL should v1 enforce for automatic expiry?",
                        why_it_matters="This changes link validity and account recovery behavior.",
                        related_decision_ids=("invite-ttl",),
                    ),
                    PMDecisionRequest(
                        request_id="pm-audit-retention",
                        question="What audit retention window must exports support in v1?",
                        why_it_matters="This changes export retention promises.",
                        related_decision_ids=("audit-retention",),
                    ),
                ),
            )
            stale_questions = ClarificationQuestionSet.from_values(
                [
                    {
                        "question": "What invitation TTL should v1 enforce for automatic expiry?",
                        "why_it_matters": "This changes link validity and account recovery behavior.",
                    },
                    {
                        "question": "What audit retention window must exports support in v1?",
                        "why_it_matters": "This changes export retention promises.",
                    },
                ]
            ).questions
            upsert_clarification_projection(
                session=session,
                spec=ClarificationProjectionSpec(
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    context_type=FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION,
                    issue_key="MAB-215",
                    request_id="parent-planning-clarification:MAB-215",
                    origin_command="clarify",
                    questions=stale_questions,
                    metadata={
                        "questions": [question.to_payload() for question in stale_questions],
                        "source": "workflow_operation_retry",
                        "blocked_operation_type": "jira_child_fanout",
                        "workflow_id": workflow.workflow_id,
                        "jira_comment_id": "deleted-comment-999",
                    },
                ),
            )
            session.commit()

            comment_lookups: list[str] = []

            def list_issue_comments(*, issue_id_or_key: str):
                comment_lookups.append(issue_id_or_key)
                return []

            fake_jira_adapter = SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    key="MAB-215",
                    summary="Identity redesign",
                    description="Parent planning",
                    labels=["pm-parent"],
                ),
                list_issue_comments=list_issue_comments,
            )
            fake_router = SimpleNamespace(jira=lambda **_kwargs: fake_jira_adapter)

            with (
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_handlers.jira_child_fanout.resolve_parent_feature_brief",
                    return_value=SimpleNamespace(to_payload=lambda: {"objective": "Ship identity redesign"}),
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                    return_value=(planner_result, {"planning": "package"}),
                ) as planner_mock,
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.resolve_pm_decisions",
                    return_value=SimpleNamespace(
                        resolved_decisions=(),
                        stakeholder_escalations=(
                            StakeholderEscalation(
                                escalation_id="stakeholder-invite-ttl",
                                question="What invitation TTL should v1 enforce for automatic expiry?",
                                why_it_matters="This changes link validity and account recovery behavior.",
                                business_impact_area="customer_business_impact",
                                source_pm_decision_request_ids=("pm-invite-ttl",),
                            ),
                            StakeholderEscalation(
                                escalation_id="stakeholder-audit-retention",
                                question="What audit retention window must exports support in v1?",
                                why_it_matters="This changes export retention promises.",
                                business_impact_area="risk_compliance",
                                source_pm_decision_request_ids=("pm-audit-retention",),
                            ),
                        ),
                        updated_planning_context={},
                    ),
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_support.post_pm_product_clarification_questions_to_jira",
                    return_value=({"id": "comment-123"}, None),
                ) as post_comment_mock,
            ):
                handle = retry_workflow_operation_with_registered_handler(
                    session=session,
                    settings=settings,
                    session_factory=session_factory,
                    workflow=workflow,
                    operation=fanout_operation,
                    handler_registry=self._resolver(fake_router=fake_router),
                )
                session.commit()

            session.refresh(workflow)
            session.refresh(fanout_operation)
            session.refresh(comment_operation)

            assert handle.operation_type == "jira_child_fanout"
            assert handle.status == "waiting_for_input"
            assert workflow.status == "waiting_for_input"
            assert fanout_operation.status == "waiting_for_input"
            assert "What invitation TTL should v1 enforce" in (fanout_operation.summary or "")
            assert comment_operation.status == "completed"
            assert "comment-123" in (comment_operation.summary or "")
            planner_mock.assert_called_once()
            post_comment_mock.assert_called_once()
            assert comment_lookups == []
            followup_context = (
                session.query(FollowupContext)
                .filter(
                    FollowupContext.issue_key == "MAB-215",
                    FollowupContext.context_type == FOLLOWUP_CONTEXT_PARENT_PLANNING_CLARIFICATION,
                    FollowupContext.status == "active",
                )
                .one()
            )
            assert followup_context.metadata_json["jira_comment_id"] == "comment-123"
            assert [question.to_payload() for question in post_comment_mock.call_args.kwargs["questions"]] == [
                {
                    "question": "What invitation TTL should v1 enforce for automatic expiry?",
                    "why_it_matters": "This changes link validity and account recovery behavior.",
                    "source_ref": "stakeholder_escalation",
                },
                {
                    "question": "What audit retention window must exports support in v1?",
                    "why_it_matters": "This changes export retention promises.",
                    "source_ref": "stakeholder_escalation",
                },
            ]

    def test_jira_child_fanout_retry_requires_confirmed_parent_brief_snapshot(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        settings = Settings(database_url=self.database_url)

        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-b",
                name="Tenant B",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-b",
                tenant_id="tenant-b",
                name="Project B",
                github_repository="example/project-b",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config=None,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-216",
                execution_id="wfexec-mab-216",
                workflow_type_key="parent_planning",
                tenant_id="tenant-b",
                project_id="project-b",
                source_system="jira",
                source_ref="MAB-216",
                display_name="Identity redesign",
                source_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="legacy",
                dedupe_scope="parent_planning",
                status="failed",
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
            fanout_operation = WorkflowOperation(
                operation_id="operation-jira-child-fanout-b",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="jira_child_fanout",
                idempotency_key="workflow-definition:jira_child_fanout",
                status="failed",
                target_system="jira",
                target_ref="MAB-216",
                summary=None,
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project, workflow, fanout_operation])
            session.commit()

            fake_jira_adapter = SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    key="MAB-216",
                    summary="Identity redesign",
                    description="Parent planning",
                    labels=["pm-parent"],
                ),
            )
            fake_router = SimpleNamespace(jira=lambda **_kwargs: fake_jira_adapter)

            with (
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_handlers.jira_child_fanout.resolve_parent_feature_brief",
                    return_value=None,
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                ) as planner_mock,
            ):
                handle = retry_workflow_operation_with_registered_handler(
                    session=session,
                    settings=settings,
                    session_factory=session_factory,
                    workflow=workflow,
                    operation=fanout_operation,
                    handler_registry=self._resolver(fake_router=fake_router),
                )
                session.commit()

            session.refresh(workflow)
            session.refresh(fanout_operation)
            attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == fanout_operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )

            assert handle.status == "failed"
            assert workflow.status == "failed"
            assert fanout_operation.status == "failed"
            assert fanout_operation.summary == "No confirmed parent brief snapshot is available for MAB-216"
            assert len(attempts) == 1
            assert attempts[0].attempt_number == 1
            assert attempts[0].status == "failed"
            assert attempts[0].error_category == "missing_input"
            assert attempts[0].error_message == "No confirmed parent brief snapshot is available for MAB-216"
            planner_mock.assert_not_called()

    def test_retry_rejects_operation_with_existing_running_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        settings = Settings(database_url=self.database_url)

        with session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-running",
                name="Tenant Running",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                experience_config={},
                setup_state={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-running",
                tenant_id="tenant-running",
                name="Project Running",
                github_repository="example/project-running",
                jira_project_key="MAB",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config=None,
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-234",
                execution_id="wfexec-mab-234",
                workflow_type_key="parent_planning",
                tenant_id="tenant-running",
                project_id="project-running",
                source_system="jira",
                source_ref="MAB-234",
                display_name="Running retry",
                source_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="legacy",
                dedupe_scope="parent_planning",
                status="failed",
                last_error="Previous retry is still active",
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
                operation_id="operation-running-backlog",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="backlog_planning",
                idempotency_key="workflow-definition:backlog_planning",
                status="failed",
                target_system="jira",
                target_ref="MAB-234",
                summary="Previous retry is still active",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            running_attempt = WorkflowOperationAttempt(
                attempt_id="attempt-running-backlog",
                operation_id=operation.operation_id,
                attempt_number=1,
                status="running",
                error_category=None,
                error_message=None,
                status_detail=None,
                retryable=False,
                next_retry_at=None,
                created_at=now,
                started_at=now,
                finished_at=None,
            )
            session.add_all([tenant, project, workflow, operation, running_attempt])
            session.commit()

            fake_jira_adapter = SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    key="MAB-234",
                    summary="Running retry",
                    description="Parent planning",
                    labels=["pm-parent"],
                    status="To Do",
                ),
            )
            fake_router = SimpleNamespace(jira=lambda **_kwargs: fake_jira_adapter)

            with (
                patch(
                    "orchestrator.core.parent_feature_workflow.retry_handlers.backlog_planning.resolve_parent_feature_brief",
                    return_value=SimpleNamespace(to_payload=lambda: {"objective": "Retry backlog planning"}),
                ),
                patch("orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent") as planner_mock,
            ):
                with self.assertRaisesRegex(
                    WorkflowOperationAttemptAlreadyRunningError,
                    "already has active attempt 1",
                ):
                    retry_workflow_operation_with_registered_handler(
                        session=session,
                        settings=settings,
                        session_factory=session_factory,
                        workflow=workflow,
                        operation=operation,
                        handler_registry=self._resolver(fake_router=fake_router),
                    )

            planner_mock.assert_not_called()
            attempts = (
                session.query(WorkflowOperationAttempt)
                .filter(WorkflowOperationAttempt.operation_id == operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
                .all()
            )
            self.assertEqual([attempt.attempt_id for attempt in attempts], ["attempt-running-backlog"])
