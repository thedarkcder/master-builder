from __future__ import annotations

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
import unittest

from sqlalchemy import select

from orchestrator.core.development.start_work import StartWorkUseCase
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import (
    Base,
    Project,
    Run,
    Tenant,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
)
from orchestrator.tools.atlassian_oauth import JiraIssueDetail


def _now() -> datetime:
    return datetime.now(timezone.utc)


class _FakeIssueGateway:
    def __init__(self, *, parent: JiraIssueDetail, children: list[JiraIssueDetail]) -> None:
        self.parent = parent
        self.children = children
        self.transitions: list[tuple[str, str]] = []

    def load_issue_detail(self, issue_key: str) -> JiraIssueDetail:
        if issue_key == self.parent.key:
            return self.parent
        for child in self.children:
            if child.key == issue_key:
                return child
        raise AssertionError(f"unexpected issue detail lookup: {issue_key}")

    def load_child_details(self, *, project_key: str, parent_issue_key: str) -> list[JiraIssueDetail]:
        assert project_key == "MAB"
        assert parent_issue_key == self.parent.key
        return list(self.children)

    def transition_issue(self, *, issue_key: str, target_status: str) -> None:
        self.transitions.append((issue_key, target_status))


class StartWorkUseCaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.tmp.name}/start_work.db"
        reset_db_engine_cache()
        self.session_factory = create_session_factory(self.database_url)
        with self.session_factory() as session:
            Base.metadata.create_all(bind=session.get_bind())
            now = _now()
            session.add(
                Tenant(
                    tenant_id="example",
                    name="Route 25",
                    is_enabled=True,
                    archived_at=None,
                    purge_after_at=None,
                    jira_config={"ready_statuses": ["Ready for Agent"]},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    experience_config={},
                    setup_state={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="example-default",
                    tenant_id="example",
                    name="Route 25",
                    github_repository="org/repo",
                    jira_project_key="MAB",
                    policy_overrides={},
                    architecture_docs_config={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                WorkflowExecution(
                    workflow_id="parent_planning:MAB-243",
                    execution_id="exec-parent",
                    workflow_type_key="parent_planning",
                    tenant_id="example",
                    project_id="example-default",
                    source_system="jira",
                    source_ref="MAB-243",
                    display_name="Identity redesign",
                    source_description="Parent brief",
                    repo_url="org/repo",
                    branch=None,
                    pr_url=None,
                    orchestration_backend="temporal",
                    dedupe_scope="parent_planning",
                    status="completed",
                    last_error=None,
                    active_run_id=None,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.tmp.cleanup()
        reset_db_engine_cache()

    def test_start_parent_work_queues_engineering_children_and_records_operation_attempt(self) -> None:
        gateway = _FakeIssueGateway(
            parent=JiraIssueDetail(
                key="MAB-243",
                summary="Identity redesign",
                status="To Do",
                description="Parent brief",
                issue_type="Epic",
                labels=["pm-parent"],
            ),
            children=[
                JiraIssueDetail(
                    key="MAB-244",
                    summary="Implement local auth binding",
                    status="Backlog",
                    description="Story brief",
                    issue_type="Task",
                    labels=["engineering-child"],
                ),
                JiraIssueDetail(
                    key="MAB-245",
                    summary="Implement recovery approvals",
                    status="To Do",
                    description="Story brief",
                    issue_type="Task",
                    labels=["engineering-child"],
                ),
            ],
        )
        with self.session_factory() as session:
            result = StartWorkUseCase(
                session=session,
                issue_gateway=gateway,
            ).start(
                tenant=session.get(Tenant, "example"),
                project=session.get(Project, "example-default"),
                issue_key="MAB-243",
                target_status="To Do",
                actor="admin",
                reason="operator_start",
                source_workflow_id="parent_planning:MAB-243",
            )

            runs = session.execute(select(Run).order_by(Run.issue_key)).scalars().all()
            operation = session.execute(select(WorkflowOperation)).scalar_one()
            attempts = session.execute(select(WorkflowOperationAttempt)).scalars().all()

        self.assertEqual([run.issue_key for run in runs], ["MAB-244", "MAB-245"])
        self.assertEqual([run.status for run in runs], ["queued", "queued"])
        self.assertEqual(gateway.transitions, [("MAB-244", "To Do")])
        self.assertEqual(operation.operation_type, "development_start")
        self.assertEqual(operation.status, "completed")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "completed")
        self.assertEqual([item.issue_key for item in result.queued], ["MAB-244", "MAB-245"])

    def test_repeated_start_parent_work_does_not_duplicate_runs_or_attempts(self) -> None:
        gateway = _FakeIssueGateway(
            parent=JiraIssueDetail(
                key="MAB-243",
                summary="Identity redesign",
                status="To Do",
                description="Parent brief",
                issue_type="Epic",
                labels=["pm-parent"],
            ),
            children=[
                JiraIssueDetail(
                    key="MAB-244",
                    summary="Implement local auth binding",
                    status="To Do",
                    description="Story brief",
                    issue_type="Task",
                    labels=["engineering-child"],
                ),
            ],
        )
        with self.session_factory() as session:
            use_case = StartWorkUseCase(session=session, issue_gateway=gateway)
            tenant = session.get(Tenant, "example")
            project = session.get(Project, "example-default")
            kwargs = {
                "tenant": tenant,
                "project": project,
                "issue_key": "MAB-243",
                "target_status": "To Do",
                "actor": "admin",
                "reason": "operator_start",
                "source_workflow_id": "parent_planning:MAB-243",
            }
            first = use_case.start(**kwargs)
            second = use_case.start(**kwargs)

            runs = session.execute(select(Run)).scalars().all()
            attempts = session.execute(select(WorkflowOperationAttempt)).scalars().all()

        self.assertEqual(len(runs), 1)
        self.assertEqual(first.queued[0].run_id, second.skipped[0].run_id)
        self.assertEqual(second.skipped[0].reason, "already_started")
        self.assertEqual(len(attempts), 1)

    def test_start_story_without_engineering_children_queues_the_story(self) -> None:
        gateway = _FakeIssueGateway(
            parent=JiraIssueDetail(
                key="MAB-250",
                summary="Implement auth binding story",
                status="To Do",
                description="Story brief",
                issue_type="Task",
                labels=["engineering-child"],
            ),
            children=[],
        )
        with self.session_factory() as session:
            result = StartWorkUseCase(session=session, issue_gateway=gateway).start(
                tenant=session.get(Tenant, "example"),
                project=session.get(Project, "example-default"),
                issue_key="MAB-250",
                target_status="To Do",
                actor="admin",
                reason="operator_start",
            )
            runs = session.execute(select(Run)).scalars().all()

        self.assertEqual([run.issue_key for run in runs], ["MAB-250"])
        self.assertEqual(result.queued[0].issue_key, "MAB-250")
        self.assertEqual(gateway.transitions, [])


class StartWorkCommentCommandTests(unittest.TestCase):
    def test_mb_start_is_a_valid_jira_comment_command(self) -> None:
        from orchestrator.api.webhooks.jira_payload_contracts import parse_jira_comment_command

        command, argument, error = parse_jira_comment_command(
            {"comment": {"body": {"content": [{"content": [{"text": "/mb start"}]}]}}}
        )

        self.assertEqual(command, "start")
        self.assertIsNone(argument)
        self.assertIsNone(error)


class StartWorkActionTokenTests(unittest.TestCase):
    def test_signed_start_work_token_round_trips_and_rejects_wrong_execution(self) -> None:
        from orchestrator.core.development.start_work_links import (
            StartWorkActionTokenClaims,
            build_start_work_action_url,
            create_start_work_action_token,
            verify_start_work_action_token,
        )
        claims_input = StartWorkActionTokenClaims(
            tenant_id="example",
            project_id="example-default",
            execution_id="exec-parent",
            workflow_id="parent_planning:MAB-243",
            issue_key="MAB-243",
        )

        token = create_start_work_action_token(
            claims=claims_input,
            secret="test-secret",
            ttl_seconds=300,
        )
        action_url = build_start_work_action_url(
            admin_ui_base_url="https://app.example.test",
            claims=claims_input,
            secret="test-secret",
            ttl_seconds=300,
        )

        claims = verify_start_work_action_token(
            token=token,
            secret="test-secret",
            expected_execution_id="exec-parent",
        )
        self.assertEqual(claims.issue_key, "MAB-243")
        self.assertIn("/example/start/exec-parent?", action_url)
        self.assertIn("startDevelopmentToken=", action_url)

        with self.assertRaises(ValueError):
            verify_start_work_action_token(
                token=token,
                secret="test-secret",
                expected_execution_id="wrong-execution",
            )
