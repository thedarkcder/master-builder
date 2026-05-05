from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from orchestrator.core.jira_project_reconciliation.dependencies import JiraProjectReconciliationHandlerDeps
from orchestrator.core.jira_project_reconciliation.handlers import JiraProjectReconciliationAdvanceHandler
from orchestrator.core.jira_project_reconciliation.retry import JiraProjectReconciliationOperationRetryHandler
from orchestrator.core.workflow.advance import WorkflowAdvanceRequest, WorkflowTrigger, execute_workflow_advance
from orchestrator.core.workflow.execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow.handler_registry import build_workflow_handler_registry
from orchestrator.core.workflow.operation_retry_use_case import retry_workflow_operation_with_registered_handler
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt
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
        self.search_calls: list[tuple[str, int, int]] = []
        self.detail_calls: list[str] = []
        self.label_replacements: list[tuple[str, list[str]]] = []
        self.label_call_counts: Counter[str] = Counter()

    def search_project_issues_page(self, *, project_key: str, start_at: int, max_results: int) -> list[JiraIssuePreview]:
        self.search_calls.append((project_key, start_at, max_results))
        return list(self.previews[start_at:start_at + max_results])

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

        assert result.handled is True
        assert reconciliation_workflow.status == "completed"
        assert [workflow.workflow_id for workflow in parent_workflows] == ["parent_planning:MAB-100"]
        assert parent_workflows[0].source_external_id == "10001"
        assert parent_workflows[0].status == "queued"
        assert parent_workflows[0].started_at is None
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
            assert gateway.search_calls == [("MAB", 0, 50)]
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
        assert gateway.search_calls == [("MAB", 0, 50)]
        assert gateway.detail_calls == ["MAB-100", "MAB-101"]
        assert gateway.label_call_counts == Counter({"MAB-100": 1, "MAB-101": 2})
        assert len(parent_workflows) == 1
