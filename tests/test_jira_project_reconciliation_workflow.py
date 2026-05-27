from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from orchestrator.api.admin.workflows.execution_read_service import list_workflow_board_items
from orchestrator.core.jira_project_reconciliation.dependencies import JiraProjectReconciliationHandlerDeps
from orchestrator.core.jira_project_reconciliation.handlers import JiraProjectReconciliationAdvanceHandler
from orchestrator.core.jira_project_reconciliation.retry import JiraProjectReconciliationOperationRetryHandler
from orchestrator.core.workflow.advance import WorkflowAdvanceRequest, WorkflowTrigger, execute_workflow_advance
from orchestrator.core.workflow.execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow.handler_registry import build_workflow_handler_registry
from orchestrator.core.workflow.operation_retry_use_case import retry_workflow_operation_with_registered_handler
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import (
    Project,
    Tenant,
    WorkflowExecutableWorkItem,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
)
from orchestrator.tools.atlassian_oauth_models import JiraIssueDetail, JiraIssuePreview
from tests.test_support.db_harness import SqliteTemplateDbTestCase


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _FakeGateway:
    previews: list[JiraIssuePreview]
    details: dict[str, JiraIssueDetail]
    fail_label_once_for: set[str]

    def __post_init__(self) -> None:
        self.search_calls: list[tuple[str, str | None, int]] = []
        self.detail_calls: list[str] = []
        self.label_replacements: list[tuple[str, list[str]]] = []
        self.label_call_counts: Counter[str] = Counter()

    def search_project_issues_page(self, *, project_key: str, next_page_token: str | None, max_results: int):
        self.search_calls.append((project_key, next_page_token, max_results))
        start_at = int(next_page_token or "0")
        issues = list(self.previews[start_at:start_at + max_results])
        next_offset = start_at + len(issues)
        return SimpleNamespace(
            issues=issues,
            next_page_token=str(next_offset) if next_offset < len(self.previews) else None,
        )

    def get_issue_detail(self, *, issue_key: str) -> JiraIssueDetail:
        self.detail_calls.append(issue_key)
        return self.details[issue_key]

    def replace_issue_labels(self, *, issue_key: str, labels: list[str]) -> None:
        self.label_call_counts[issue_key] += 1
        if issue_key in self.fail_label_once_for:
            self.fail_label_once_for.remove(issue_key)
            raise RuntimeError(f"label replacement failed for {issue_key}")
        self.label_replacements.append((issue_key, list(labels)))


class JiraProjectReconciliationWorkflowTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="jira-project-reconciliation-workflow")
        self.session_factory = create_session_factory(self.database_url)
        with self.session_factory() as session:
            now = _now()
            session.add(
                Tenant(
                    tenant_id="route25",
                    name="Route 25",
                    is_enabled=True,
                    archived_at=None,
                    purge_after_at=None,
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
            session.commit()

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def _request(self, *, request_id: str = "reconcile-1", max_items: int = 1000) -> WorkflowAdvanceRequest:
        return WorkflowAdvanceRequest(
            workflow_handler_key="jira_project_reconciliation",
            tenant_id="route25",
            tenant=SimpleNamespace(tenant_id="route25"),
            project_id="route25-default",
            execution=WorkflowExecutionReference(
                key="route25-default",
                source=WorkflowSourceReference(
                    source_system="jira_project",
                    source_ref="MAB",
                ),
            ),
            payload={"request_id": request_id, "max_items": max_items},
            trigger=WorkflowTrigger(event="manual_sync"),
        )

    def _handler_registry(self, *, gateway: _FakeGateway):
        deps = JiraProjectReconciliationHandlerDeps(
            gateway_factory=lambda **_kwargs: gateway,
        )
        return build_workflow_handler_registry(
            advance_handlers={
                "jira_project_reconciliation": JiraProjectReconciliationAdvanceHandler(deps=deps),
            },
            operation_retry_handlers={
                "jira_project_reconciliation": JiraProjectReconciliationOperationRetryHandler(deps=deps),
            },
        )

    def test_full_scan_creates_missing_parent_workflows_and_persists_attempts(self) -> None:
        gateway = _FakeGateway(
            previews=[
                JiraIssuePreview(key="MAB-100", summary="Tenant auth redesign", status="Backlog"),
                JiraIssuePreview(key="MAB-101", summary="Build local auth verification", status="Backlog"),
            ],
            details={
                "MAB-100": JiraIssueDetail(
                    key="MAB-100",
                    summary="Tenant auth redesign",
                    status="Backlog",
                    description="Parent brief",
                    issue_type="Epic",
                    labels=["customer-facing", "engineering-child"],
                    issue_id="10001",
                    parent_key=None,
                    parent_issue_id=None,
                ),
                "MAB-101": JiraIssueDetail(
                    key="MAB-101",
                    summary="Build local auth verification",
                    status="Backlog",
                    description="Engineering story",
                    issue_type="Story",
                    labels=["pm-parent", "parent-mab-999"],
                    issue_id="10002",
                    parent_key="MAB-100",
                    parent_issue_id="10001",
                ),
            },
            fail_label_once_for=set(),
        )

        with self.session_factory() as session:
            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")

            result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            reconciliation_workflow = session.execute(
                select(WorkflowExecution).where(WorkflowExecution.workflow_type_key == "jira_project_reconciliation")
            ).scalar_one()
            parent_workflows = session.execute(
                select(WorkflowExecution).where(WorkflowExecution.workflow_type_key == "parent_planning")
            ).scalars().all()
            operations = {
                operation.operation_type: operation
                for operation in session.execute(
                    select(WorkflowOperation).where(WorkflowOperation.workflow_id == reconciliation_workflow.workflow_id)
                ).scalars()
            }
            attempts = session.execute(
                select(WorkflowOperationAttempt).join(
                    WorkflowOperation,
                    WorkflowOperation.operation_id == WorkflowOperationAttempt.operation_id,
                ).where(WorkflowOperation.workflow_id == reconciliation_workflow.workflow_id)
            ).scalars().all()
            work_items = session.execute(
                select(WorkflowExecutableWorkItem).order_by(
                    WorkflowExecutableWorkItem.item_kind,
                    WorkflowExecutableWorkItem.issue_key,
                )
            ).scalars().all()
            board_items = list_workflow_board_items(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                limit=100,
                offset=0,
            )

        assert result.handled is True
        assert reconciliation_workflow.status == "completed"
        assert [workflow.workflow_id for workflow in parent_workflows] == ["parent_planning:MAB-100"]
        assert parent_workflows[0].source_external_id == "10001"
        assert parent_workflows[0].status == "queued"
        assert parent_workflows[0].started_at is None
        assert [(item.item_kind, item.issue_key, item.parent_issue_key) for item in work_items] == [
            ("child", "MAB-101", "MAB-100"),
            ("parent", "MAB-100", None),
        ]
        parent_board_item = next(item for item in board_items if item.source_ref == "MAB-100")
        assert parent_board_item.work_item_id == f"parent:{parent_board_item.execution_id}"
        assert parent_board_item.startable is True
        assert parent_board_item.start_label == "Start planning"
        assert [(child.issue_key, child.summary, child.status) for child in parent_board_item.children] == [
            ("MAB-101", "Build local auth verification", "Backlog")
        ]
        assert parent_board_item.children[0].work_item_id == f"child:{parent_board_item.execution_id}:MAB-101"
        assert parent_board_item.children[0].startable is False
        assert parent_board_item.children[0].start_blocked_reason == "parent_not_completed"
        assert gateway.label_replacements == [
            ("MAB-100", ["customer-facing", "pm-parent"]),
            ("MAB-101", ["engineering-child", "parent-mab-100"]),
        ]
        assert set(operations) == {
            "jira_project_scan",
            "jira_issue_classification",
            "jira_label_reconciliation",
            "parent_workflow_reconciliation",
            "reconciliation_summary",
        }
        assert {attempt.status for attempt in attempts} == {"completed"}

    def test_board_does_not_derive_parent_cards_without_executable_work_item_projection(self) -> None:
        now = _now()
        with self.session_factory() as session:
            session.add(
                WorkflowExecution(
                    workflow_id="parent_planning:MAB-999",
                    execution_id="exec-unprojected-parent",
                    workflow_type_key="parent_planning",
                    tenant_id="route25",
                    project_id="route25-default",
                    source_system="jira",
                    source_ref="MAB-999",
                    source_external_id="19999",
                    display_name="Unprojected parent",
                    source_description="Should not be board visible without projection.",
                    repo_url=None,
                    branch=None,
                    pr_url=None,
                    orchestration_backend="temporal",
                    dedupe_scope="parent_planning",
                    status="queued",
                    last_error=None,
                    active_run_id=None,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
            session.commit()

            board_items = list_workflow_board_items(
                session=session,
                tenant_id="route25",
                project_id="route25-default",
                limit=100,
                offset=0,
            )

        assert board_items == []

    def test_limited_scan_updates_seen_projection_without_pruning_unseen_work_items(self) -> None:
        gateway = _FakeGateway(
            previews=[
                JiraIssuePreview(key="MAB-300", summary="Seen issue", status="Backlog"),
                JiraIssuePreview(key="MAB-301", summary="Unseen issue", status="Backlog"),
            ],
            details={
                "MAB-300": JiraIssueDetail(
                    key="MAB-300",
                    summary="Seen issue",
                    status="Backlog",
                    description="Seen parent brief",
                    issue_type="Epic",
                    labels=[],
                    issue_id="10300",
                    parent_key=None,
                    parent_issue_id=None,
                )
            },
            fail_label_once_for=set(),
        )

        with self.session_factory() as session:
            now = _now()
            session.add(
                WorkflowExecution(
                    workflow_id="parent_planning:MAB-301",
                    execution_id="exec-existing-unseen",
                    workflow_type_key="parent_planning",
                    tenant_id="route25",
                    project_id="route25-default",
                    source_system="jira",
                    source_ref="MAB-301",
                    source_external_id="10301",
                    display_name="Existing unseen parent",
                    source_description="Existing projection must survive limited scans.",
                    repo_url=None,
                    branch=None,
                    pr_url=None,
                    orchestration_backend="temporal",
                    dedupe_scope="parent_planning",
                    status="queued",
                    last_error=None,
                    active_run_id=None,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
            session.add(
                WorkflowExecutableWorkItem(
                    work_item_id="parent:exec-existing-unseen",
                    item_kind="parent",
                    tenant_id="route25",
                    project_id="route25-default",
                    parent_workflow_id="parent_planning:MAB-301",
                    parent_execution_id="exec-existing-unseen",
                    issue_key="MAB-301",
                    parent_issue_key=None,
                    issue_summary="Existing unseen parent",
                    issue_status="Backlog",
                    issue_type="Epic",
                    mb_work_state="planning_candidate",
                    source_system="jira",
                    source_external_id="10301",
                    source_payload_json={},
                    created_at=now,
                    updated_at=now,
                    last_seen_at=now,
                )
            )
            session.commit()

            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="limited-projection", max_items=1),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            work_items = session.execute(
                select(WorkflowExecutableWorkItem).order_by(WorkflowExecutableWorkItem.issue_key.asc())
            ).scalars().all()

        assert [item.issue_key for item in work_items] == ["MAB-300", "MAB-301"]
        assert work_items[0].work_item_id.startswith("parent:")
        assert work_items[1].work_item_id == "parent:exec-existing-unseen"
        assert gateway.search_calls == [("MAB", None, 1)]

    def test_scan_fails_when_provider_repeats_a_full_page(self) -> None:
        class _RepeatingPageGateway(_FakeGateway):
            def search_project_issues_page(self, *, project_key: str, next_page_token: str | None, max_results: int):
                self.search_calls.append((project_key, next_page_token, max_results))
                return SimpleNamespace(
                    issues=list(self.previews[:max_results]),
                    next_page_token="page-2" if next_page_token is None else "page-3",
                )

        gateway = _RepeatingPageGateway(
            previews=[
                JiraIssuePreview(key=f"MAB-{index}", summary=f"Issue {index}", status="Backlog")
                for index in range(1, 51)
            ],
            details={
                f"MAB-{index}": JiraIssueDetail(
                    key=f"MAB-{index}",
                    summary=f"Issue {index}",
                    status="Backlog",
                    description="Parent brief",
                    issue_type="Epic",
                    labels=[],
                    issue_id=f"10{index:03d}",
                    parent_key=None,
                    parent_issue_id=None,
                )
                for index in range(1, 51)
            },
            fail_label_once_for=set(),
        )

        with self.session_factory() as session:
            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")
            result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="repeating-page", max_items=100),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )

        assert result.failed is True
        assert "repeated a full Jira issue page" in str(result.reason)
        assert gateway.search_calls == [("MAB", None, 50), ("MAB", "page-2", 50)]

    def test_existing_parent_workflow_is_matched_by_stable_issue_id_without_duplicate_on_key_rename(self) -> None:
        gateway = _FakeGateway(
            previews=[JiraIssuePreview(key="MAB-200", summary="Renamed issue key", status="Backlog")],
            details={
                "MAB-200": JiraIssueDetail(
                    key="MAB-200",
                    summary="Renamed issue key",
                    status="Backlog",
                    description="Updated parent brief",
                    issue_type="Epic",
                    labels=["pm-parent"],
                    issue_id="10001",
                    parent_key=None,
                    parent_issue_id=None,
                )
            },
            fail_label_once_for=set(),
        )

        with self.session_factory() as session:
            now = _now()
            session.add(
                WorkflowExecution(
                    workflow_id="parent_planning:MAB-100",
                    execution_id="exec-existing",
                    workflow_type_key="parent_planning",
                    tenant_id="route25",
                    project_id="route25-default",
                    source_system="jira",
                    source_ref="MAB-100",
                    source_external_id="10001",
                    display_name="Original key",
                    source_description="Original brief",
                    repo_url=None,
                    branch=None,
                    pr_url=None,
                    orchestration_backend="temporal",
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
            )
            session.commit()

            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="rename-reconcile-1"),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            parent_workflows = session.execute(
                select(WorkflowExecution).where(WorkflowExecution.workflow_type_key == "parent_planning")
            ).scalars().all()

        assert len(parent_workflows) == 1
        assert parent_workflows[0].workflow_id == "parent_planning:MAB-100"
        assert parent_workflows[0].source_ref == "MAB-200"
        assert parent_workflows[0].display_name == "Renamed issue key"

    def test_release_ready_parent_source_deactivates_existing_planning_workflow(self) -> None:
        gateway = _FakeGateway(
            previews=[JiraIssuePreview(key="MAB-300", summary="Release-ready parent", status="Ready to Release")],
            details={
                "MAB-300": JiraIssueDetail(
                    key="MAB-300",
                    summary="Release-ready parent",
                    status="Ready to Release",
                    status_category_key="done",
                    description="Already past planning",
                    issue_type="Epic",
                    labels=["pm-parent"],
                    issue_id="30001",
                    parent_key=None,
                    parent_issue_id=None,
                )
            },
            fail_label_once_for=set(),
        )

        with self.session_factory() as session:
            now = _now()
            session.add(
                WorkflowExecution(
                    workflow_id="parent_planning:MAB-300",
                    execution_id="exec-release-ready",
                    workflow_type_key="parent_planning",
                    tenant_id="route25",
                    project_id="route25-default",
                    source_system="jira",
                    source_ref="MAB-300",
                    source_external_id="30001",
                    display_name="Release-ready parent",
                    source_description="Already past planning",
                    repo_url=None,
                    branch=None,
                    pr_url=None,
                    orchestration_backend="temporal",
                    dedupe_scope="parent_planning",
                    status="queued",
                    last_error=None,
                    active_run_id=None,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=None,
                    finished_at=None,
                    updated_at=now,
                )
            )
            session.commit()

            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="release-ready-reconcile-1"),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            parent_workflow = session.get(WorkflowExecution, "parent_planning:MAB-300")

        assert parent_workflow.status == "cancelled"
        assert parent_workflow.last_error == "Source item is no longer eligible for MB parent planning."

    def test_engineering_parent_source_reactivates_failed_or_waiting_board_workflows(self) -> None:
        gateway = _FakeGateway(
            previews=[
                JiraIssuePreview(key="MAB-303", summary="Failed stale parent", status="Testing"),
                JiraIssuePreview(key="MAB-304", summary="Waiting stale parent", status="Testing"),
            ],
            details={
                "MAB-303": JiraIssueDetail(
                    key="MAB-303",
                    summary="Failed stale parent",
                    status="Testing",
                    status_category_key="indeterminate",
                    description="Already in engineering",
                    issue_type="Epic",
                    labels=["pm-parent"],
                    issue_id="30301",
                    parent_key=None,
                    parent_issue_id=None,
                ),
                "MAB-304": JiraIssueDetail(
                    key="MAB-304",
                    summary="Waiting stale parent",
                    status="Testing",
                    status_category_key="indeterminate",
                    description="Already in engineering",
                    issue_type="Epic",
                    labels=["pm-parent"],
                    issue_id="30401",
                    parent_key=None,
                    parent_issue_id=None,
                ),
            },
            fail_label_once_for=set(),
        )

        with self.session_factory() as session:
            now = _now()
            for issue_key, issue_id, workflow_status in (
                ("MAB-303", "30301", "failed"),
                ("MAB-304", "30401", "waiting_for_input"),
            ):
                session.add(
                    WorkflowExecution(
                        workflow_id=f"parent_planning:{issue_key}",
                        execution_id=f"exec-{issue_key.lower()}",
                        workflow_type_key="parent_planning",
                        tenant_id="route25",
                        project_id="route25-default",
                        source_system="jira",
                        source_ref=issue_key,
                        source_external_id=issue_id,
                        display_name=f"Stale {issue_key}",
                        source_description="Already in engineering",
                        repo_url=None,
                        branch=None,
                        pr_url=None,
                        orchestration_backend="temporal",
                        dedupe_scope="parent_planning",
                        status=workflow_status,
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
                )
            session.commit()

            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="stale-active-parent-reconcile"),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            workflows = {
                workflow.source_ref: workflow
                for workflow in session.execute(
                    select(WorkflowExecution).where(WorkflowExecution.workflow_type_key == "parent_planning")
                ).scalars()
            }

        assert workflows["MAB-303"].status == "running"
        assert workflows["MAB-304"].status == "running"

    def test_release_ready_parent_source_does_not_create_planning_workflow(self) -> None:
        gateway = _FakeGateway(
            previews=[JiraIssuePreview(key="MAB-301", summary="Already release-ready parent", status="Ready to Release")],
            details={
                "MAB-301": JiraIssueDetail(
                    key="MAB-301",
                    summary="Already release-ready parent",
                    status="Ready to Release",
                    status_category_key="done",
                    description="Already past planning",
                    issue_type="Epic",
                    labels=[],
                    issue_id="30101",
                    parent_key=None,
                    parent_issue_id=None,
                )
            },
            fail_label_once_for=set(),
        )

        with self.session_factory() as session:
            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="release-ready-reconcile-no-create"),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            parent_workflows = session.execute(
                select(WorkflowExecution).where(WorkflowExecution.workflow_type_key == "parent_planning")
            ).scalars().all()

        assert parent_workflows == []

    def test_in_progress_parent_source_remains_visible_for_engineering_work(self) -> None:
        gateway = _FakeGateway(
            previews=[JiraIssuePreview(key="MAB-302", summary="Already in engineering", status="Testing")],
            details={
                "MAB-302": JiraIssueDetail(
                    key="MAB-302",
                    summary="Already in engineering",
                    status="Testing",
                    status_category_key="indeterminate",
                    description="Already past planning",
                    issue_type="Epic",
                    labels=[],
                    issue_id="30201",
                    parent_key=None,
                    parent_issue_id=None,
                )
            },
            fail_label_once_for=set(),
        )

        with self.session_factory() as session:
            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="in-progress-reconcile-no-create"),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            parent_workflows = session.execute(
                select(WorkflowExecution).where(WorkflowExecution.workflow_type_key == "parent_planning")
            ).scalars().all()

        assert [workflow.workflow_id for workflow in parent_workflows] == ["parent_planning:MAB-302"]
        assert parent_workflows[0].status == "running"

    def test_repeated_sync_rereads_current_jira_state_before_board_eligibility(self) -> None:
        gateway = _FakeGateway(
            previews=[JiraIssuePreview(key="MAB-400", summary="State changed parent", status="Backlog")],
            details={
                "MAB-400": JiraIssueDetail(
                    key="MAB-400",
                    summary="State changed parent",
                    status="Backlog",
                    description="Planning candidate",
                    issue_type="Epic",
                    labels=[],
                    issue_id="40001",
                    parent_key=None,
                    parent_issue_id=None,
                )
            },
            fail_label_once_for=set(),
        )

        with self.session_factory() as session:
            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="sync-before-status-change"),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            created_parent = session.get(WorkflowExecution, "parent_planning:MAB-400")
            assert created_parent is not None
            assert created_parent.status == "queued"

            gateway.details["MAB-400"] = JiraIssueDetail(
                key="MAB-400",
                summary="State changed parent",
                status="Ready to Release",
                status_category_key="done",
                description="No longer a planning candidate",
                issue_type="Epic",
                labels=["pm-parent"],
                issue_id="40001",
                parent_key=None,
                parent_issue_id=None,
            )
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="sync-after-status-change"),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            refreshed_parent = session.get(WorkflowExecution, "parent_planning:MAB-400")

        assert gateway.detail_calls == ["MAB-400", "MAB-400"]
        assert refreshed_parent.status == "cancelled"
        assert refreshed_parent.last_error == "Source item is no longer eligible for MB parent planning."

    def test_retry_after_label_failure_reuses_completed_scan_work_units_and_does_not_duplicate_parent_workflows(self) -> None:
        gateway = _FakeGateway(
            previews=[
                JiraIssuePreview(key="MAB-100", summary="Tenant auth redesign", status="Backlog"),
                JiraIssuePreview(key="MAB-101", summary="Build local auth verification", status="Backlog"),
            ],
            details={
                "MAB-100": JiraIssueDetail(
                    key="MAB-100",
                    summary="Tenant auth redesign",
                    status="Backlog",
                    description="Parent brief",
                    issue_type="Epic",
                    labels=["customer-facing", "engineering-child"],
                    issue_id="10001",
                    parent_key=None,
                    parent_issue_id=None,
                ),
                "MAB-101": JiraIssueDetail(
                    key="MAB-101",
                    summary="Build local auth verification",
                    status="Backlog",
                    description="Engineering story",
                    issue_type="Story",
                    labels=["pm-parent", "parent-mab-999"],
                    issue_id="10002",
                    parent_key="MAB-100",
                    parent_issue_id="10001",
                ),
            },
            fail_label_once_for={"MAB-101"},
        )

        with self.session_factory() as session:
            registry = self._handler_registry(gateway=gateway)
            workflow_type = get_workflow_type(session, workflow_type_key="jira_project_reconciliation")

            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=self._request(request_id="label-failure-1"),
                resolve_advance_handler_fn=registry.resolve_advance_handler,
            )
            session.commit()

            reconciliation_workflow = session.execute(
                select(WorkflowExecution).where(WorkflowExecution.workflow_type_key == "jira_project_reconciliation")
            ).scalar_one()
            failed_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == reconciliation_workflow.workflow_id,
                    WorkflowOperation.operation_type == "jira_label_reconciliation",
                )
            ).scalar_one()

            assert reconciliation_workflow.status == "failed"
            assert gateway.search_calls == [("MAB", None, 50)]
            assert gateway.detail_calls == ["MAB-100", "MAB-101"]
            assert gateway.label_call_counts == Counter({"MAB-100": 1, "MAB-101": 1})

            retry_workflow_operation_with_registered_handler(
                session=session,
                settings=SimpleNamespace(),
                session_factory=self.session_factory,
                workflow=reconciliation_workflow,
                operation=failed_operation,
                handler_registry=registry,
            )
            session.commit()

            parent_workflows = session.execute(
                select(WorkflowExecution).where(WorkflowExecution.workflow_type_key == "parent_planning")
            ).scalars().all()
            refreshed_workflow = session.get(WorkflowExecution, reconciliation_workflow.workflow_id)

        assert refreshed_workflow is not None
        assert refreshed_workflow.status == "completed"
        assert gateway.search_calls == [("MAB", None, 50)]
        assert gateway.detail_calls == ["MAB-100", "MAB-101"]
        assert gateway.label_call_counts == Counter({"MAB-100": 1, "MAB-101": 2})
        assert len(parent_workflows) == 1
