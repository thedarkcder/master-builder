from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.config import Settings
from orchestrator.core.workflow_operation_executor import execute_workflow_operation_retry
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class WorkflowOperationExecutorTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="workflow-operation-executor")

    def tearDown(self) -> None:
        self._cleanup_test_database()

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
                issue_key="MAB-215",
                issue_summary="Identity redesign",
                issue_description="Parent planning",
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

            with (
                patch(
                    "orchestrator.core.workflow_operation_executor.resolve_parent_feature_brief",
                    return_value=SimpleNamespace(to_payload=lambda: {"objective": "Ship identity redesign"}),
                ),
                patch(
                    "orchestrator.core.workflow_operation_executor.list_workflow_type_operations",
                    return_value=[
                        SimpleNamespace(operation_type="jira_child_fanout"),
                        SimpleNamespace(operation_type="jira_comment_projection"),
                    ],
                ),
                patch(
                    "orchestrator.core.workflow_operation_executor._ParentBriefPlanner.plan_backlog_parent",
                    return_value=(
                        SimpleNamespace(
                            planning_state="planning_blocked",
                            open_behavior_questions=(
                                "What invitation TTL should v1 enforce for automatic expiry?",
                                "What audit retention window must exports support in v1?",
                            ),
                        ),
                        {"planning": "package"},
                    ),
                ),
                patch(
                    "orchestrator.core.workflow_operation_executor._ParentChildSyncGateway.seed_parent_backlog_children",
                    return_value={"requires_input": True, "questions": []},
                ),
                patch(
                    "orchestrator.core.workflow_operation_executor.has_matching_active_clarification_state",
                    return_value=False,
                ),
                patch(
                    "orchestrator.core.workflow_operation_executor.upsert_clarification_projection",
                    return_value=SimpleNamespace(metadata={"questions": []}),
                ),
                patch(
                    "orchestrator.core.workflow_operation_executor._post_engineering_clarification_questions_to_jira",
                    return_value=({"id": "comment-123"}, None),
                ) as post_comment_mock,
            ):
                handle = execute_workflow_operation_retry(
                    session=session,
                    settings=settings,
                    session_factory=session_factory,
                    workflow=workflow,
                    operation=fanout_operation,
                    integration_router=fake_router,
                    build_runtime_for_selector_fn=lambda *_args, **_kwargs: object(),
                    seed_issues_with_runtime_fn=lambda *_args, **_kwargs: ("seeded", {}),
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
            post_comment_mock.assert_called_once()
