from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.core.config import Settings
from orchestrator.core.workflow_advance import InvalidWorkflowOperationRetryError
from orchestrator.core.workflow_handler_composition import build_installed_workflow_handler_registry
from orchestrator.core.workflow_operation_retry_use_case import retry_workflow_operation_with_registered_handler
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation
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

            fake_jira_adapter = SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    key="MAB-215",
                    summary="Identity redesign",
                    description="Parent planning",
                    labels=["pm-parent"],
                ),
            )
            fake_router = SimpleNamespace(jira=lambda **_kwargs: fake_jira_adapter)
            planner_result = SimpleNamespace(
                planning_state="planning_blocked",
                open_behavior_questions=(
                    {
                        "question": "What invitation TTL should v1 enforce for automatic expiry?",
                        "why_it_matters": "This changes link validity and account recovery behavior.",
                    },
                    "What audit retention window must exports support in v1?",
                ),
            )

            with (
                patch(
                    "orchestrator.core.parent_feature_workflow.retry.resolve_parent_feature_brief",
                    return_value=SimpleNamespace(to_payload=lambda: {"objective": "Ship identity redesign"}),
                ),
                patch(
                    "orchestrator.core.workflow_execution_status.list_workflow_type_operations",
                    return_value=[
                        SimpleNamespace(operation_type="jira_child_fanout", required=True),
                        SimpleNamespace(operation_type="jira_comment_projection", required=False),
                    ],
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                    return_value=(planner_result, {"planning": "package"}),
                ) as planner_mock,
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentChildSyncGateway.seed_parent_backlog_children",
                    return_value={"requires_input": True, "questions": []},
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.retry.has_matching_active_clarification_state",
                    return_value=False,
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.retry.upsert_clarification_projection",
                    return_value=SimpleNamespace(metadata={"questions": []}),
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.retry._post_engineering_clarification_questions_to_jira",
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

            assert handle.status == "failed"
            assert workflow.status == "failed"
            assert fanout_operation.status == "failed"
            assert "Answer the product clarification on Jira issue MAB-215" in (fanout_operation.summary or "")
            assert "What invitation TTL should v1 enforce for automatic expiry?" in (fanout_operation.summary or "")
            assert "What audit retention window must exports support in v1?" in (fanout_operation.summary or "")
            assert comment_operation.status == "completed"
            planner_mock.assert_called_once()
            post_comment_mock.assert_called_once()
            assert [question.to_payload() for question in post_comment_mock.call_args.kwargs["questions"]] == [
                {
                    "question": "What invitation TTL should v1 enforce for automatic expiry?",
                    "why_it_matters": "This changes link validity and account recovery behavior.",
                },
                {"question": "What audit retention window must exports support in v1?"},
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
                    "orchestrator.core.parent_feature_workflow.retry.resolve_parent_feature_brief",
                    return_value=None,
                ),
                patch(
                    "orchestrator.core.parent_feature_workflow.adapters._ParentBriefPlanner.plan_backlog_parent",
                ) as planner_mock,
            ):
                with pytest.raises(InvalidWorkflowOperationRetryError) as excinfo:
                    retry_workflow_operation_with_registered_handler(
                        session=session,
                        settings=settings,
                        session_factory=session_factory,
                        workflow=workflow,
                        operation=fanout_operation,
                        handler_registry=self._resolver(fake_router=fake_router),
                    )

            session.refresh(workflow)
            session.refresh(fanout_operation)

            assert workflow.status == "failed"
            assert fanout_operation.status == "failed"
            assert "No confirmed parent brief snapshot is available for MAB-216" in str(excinfo.value)
            planner_mock.assert_not_called()
