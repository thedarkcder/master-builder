from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from orchestrator.core.decision.types import PrecheckOutcome
from orchestrator.core.runs.enqueue_types import EnqueueFailureReason
from orchestrator.core.runs.service import EnqueueRunResult, enqueue_run
from orchestrator.core.workflow.operation_service import (
    OPERATION_STATUS_COMPLETED,
    complete_workflow_operation,
    fail_workflow_operation,
    start_workflow_operation_attempt,
    upsert_workflow_operation,
)
from orchestrator.storage.models import Project, Run, Tenant, WorkflowExecution, WorkflowOperation
from orchestrator.tools.atlassian_oauth import JiraIssueDetail

DEVELOPMENT_START_OPERATION = "development_start"
_ACTIONABLE_STATUSES = {"to do", "ready for agent"}
_BLOCKING_LABELS = {"sync-blocked", "sync-stale"}


class StartWorkIssueGateway(Protocol):
    def load_issue_detail(self, issue_key: str) -> JiraIssueDetail:
        ...

    def load_child_details(self, *, project_key: str, parent_issue_key: str) -> list[JiraIssueDetail]:
        ...

    def transition_issue(self, *, issue_key: str, target_status: str) -> None:
        ...


@dataclass(frozen=True)
class StartWorkIssueResult:
    issue_key: str
    run_id: str | None
    status: str
    reason: str | None = None


@dataclass(frozen=True)
class StartWorkResult:
    source_issue_key: str
    queued: tuple[StartWorkIssueResult, ...]
    skipped: tuple[StartWorkIssueResult, ...]
    promoted_issue_keys: tuple[str, ...]
    operation_id: str | None = None
    attempt_id: str | None = None


def _normalized(value: object) -> str:
    return str(value or "").strip()


def _normalized_key(value: object) -> str:
    return _normalized(value).upper()


def _normalized_status(value: object) -> str:
    return _normalized(value).casefold()


def _labels(issue: JiraIssueDetail) -> set[str]:
    return {str(label or "").strip().casefold() for label in issue.labels if str(label or "").strip()}


def _project_key_for_issue(issue_key: str) -> str:
    normalized = _normalized_key(issue_key)
    if "-" not in normalized:
        return normalized
    return normalized.split("-", 1)[0]


def _is_clean_engineering_issue(issue: JiraIssueDetail) -> bool:
    labels = _labels(issue)
    return "engineering-child" in labels and labels.isdisjoint(_BLOCKING_LABELS)


def _is_pm_parent(issue: JiraIssueDetail) -> bool:
    return "pm-parent" in _labels(issue)


class StartWorkUseCase:
    def __init__(
        self,
        *,
        session: Session,
        issue_gateway: StartWorkIssueGateway,
        enqueue_run_fn=enqueue_run,  # noqa: ANN001
    ) -> None:
        self._session = session
        self._issue_gateway = issue_gateway
        self._enqueue_run_fn = enqueue_run_fn

    def start(
        self,
        *,
        tenant: Tenant,
        project: Project,
        issue_key: str,
        target_status: str,
        actor: str,
        reason: str,
        source_workflow_id: str | None = None,
        require_source_workflow_completed: bool = True,
        promote_targets: bool = True,
    ) -> StartWorkResult:
        source_issue_key = _normalized_key(issue_key)
        if not source_issue_key:
            raise ValueError("Start work requires a Jira issue key")
        if tenant is None:
            raise ValueError("Start work requires a tenant")
        if project is None:
            raise ValueError("Start work requires a project")
        if project.tenant_id != tenant.tenant_id:
            raise ValueError("Start work project must belong to the supplied tenant")

        source_workflow = self._source_workflow(
            source_workflow_id=source_workflow_id,
            tenant=tenant,
            issue_key=source_issue_key,
            require_completed=require_source_workflow_completed,
        )
        source_issue = self._issue_gateway.load_issue_detail(source_issue_key)
        child_issues = self._issue_gateway.load_child_details(
            project_key=_project_key_for_issue(source_issue_key),
            parent_issue_key=source_issue_key,
        )
        targets = self._execution_targets(source_issue=source_issue, child_issues=child_issues)
        if not targets:
            raise ValueError(f"No executable engineering work found for {source_issue_key}")

        operation = self._operation_for_source_workflow(
            source_workflow=source_workflow,
            source_issue_key=source_issue_key,
            actor=actor,
            reason=reason,
        )
        if operation is not None and str(operation.status or "").strip().lower() == OPERATION_STATUS_COMPLETED:
            return self._start_targets(
                source_issue_key=source_issue_key,
                targets=targets,
                tenant=tenant,
                project=project,
                target_status=target_status,
                operation=operation,
                attempt_id=None,
                promote_targets=promote_targets,
            )

        attempt = start_workflow_operation_attempt(self._session, operation=operation) if operation is not None else None
        try:
            result = self._start_targets(
                source_issue_key=source_issue_key,
                targets=targets,
                tenant=tenant,
                project=project,
                target_status=target_status,
                operation=operation,
                attempt_id=attempt.attempt_id if attempt is not None else None,
                promote_targets=promote_targets,
            )
            if operation is not None and attempt is not None:
                complete_workflow_operation(
                    self._session,
                    operation=operation,
                    attempt=attempt,
                    summary=self._summary(result=result),
                )
                self._session.commit()
            return result
        except Exception as exc:
            if operation is not None and attempt is not None:
                fail_workflow_operation(
                    self._session,
                    operation=operation,
                    attempt=attempt,
                    category="start_work_failed",
                    message=str(exc),
                )
                self._session.commit()
            raise

    def _source_workflow(
        self,
        *,
        source_workflow_id: str | None,
        tenant: Tenant,
        issue_key: str,
        require_completed: bool,
    ) -> WorkflowExecution | None:
        normalized_workflow_id = _normalized(source_workflow_id)
        if not normalized_workflow_id:
            return None
        workflow = self._session.get(WorkflowExecution, normalized_workflow_id)
        if workflow is None:
            raise ValueError(f"Source workflow not found: {normalized_workflow_id}")
        if workflow.tenant_id != tenant.tenant_id or _normalized_key(workflow.source_ref) != issue_key:
            raise ValueError("Source workflow does not match the supplied tenant and issue")
        if str(workflow.workflow_type_key or "").strip() != "parent_planning":
            raise ValueError("Start work source workflow must be a parent planning workflow")
        if require_completed and str(workflow.status or "").strip().lower() != "completed":
            raise ValueError("Parent planning must be completed before development can start")
        return workflow

    def _operation_for_source_workflow(
        self,
        *,
        source_workflow: WorkflowExecution | None,
        source_issue_key: str,
        actor: str,
        reason: str,
    ) -> WorkflowOperation | None:
        if source_workflow is None:
            return None
        operation = upsert_workflow_operation(
            self._session,
            workflow_id=source_workflow.workflow_id,
            operation_type=DEVELOPMENT_START_OPERATION,
            idempotency_key=f"{source_workflow.workflow_id}:{DEVELOPMENT_START_OPERATION}:{source_issue_key}",
            target_system="jira",
            target_ref=source_issue_key,
            summary=f"Start development requested by {actor}: {reason}",
        )
        self._session.flush()
        return operation

    def _execution_targets(
        self,
        *,
        source_issue: JiraIssueDetail,
        child_issues: list[JiraIssueDetail],
    ) -> tuple[JiraIssueDetail, ...]:
        if _is_pm_parent(source_issue):
            return tuple(issue for issue in child_issues if _is_clean_engineering_issue(issue))

        executable_children = tuple(issue for issue in child_issues if _is_clean_engineering_issue(issue))
        if executable_children:
            return executable_children
        if _is_clean_engineering_issue(source_issue):
            return (source_issue,)
        return ()

    def _start_targets(
        self,
        *,
        source_issue_key: str,
        targets: tuple[JiraIssueDetail, ...],
        tenant: Tenant,
        project: Project,
        target_status: str,
        operation: WorkflowOperation | None,
        attempt_id: str | None,
        promote_targets: bool,
    ) -> StartWorkResult:
        queued: list[StartWorkIssueResult] = []
        skipped: list[StartWorkIssueResult] = []
        promoted: list[str] = []
        normalized_target_status = _normalized(target_status) or "To Do"
        for issue in targets:
            if promote_targets and _normalized_status(issue.status) not in _ACTIONABLE_STATUSES:
                self._issue_gateway.transition_issue(issue_key=issue.key, target_status=normalized_target_status)
                promoted.append(issue.key)
            existing_run = self._existing_run_for_issue(tenant=tenant, issue=issue)
            if existing_run is not None:
                skipped.append(
                    StartWorkIssueResult(
                        issue_key=issue.key,
                        run_id=existing_run.run_id,
                        status=existing_run.status,
                        reason="already_started",
                    )
                )
                continue
            enqueue_result = self._enqueue_issue_run(tenant=tenant, project=project, issue=issue)
            if enqueue_result.enqueued:
                queued.append(
                    StartWorkIssueResult(
                        issue_key=issue.key,
                        run_id=enqueue_result.run.run_id,
                        status=enqueue_result.run.status,
                    )
                )
                continue
            skipped.append(
                StartWorkIssueResult(
                    issue_key=issue.key,
                    run_id=enqueue_result.run.run_id,
                    status=enqueue_result.run.status,
                    reason=_skip_reason(enqueue_result.reason),
                )
            )
        return StartWorkResult(
            source_issue_key=source_issue_key,
            queued=tuple(queued),
            skipped=tuple(skipped),
            promoted_issue_keys=tuple(promoted),
            operation_id=operation.operation_id if operation is not None else None,
            attempt_id=attempt_id,
        )

    def _enqueue_issue_run(self, *, tenant: Tenant, project: Project, issue: JiraIssueDetail) -> EnqueueRunResult:
        return self._enqueue_run_fn(
            self._session,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key=issue.key,
            issue_summary=issue.summary,
            issue_description=issue.description,
            repo_url=project.github_repository,
            delivery_id=None,
            precheck_outcome=PrecheckOutcome.READY_FOR_AGENT.value,
            required_worker_capability=None,
            max_concurrent_runs=None,
        )

    def _existing_run_for_issue(self, *, tenant: Tenant, issue: JiraIssueDetail) -> Run | None:
        workflow = self._session.execute(
            select(WorkflowExecution)
            .where(
                WorkflowExecution.tenant_id == tenant.tenant_id,
                WorkflowExecution.source_system == "jira",
                WorkflowExecution.source_ref == _normalized_key(issue.key),
                WorkflowExecution.dedupe_scope == "issue_execution",
            )
            .order_by(desc(WorkflowExecution.created_at))
            .limit(1)
        ).scalar_one_or_none()
        if workflow is None:
            return None
        return self._session.execute(
            select(Run)
            .where(Run.workflow_id == workflow.workflow_id)
            .order_by(desc(Run.attempt_number))
            .limit(1)
        ).scalar_one_or_none()

    @staticmethod
    def _summary(*, result: StartWorkResult) -> str:
        queued_count = len(result.queued)
        skipped_count = len(result.skipped)
        promoted_count = len(result.promoted_issue_keys)
        return (
            f"Started development for {queued_count} issue{'s' if queued_count != 1 else ''}; "
            f"{skipped_count} already active or skipped; {promoted_count} promoted."
        )


def _skip_reason(reason: EnqueueFailureReason | None) -> str:
    if reason is EnqueueFailureReason.RUN_ALREADY_ACTIVE:
        return "already_active"
    if reason is None:
        return "not_enqueued"
    return reason.value


def latest_start_work_operation(*, session: Session, workflow_id: str) -> WorkflowOperation | None:
    return session.execute(
        select(WorkflowOperation)
        .where(
            WorkflowOperation.workflow_id == workflow_id,
            WorkflowOperation.operation_type == DEVELOPMENT_START_OPERATION,
        )
        .limit(1)
    ).scalar_one_or_none()
