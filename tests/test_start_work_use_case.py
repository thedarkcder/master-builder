from __future__ import annotations

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from sqlalchemy import select

from orchestrator.api.admin.workflows.start_development_service import start_work_item_from_board
from orchestrator.core.development.executable_work_items import child_work_item_id, parent_work_item_id, parse_work_item_id
from orchestrator.core.development.start_work import StartWorkUseCase
from orchestrator.core.pm.interview_service import (
    PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
    PM_INTERVIEW_STATUS_PM_COMPLETED,
)
from orchestrator.core.security import AuthenticatedPrincipal
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import (
    Base,
    PMInterviewCase,
    Project,
    Run,
    Tenant,
    WorkflowExecutableWorkItem,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
    WorkflowOperationWorkUnit,
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
                    tenant_id="route25",
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
                    project_id="route25-default",
                    tenant_id="route25",
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
                    tenant_id="route25",
                    project_id="route25-default",
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

    def _seed_self_executable_contract(self, session) -> None:  # noqa: ANN001
        now = _now()
        session.add(
            PMInterviewCase(
                case_id="pm-snapshot-mab-243",
                tenant_id="route25",
                project_id="route25-default",
                request_id="pm-snapshot-mab-243",
                parent_issue_key="MAB-243",
                source_kind=PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
                status=PM_INTERVIEW_STATUS_PM_COMPLETED,
                channel_id="",
                thread_channel_id=None,
                root_message_id=None,
                owner_user_id=None,
                source_text="",
                brief_json={
                    "objective": "Generate signed downloads for queued audit export requests.",
                    "user_value": "Administrators can securely retrieve completed exports.",
                    "acceptance_criteria": ["Signed download URLs are generated for completed export jobs."],
                    "scope_in": ["Signed URL generation"],
                    "scope_out": ["Changing export generation"],
                    "constraints": ["Links must expire."],
                    "risks": ["Leaked URLs expose exports."],
                    "success_outcomes": ["Audit exports can be downloaded securely."],
                    "recommendation": "Implement signed download creation directly on the source task.",
                },
                evidence_json=[],
                question_history_json=[],
                current_question_json={},
                next_question_json={},
                missing_slots_json=[],
                notes_json={},
                created_at=now,
                updated_at=now,
                closed_at=now,
            )
        )
        operation = WorkflowOperation(
            operation_id="operation-backlog-mab-243",
            workflow_id="parent_planning:MAB-243",
            run_id=None,
            operation_type="backlog_planning",
            idempotency_key="workflow-definition:backlog_planning",
            status="completed",
            target_system="jira",
            target_ref="MAB-243",
            summary="Backlog planning completed.",
            created_at=now,
            started_at=now,
            finished_at=now,
            updated_at=now,
        )
        attempt = WorkflowOperationAttempt(
            attempt_id="attempt-backlog-mab-243",
            operation_id=operation.operation_id,
            attempt_number=1,
            status="completed",
            retryable=False,
            next_retry_at=None,
            created_at=now,
            started_at=now,
            finished_at=now,
        )
        work_unit = WorkflowOperationWorkUnit(
            work_unit_id="work-unit-planning-package-mab-243",
            operation_id=operation.operation_id,
            parent_attempt_id=attempt.attempt_id,
            unit_key="backlog_planning.package_assembly",
            unit_kind="assembly",
            idempotency_key="mab-243-planning-package",
            input_fingerprint="fingerprint-mab-243",
            status="completed",
            output_json={
                "planning_package": {
                    "planning_state": "planning_completed",
                    "specialist_outputs": {},
                    "child_issues": [],
                    "technical_decisions": [],
                    "pm_decision_requests": [],
                }
            },
            created_at=now,
            updated_at=now,
            completed_at=now,
        )
        session.add_all([operation, attempt, work_unit])

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
                tenant=session.get(Tenant, "route25"),
                project=session.get(Project, "route25-default"),
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
            tenant = session.get(Tenant, "route25")
            project = session.get(Project, "route25-default")
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

    def test_failed_existing_run_does_not_block_restart(self) -> None:
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
            now = _now()
            session.add(
                WorkflowExecution(
                    workflow_id="issue-execution-mab-244-failed",
                    execution_id="exec-mab-244-failed",
                    workflow_type_key="issue_execution",
                    tenant_id="route25",
                    project_id="route25-default",
                    source_system="jira",
                    source_ref="MAB-244",
                    display_name="Failed child run",
                    source_description="Previous failed run",
                    repo_url="org/repo",
                    branch="feature/MAB-244",
                    pr_url=None,
                    orchestration_backend="temporal",
                    dedupe_scope="issue_execution",
                    status="failed",
                    last_error="Previous failure",
                    active_run_id="run-mab-244-failed",
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Run(
                    run_id="run-mab-244-failed",
                    tenant_id="route25",
                    project_id="route25-default",
                    issue_key="MAB-244",
                    issue_summary="Failed child run",
                    repo_url="org/repo",
                    branch="feature/MAB-244",
                    pr_url=None,
                    status="failed",
                    last_error="Previous failure",
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                    dedupe_scope="issue_execution",
                    workflow_id="issue-execution-mab-244-failed",
                    attempt_number=1,
                    entry_mode="dev",
                    entry_stage="dev",
                    required_runtime_kinds_json=[],
                )
            )
            session.commit()

            result = StartWorkUseCase(session=session, issue_gateway=gateway).start(
                tenant=session.get(Tenant, "route25"),
                project=session.get(Project, "route25-default"),
                issue_key="MAB-243",
                target_status="To Do",
                actor="admin",
                reason="operator_start",
                source_workflow_id="parent_planning:MAB-243",
            )
            runs = session.execute(select(Run).where(Run.issue_key == "MAB-244").order_by(Run.created_at)).scalars().all()

        self.assertEqual([item.issue_key for item in result.queued], ["MAB-244"])
        self.assertEqual([run.status for run in runs], ["failed", "queued"])

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
                tenant=session.get(Tenant, "route25"),
                project=session.get(Project, "route25-default"),
                issue_key="MAB-250",
                target_status="To Do",
                actor="admin",
                reason="operator_start",
            )
            runs = session.execute(select(Run)).scalars().all()

        self.assertEqual([run.issue_key for run in runs], ["MAB-250"])
        self.assertEqual(result.queued[0].issue_key, "MAB-250")
        self.assertEqual(gateway.transitions, [])

    def test_start_completed_task_parent_without_children_queues_the_source_issue(self) -> None:
        gateway = _FakeIssueGateway(
            parent=JiraIssueDetail(
                key="MAB-243",
                summary="Generate signed downloads",
                status="Backlog",
                description="Task brief",
                issue_type="Task",
                labels=["pm-parent"],
            ),
            children=[],
        )
        with self.session_factory() as session:
            self._seed_self_executable_contract(session)
            session.commit()
            result = StartWorkUseCase(session=session, issue_gateway=gateway).start(
                tenant=session.get(Tenant, "route25"),
                project=session.get(Project, "route25-default"),
                issue_key="MAB-243",
                target_status="To Do",
                actor="admin",
                reason="operator_start",
                source_workflow_id="parent_planning:MAB-243",
            )
            runs = session.execute(select(Run)).scalars().all()

        self.assertEqual([run.issue_key for run in runs], ["MAB-243"])
        self.assertIn("Owned by Engineering", str(runs[0].issue_description))
        self.assertNotEqual(runs[0].issue_description, "Task brief")
        self.assertEqual(result.queued[0].issue_key, "MAB-243")
        self.assertEqual(gateway.transitions, [("MAB-243", "To Do")])

    def test_start_task_parent_without_completed_self_executable_contract_is_rejected(self) -> None:
        gateway = _FakeIssueGateway(
            parent=JiraIssueDetail(
                key="MAB-243",
                summary="Generate signed downloads",
                status="Backlog",
                description="Task brief",
                issue_type="Task",
                labels=["pm-parent"],
            ),
            children=[],
        )
        with self.session_factory() as session:
            with self.assertRaisesRegex(ValueError, "No executable engineering work found"):
                StartWorkUseCase(session=session, issue_gateway=gateway).start(
                    tenant=session.get(Tenant, "route25"),
                    project=session.get(Project, "route25-default"),
                    issue_key="MAB-243",
                    target_status="To Do",
                    actor="admin",
                    reason="operator_start",
                    source_workflow_id="parent_planning:MAB-243",
                )

    def test_work_item_ids_round_trip_parent_and_child_refs(self) -> None:
        parent_ref = parse_work_item_id(parent_work_item_id(execution_id="exec-parent"))
        child_ref = parse_work_item_id(child_work_item_id(execution_id="exec-parent", issue_key="MAB-244"))

        self.assertEqual(parent_ref.kind, "parent")
        self.assertEqual(parent_ref.execution_id, "exec-parent")
        self.assertIsNone(parent_ref.issue_key)
        self.assertEqual(child_ref.kind, "child")
        self.assertEqual(child_ref.execution_id, "exec-parent")
        self.assertEqual(child_ref.issue_key, "MAB-244")

    def test_start_work_item_from_board_queues_only_requested_child_issue(self) -> None:
        child = JiraIssueDetail(
            key="MAB-244",
            summary="Implement local auth binding",
            status="To Do",
            description="Story brief",
            issue_type="Task",
            labels=["engineering-child"],
        )

        class _FakeJiraAdapter:
            access_token = "token"
            cloud_id = "cloud"
            site_url = "https://example.atlassian.net"

            def __init__(self) -> None:
                self.client = self

            def list_child_issue_previews(self, *, project_key: str, parent_issue_key: str):  # noqa: ANN001
                if parent_issue_key == "MAB-243":
                    return [SimpleNamespace(key="MAB-244")]
                return []

            def get_issue_detail(self, *, issue_id_or_key: str):  # noqa: ANN001
                if issue_id_or_key == "MAB-244":
                    return child
                if issue_id_or_key == "MAB-243":
                    return JiraIssueDetail(
                        key="MAB-243",
                        summary="Identity redesign",
                        status="To Do",
                        description="Parent brief",
                        issue_type="Epic",
                        labels=["pm-parent"],
                    )
                raise AssertionError(f"unexpected issue lookup: {issue_id_or_key}")

        fake_router = SimpleNamespace(jira=lambda **_kwargs: _FakeJiraAdapter())

        with self.session_factory() as session:
            now = _now()
            session.add(
                WorkflowExecutableWorkItem(
                    work_item_id=child_work_item_id(execution_id="exec-parent", issue_key="MAB-244"),
                    item_kind="child",
                    tenant_id="route25",
                    project_id="route25-default",
                    parent_workflow_id="parent_planning:MAB-243",
                    parent_execution_id="exec-parent",
                    issue_key="MAB-244",
                    parent_issue_key="MAB-243",
                    issue_summary="Implement local auth binding",
                    issue_status="To Do",
                    issue_type="Task",
                    mb_work_state="planning_candidate",
                    source_system="jira",
                    source_external_id="10002",
                    source_payload_json={},
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
            )
            session.commit()
            result = start_work_item_from_board(
                session=session,
                work_item_id=child_work_item_id(execution_id="exec-parent", issue_key="MAB-244"),
                principal=AuthenticatedPrincipal(principal_type="platform_super_admin", username="admin"),
                integration_router=fake_router,
            )
            runs = session.execute(select(Run)).scalars().all()

        self.assertEqual(result.action, "engineering")
        self.assertEqual([run.issue_key for run in runs], ["MAB-244"])
        self.assertEqual([item.issue_key for item in result.queued], ["MAB-244"])

    def test_start_work_item_from_board_allows_projected_child_before_parent_completion(self) -> None:
        child = JiraIssueDetail(
            key="MAB-244",
            summary="Implement local auth binding",
            status="Testing",
            description="Story brief",
            issue_type="Task",
            labels=["engineering-child"],
        )

        class _FakeJiraAdapter:
            access_token = "token"
            cloud_id = "cloud"
            site_url = "https://example.atlassian.net"

            def __init__(self) -> None:
                self.client = self

            def list_child_issue_previews(self, *, project_key: str, parent_issue_key: str):  # noqa: ANN001
                if parent_issue_key == "MAB-243":
                    return [SimpleNamespace(key="MAB-244")]
                return []

            def get_issue_detail(self, *, issue_id_or_key: str):  # noqa: ANN001
                if issue_id_or_key == "MAB-244":
                    return child
                if issue_id_or_key == "MAB-243":
                    return JiraIssueDetail(
                        key="MAB-243",
                        summary="Identity redesign",
                        status="In Progress",
                        description="Parent brief",
                        issue_type="Epic",
                        labels=["pm-parent"],
                    )
                raise AssertionError(f"unexpected issue lookup: {issue_id_or_key}")

            def transition_issue(self, **kwargs):  # noqa: ANN001
                assert kwargs["issue_id_or_key"] == "MAB-244"
                assert kwargs["target_status"] == "To Do"

        fake_router = SimpleNamespace(jira=lambda **_kwargs: _FakeJiraAdapter())

        with self.session_factory() as session:
            now = _now()
            parent_workflow = session.get(WorkflowExecution, "parent_planning:MAB-243")
            parent_workflow.status = "waiting_for_input"
            parent_workflow.finished_at = None
            session.add(
                WorkflowExecutableWorkItem(
                    work_item_id=child_work_item_id(execution_id="exec-parent", issue_key="MAB-244"),
                    item_kind="child",
                    tenant_id="route25",
                    project_id="route25-default",
                    parent_workflow_id="parent_planning:MAB-243",
                    parent_execution_id="exec-parent",
                    issue_key="MAB-244",
                    parent_issue_key="MAB-243",
                    issue_summary="Implement local auth binding",
                    issue_status="Testing",
                    issue_type="Task",
                    mb_work_state="not_planning",
                    source_system="jira",
                    source_external_id="10002",
                    source_payload_json={},
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
            )
            session.commit()

            result = start_work_item_from_board(
                session=session,
                work_item_id=child_work_item_id(execution_id="exec-parent", issue_key="MAB-244"),
                principal=AuthenticatedPrincipal(principal_type="platform_super_admin", username="admin"),
                integration_router=fake_router,
            )
            runs = session.execute(select(Run)).scalars().all()

        self.assertEqual(result.action, "engineering")
        self.assertEqual([run.issue_key for run in runs], ["MAB-244"])
        self.assertEqual([item.issue_key for item in result.queued], ["MAB-244"])


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
            tenant_id="route25",
            project_id="route25-default",
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
        self.assertIn("/route25/start/exec-parent?", action_url)
        self.assertIn("startDevelopmentToken=", action_url)

        with self.assertRaises(ValueError):
            verify_start_work_action_token(
                token=token,
                secret="test-secret",
                expected_execution_id="wrong-execution",
            )
