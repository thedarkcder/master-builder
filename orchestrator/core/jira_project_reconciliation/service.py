from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orchestrator.api.atlassian_oauth.connection_service import tenant_atlassian_oauth_context
from orchestrator.core.jira_project_reconciliation.models import (
    ClassifiedJiraIssue,
    ENGINEERING_CHILD_LABEL,
    ISSUE_CLASS_ENGINEERING_CHILD,
    ISSUE_CLASS_PARENT,
    JiraProjectReconciliationSummary,
    JiraReconciliationIssue,
    MB_WORK_STATE_NOT_PLANNING,
    MB_WORK_STATE_PLANNING_CANDIDATE,
    PM_PARENT_LABEL,
    PARENT_LABEL_PREFIX,
    ParentWorkflowReconciliationResult,
)
from orchestrator.core.jira_project_reconciliation.workflow import (
    JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION,
    JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION,
    JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION,
    JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
    JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY,
    JIRA_PROJECT_RECONCILIATION_WU_CLASSIFICATION_COMPUTE,
    JIRA_PROJECT_RECONCILIATION_WU_ISSUE_DETAIL_FETCH,
    JIRA_PROJECT_RECONCILIATION_WU_LABEL_REPLACE,
    JIRA_PROJECT_RECONCILIATION_WU_PAGE_FETCH,
    JIRA_PROJECT_RECONCILIATION_WU_PARENT_UPSERT,
    JIRA_PROJECT_RECONCILIATION_WU_SUMMARY_COMPUTE,
)
from orchestrator.core.workflow.execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    classify_external_workflow_failure,
    ensure_workflow_execution,
)
from orchestrator.core.workflow.operation_service import (
    WorkflowOperationAttemptAlreadyRunningError,
    WorkflowOperationHandle,
)
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.core.workflow.work_units import run_work_unit, workflow_work_unit_input_fingerprint
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation, WorkflowOperationWorkUnit
from orchestrator.tools.atlassian_oauth_models import (
    AtlassianOAuthAuthRequiredError,
    AtlassianOAuthError,
    AtlassianOAuthHttpError,
    JiraIssueDetail,
    JiraIssuePreview,
)


class JiraProjectReconciliationGateway(Protocol):
    def search_project_issues_page(self, *, project_key: str, start_at: int, max_results: int) -> list[JiraIssuePreview]:
        ...

    def get_issue_detail(self, *, issue_key: str) -> JiraIssueDetail:
        ...

    def replace_issue_labels(self, *, issue_key: str, labels: list[str]) -> None:
        ...


DEACTIVATABLE_PARENT_PLANNING_STATUSES = frozenset(
    {"queued", "pending", "running", "waiting_for_input", "failed", "retrying"}
)


class _AtlassianJiraProjectReconciliationGateway:
    def __init__(self, *, oauth_context) -> None:  # noqa: ANN001
        self._oauth_context = oauth_context

    @property
    def _client(self):  # noqa: ANN202
        return self._oauth_context.client

    @property
    def _access_token(self) -> str:
        return str(self._oauth_context.access_token or "").strip()

    @property
    def _cloud_id(self) -> str:
        return str(self._oauth_context.connection.cloud_id or "").strip()

    def search_project_issues_page(self, *, project_key: str, start_at: int, max_results: int) -> list[JiraIssuePreview]:
        normalized_project_key = _normalized_key(project_key)
        if not normalized_project_key:
            raise ValueError("Jira project reconciliation requires a project key")
        return self._client.search_issues_by_jql(
            access_token=self._access_token,
            cloud_id=self._cloud_id,
            jql=f"project = {normalized_project_key} ORDER BY created ASC",
            max_results=max_results,
            start_at=start_at,
        )

    def get_issue_detail(self, *, issue_key: str) -> JiraIssueDetail:
        normalized_issue_key = _normalized_key(issue_key)
        if not normalized_issue_key:
            raise ValueError("Jira project reconciliation requires an issue key for detail fetch")
        return self._client.get_issue_detail(
            access_token=self._access_token,
            cloud_id=self._cloud_id,
            issue_id_or_key=normalized_issue_key,
        )

    def replace_issue_labels(self, *, issue_key: str, labels: list[str]) -> None:
        self._client.replace_issue_labels(
            access_token=self._access_token,
            cloud_id=self._cloud_id,
            issue_id_or_key=_normalized_key(issue_key),
            labels=labels,
        )


def build_default_jira_project_reconciliation_gateway(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project,
    max_items: int,
) -> JiraProjectReconciliationGateway:
    _ = max_items, project
    oauth_context = tenant_atlassian_oauth_context(
        session=session,
        tenant=tenant,
        settings=settings,
    )
    return _AtlassianJiraProjectReconciliationGateway(oauth_context=oauth_context)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized(value: object) -> str:
    return str(value or "").strip()


def _normalized_key(value: object) -> str:
    return _normalized(value).upper()


def _normalized_label(value: object) -> str:
    return _normalized(value).casefold()


def _without_owned_labels(labels: tuple[str, ...] | list[str]) -> list[str]:
    retained: list[str] = []
    seen: set[str] = set()
    for raw_label in labels:
        label = _normalized(raw_label)
        normalized = _normalized_label(label)
        if not label:
            continue
        if normalized in {PM_PARENT_LABEL, ENGINEERING_CHILD_LABEL}:
            continue
        if normalized.startswith(PARENT_LABEL_PREFIX):
            continue
        if normalized in seen:
            continue
        retained.append(label)
        seen.add(normalized)
    return retained


def _labels_equal(left: tuple[str, ...] | list[str], right: tuple[str, ...] | list[str]) -> bool:
    return {_normalized_label(label) for label in left if _normalized(label)} == {
        _normalized_label(label) for label in right if _normalized(label)
    }


def _parent_label(parent_key: str) -> str:
    normalized_parent_key = _normalized_key(parent_key)
    if not normalized_parent_key:
        raise ValueError("Engineering Jira work item requires a parent issue key")
    return f"{PARENT_LABEL_PREFIX}{normalized_parent_key.lower()}"


def _classify_issue(issue: JiraReconciliationIssue) -> str:
    parent_key = _normalized_key(issue.parent_key)
    if parent_key:
        return ISSUE_CLASS_ENGINEERING_CHILD
    if issue.issue_type_is_subtask is True:
        raise ValueError(f"Jira issue {issue.key} is a subtask but is missing parent metadata")
    if issue.issue_type_hierarchy_level == -1:
        raise ValueError(f"Jira issue {issue.key} has subtask hierarchy without parent metadata")
    if _normalized(issue.issue_type):
        return ISSUE_CLASS_PARENT
    raise ValueError(f"Jira issue {issue.key} is missing hierarchy metadata required for classification")


def _mb_work_state_for_jira_detail(detail: JiraIssueDetail) -> str:
    status_name = _normalized(detail.status).casefold()
    status_category = _normalized(getattr(detail, "status_category_key", None)).casefold()
    if status_category:
        return MB_WORK_STATE_PLANNING_CANDIDATE if status_category == "new" else MB_WORK_STATE_NOT_PLANNING
    if status_name in {"backlog", "to do", "todo", "open"}:
        return MB_WORK_STATE_PLANNING_CANDIDATE
    return MB_WORK_STATE_NOT_PLANNING


def _desired_labels(issue: JiraReconciliationIssue, *, classification: str) -> tuple[str, ...]:
    retained = _without_owned_labels(issue.labels)
    if classification == ISSUE_CLASS_PARENT:
        return tuple([*retained, PM_PARENT_LABEL])
    if classification == ISSUE_CLASS_ENGINEERING_CHILD:
        return tuple([*retained, ENGINEERING_CHILD_LABEL, _parent_label(str(issue.parent_key or ""))])
    raise ValueError(f"Unsupported Jira reconciliation classification: {classification}")


def _issue_from_detail(detail: JiraIssueDetail) -> JiraReconciliationIssue:
    issue_id = _normalized(detail.issue_id)
    issue_key = _normalized_key(detail.key)
    if not issue_id:
        raise ValueError(f"Jira issue {issue_key or '<missing>'} is missing stable issue id")
    if not issue_key:
        raise ValueError("Jira issue detail is missing issue key")
    return JiraReconciliationIssue(
        issue_id=issue_id,
        key=issue_key,
        summary=_normalized(detail.summary),
        description=str(detail.description or ""),
        status=_normalized(detail.status),
        mb_work_state=_mb_work_state_for_jira_detail(detail),
        issue_type=_normalized(detail.issue_type) or None,
        labels=tuple(_normalized(label) for label in list(detail.labels or []) if _normalized(label)),
        parent_key=_normalized_key(detail.parent_key) or None,
        parent_issue_id=_normalized(detail.parent_issue_id) or None,
        issue_type_hierarchy_level=getattr(detail, "issue_type_hierarchy_level", None),
        issue_type_is_subtask=getattr(detail, "issue_type_is_subtask", None),
    )


def _step_failure_category(exc: Exception) -> str:
    if isinstance(exc, AtlassianOAuthAuthRequiredError):
        return "jira_auth_failure"
    if isinstance(exc, AtlassianOAuthHttpError):
        if exc.status_code == 429:
            return "jira_rate_api_failure"
        return "jira_rate_api_failure" if exc.status_code >= 500 else "jira_api_failure"
    if isinstance(exc, AtlassianOAuthError):
        return "invalid_jira_issue_payload"
    if isinstance(exc, IntegrityError):
        return "workflow_upsert_conflict"
    if isinstance(exc, ValueError):
        return "invalid_jira_issue_payload"
    return classify_external_workflow_failure(error=exc)


def _workflow_exists_by_key_or_external_id(
    *,
    session: Session,
    workflow_id: str,
    tenant_id: str,
    source_external_id: str,
    dedupe_scope: str,
) -> bool:
    existing = session.get(WorkflowExecution, workflow_id)
    if existing is not None:
        return True
    return (
        session.execute(
            select(WorkflowExecution.workflow_id)
            .where(
                WorkflowExecution.tenant_id == tenant_id,
                WorkflowExecution.source_system == "jira",
                WorkflowExecution.source_external_id == source_external_id,
                WorkflowExecution.dedupe_scope == dedupe_scope,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _parent_workflow_by_key_or_external_id(
    *,
    session: Session,
    workflow_id: str,
    tenant_id: str,
    source_external_id: str,
    dedupe_scope: str,
) -> WorkflowExecution | None:
    existing = session.get(WorkflowExecution, workflow_id)
    if existing is not None:
        return existing
    return (
        session.execute(
            select(WorkflowExecution)
            .where(
                WorkflowExecution.tenant_id == tenant_id,
                WorkflowExecution.source_system == "jira",
                WorkflowExecution.source_external_id == source_external_id,
                WorkflowExecution.dedupe_scope == dedupe_scope,
            )
            .order_by(WorkflowExecution.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()
    )


def _operation_by_type(*, session: Session, workflow_id: str, operation_type: str) -> WorkflowOperation:
    operation = session.execute(
        select(WorkflowOperation).where(
            WorkflowOperation.workflow_id == workflow_id,
            WorkflowOperation.operation_type == operation_type,
        )
    ).scalar_one_or_none()
    if operation is None:
        raise RuntimeError(f"Workflow {workflow_id} is missing operation {operation_type}")
    return operation


def _completed_unit_outputs(
    *,
    session: Session,
    operation_id: str,
    unit_key: str,
    idempotency_key_prefix: str | None = None,
) -> list[dict[str, object]]:
    query = select(WorkflowOperationWorkUnit).where(
        WorkflowOperationWorkUnit.operation_id == operation_id,
        WorkflowOperationWorkUnit.unit_key == unit_key,
        WorkflowOperationWorkUnit.status == "completed",
    )
    if idempotency_key_prefix is not None:
        query = query.where(WorkflowOperationWorkUnit.idempotency_key.like(f"{idempotency_key_prefix}%"))
    rows = session.execute(query.order_by(WorkflowOperationWorkUnit.created_at.asc())).scalars().all()
    payloads: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row.output_json, dict):
            raise RuntimeError(f"Completed work unit {row.unit_key} is missing persisted output")
        payloads.append(dict(row.output_json))
    return payloads


def _single_completed_unit_output(
    *,
    session: Session,
    operation_id: str,
    unit_key: str,
    idempotency_key_prefix: str | None = None,
) -> dict[str, object]:
    outputs = _completed_unit_outputs(
        session=session,
        operation_id=operation_id,
        unit_key=unit_key,
        idempotency_key_prefix=idempotency_key_prefix,
    )
    if len(outputs) != 1:
        raise RuntimeError(f"Workflow operation {operation_id} expected one completed output for {unit_key}, found {len(outputs)}")
    return outputs[0]


def latest_reconciliation_request_id_for_workflow(
    *,
    session: Session,
    workflow_id: str,
    project_id: str,
) -> str:
    prefix = f"{project_id}:reconciliation:"
    rows = session.execute(
        select(WorkflowOperationWorkUnit.idempotency_key)
        .join(WorkflowOperation, WorkflowOperation.operation_id == WorkflowOperationWorkUnit.operation_id)
        .where(
            WorkflowOperation.workflow_id == workflow_id,
            WorkflowOperationWorkUnit.idempotency_key.like(f"{prefix}%"),
        )
        .order_by(desc(WorkflowOperationWorkUnit.created_at))
    ).scalars()
    for idempotency_key in rows:
        remainder = str(idempotency_key or "")[len(prefix):]
        for marker in (":page:", ":issue:", ":classification:", ":labels:", ":parent:", ":summary:"):
            if marker in remainder:
                request_id = remainder.split(marker, 1)[0].strip()
                if request_id:
                    return request_id
    raise RuntimeError(f"Workflow {workflow_id} has no persisted Jira reconciliation request scope")


class JiraProjectReconciliationStepFailed(RuntimeError):
    pass


@dataclass(frozen=True)
class JiraProjectReconciliationRunResult:
    summary: JiraProjectReconciliationSummary


class JiraProjectReconciliationWorkflowService:
    def __init__(
        self,
        *,
        session: Session,
        settings,  # noqa: ANN001
        lifecycle,
        workflow_type,
        tenant: Tenant,
        project: Project,
        gateway: JiraProjectReconciliationGateway,
        max_items: int,
        request_id: str,
    ) -> None:
        self._session = session
        self._settings = settings
        self._lifecycle = lifecycle
        self._workflow_type = workflow_type
        self._tenant = tenant
        self._project = project
        self._gateway = gateway
        self._max_items = max(1, int(max_items))
        self._request_id = _normalized(request_id)
        if not self._request_id:
            raise ValueError("Jira project reconciliation requires request_id")
        self._idempotency_scope = f"{self._project.project_id}:reconciliation:{self._request_id}"

    def run(self, *, start_from: str | None = None) -> JiraProjectReconciliationRunResult:
        step_order = [
            JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
            JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION,
            JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION,
            JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION,
            JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY,
        ]
        start_index = step_order.index(start_from) if start_from else 0
        for step_key in step_order[start_index:]:
            if step_key == JIRA_PROJECT_RECONCILIATION_STEP_SCAN:
                self._execute_step(step_key, self._run_scan_step)
            elif step_key == JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION:
                self._execute_step(step_key, self._run_classification_step)
            elif step_key == JIRA_PROJECT_RECONCILIATION_STEP_LABEL_RECONCILIATION:
                self._execute_step(step_key, self._run_label_reconciliation_step)
            elif step_key == JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION:
                self._execute_step(step_key, self._run_parent_reconciliation_step)
            elif step_key == JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY:
                self._execute_step(step_key, self._run_summary_step)
        self._lifecycle.mark_completed_if_ready()
        return JiraProjectReconciliationRunResult(summary=self.summary())

    def retry_operation(self, *, operation_type: str) -> WorkflowOperationHandle:
        self.run(start_from=operation_type)
        operation = _operation_by_type(
            session=self._session,
            workflow_id=self.workflow.workflow_id,
            operation_type=operation_type,
        )
        return WorkflowOperationHandle(
            operation_id=operation.operation_id,
            workflow_id=operation.workflow_id,
            operation_type=operation.operation_type,
            status=operation.status,
        )

    @property
    def workflow(self) -> WorkflowExecution:
        workflow_id = f"{self._workflow_type.workflow_type_key}:{self._project.project_id}"
        workflow = self._session.get(WorkflowExecution, workflow_id)
        if workflow is None:
            raise RuntimeError(f"Workflow {workflow_id} was not created")
        return workflow

    def summary(self) -> JiraProjectReconciliationSummary:
        summary_operation = _operation_by_type(
            session=self._session,
            workflow_id=self.workflow.workflow_id,
            operation_type=JIRA_PROJECT_RECONCILIATION_STEP_SUMMARY,
        )
        return JiraProjectReconciliationSummary.from_payload(
            _single_completed_unit_output(
                session=self._session,
                operation_id=summary_operation.operation_id,
                unit_key=JIRA_PROJECT_RECONCILIATION_WU_SUMMARY_COMPUTE,
                idempotency_key_prefix=f"{self._idempotency_scope}:summary:",
            )
        )

    def _execute_step(self, operation_type: str, executor) -> None:  # noqa: ANN001
        try:
            operation, attempt = self._lifecycle.start_operation_attempt(operation_type=operation_type)
        except WorkflowOperationAttemptAlreadyRunningError as exc:
            raise JiraProjectReconciliationStepFailed(str(exc)) from exc
        try:
            summary = executor(operation=operation, attempt=attempt)
        except Exception as exc:
            self._lifecycle.fail_started_operation(
                operation=operation,
                attempt=attempt,
                category=_step_failure_category(exc),
                message=str(exc),
            )
            raise JiraProjectReconciliationStepFailed(str(exc)) from exc
        self._lifecycle.complete_started_operation(
            operation=operation,
            attempt=attempt,
            summary=summary,
        )

    def _run_scan_step(self, *, operation, attempt) -> str:  # noqa: ANN001
        loaded = 0
        page_size = min(50, self._max_items)
        start_at = 0
        while loaded < self._max_items:
            previews = run_work_unit(
                self._session,
                operation=operation,
                operation_attempt=attempt,
                unit_key=JIRA_PROJECT_RECONCILIATION_WU_PAGE_FETCH,
                idempotency_key=f"{self._idempotency_scope}:page:{start_at}:{page_size}",
                input_payload={
                    "request_id": self._request_id,
                    "project_id": self._project.project_id,
                    "project_key": self._project.jira_project_key,
                    "start_at": start_at,
                    "max_results": page_size,
                },
                execute=lambda _context: [
                    {
                        "key": preview.key,
                        "summary": preview.summary,
                        "status": preview.status,
                    }
                    for preview in self._gateway.search_project_issues_page(
                        project_key=self._project.jira_project_key,
                        start_at=start_at,
                        max_results=page_size,
                    )
                ],
                serialize=lambda result: {"items": list(result)},
                deserialize=lambda payload: list(payload.get("items") or []),
            )
            if not previews:
                break
            for preview in previews:
                issue_key = _normalized_key(preview.get("key"))
                if not issue_key:
                    raise ValueError("Jira preview payload is missing issue key")
                issue = run_work_unit(
                    self._session,
                    operation=operation,
                    operation_attempt=attempt,
                    unit_key=JIRA_PROJECT_RECONCILIATION_WU_ISSUE_DETAIL_FETCH,
                    idempotency_key=f"{self._idempotency_scope}:issue:{issue_key}",
                    input_payload={"request_id": self._request_id, "issue_key": issue_key},
                    execute=lambda _context, requested_issue_key=issue_key: _issue_from_detail(
                        self._gateway.get_issue_detail(issue_key=requested_issue_key)
                    ),
                    serialize=lambda result: result.to_payload(),
                    deserialize=lambda payload: JiraReconciliationIssue.from_payload(payload),
                )
                if issue.key != issue_key:
                    raise ValueError(f"Jira detail key mismatch: expected {issue_key}, got {issue.key}")
                loaded += 1
                if loaded >= self._max_items:
                    break
            if len(previews) < page_size:
                break
            start_at += len(previews)
        return f"Scanned {loaded} Jira issues for project {self._project.jira_project_key}."

    def _run_classification_step(self, *, operation, attempt) -> str:  # noqa: ANN001
        issues = self._scanned_issues()
        classified = run_work_unit(
            self._session,
            operation=operation,
            operation_attempt=attempt,
            unit_key=JIRA_PROJECT_RECONCILIATION_WU_CLASSIFICATION_COMPUTE,
            idempotency_key=f"{self._idempotency_scope}:classification:{workflow_work_unit_input_fingerprint([issue.to_payload() for issue in issues])}",
            input_payload={"request_id": self._request_id, "issues": [issue.to_payload() for issue in issues]},
            execute=lambda _context: [
                ClassifiedJiraIssue(
                    issue=issue,
                    classification=_classify_issue(issue),
                    desired_labels=_desired_labels(issue, classification=_classify_issue(issue)),
                    labels_changed=not _labels_equal(issue.labels, _desired_labels(issue, classification=_classify_issue(issue))),
                )
                for issue in issues
            ],
            serialize=lambda result: {"items": [item.to_payload() for item in result]},
            deserialize=lambda payload: [
                ClassifiedJiraIssue.from_payload(item)
                for item in list(payload.get("items") or [])
                if isinstance(item, dict)
            ],
        )
        parent_count = sum(1 for item in classified if item.classification == ISSUE_CLASS_PARENT)
        child_count = sum(1 for item in classified if item.classification == ISSUE_CLASS_ENGINEERING_CHILD)
        return f"Classified {parent_count} parent issues and {child_count} engineering child issues."

    def _run_label_reconciliation_step(self, *, operation, attempt) -> str:  # noqa: ANN001
        classified = self._classified_issues()
        labels_updated = 0
        for item in classified:
            if not item.labels_changed:
                continue
            run_work_unit(
                self._session,
                operation=operation,
                operation_attempt=attempt,
                unit_key=JIRA_PROJECT_RECONCILIATION_WU_LABEL_REPLACE,
                idempotency_key=f"{self._idempotency_scope}:labels:{item.issue.issue_id}:{workflow_work_unit_input_fingerprint(list(item.desired_labels))}",
                input_payload={"request_id": self._request_id, "issue_key": item.issue.key, "labels": list(item.desired_labels)},
                execute=lambda _context, issue_key=item.issue.key, labels=list(item.desired_labels): self._gateway.replace_issue_labels(
                    issue_key=issue_key,
                    labels=labels,
                )
                or {"issue_key": issue_key, "labels": labels},
                serialize=lambda result: dict(result),
                deserialize=lambda payload: dict(payload),
            )
            labels_updated += 1
        return f"Reconciled {labels_updated} Jira label updates."

    def _run_parent_reconciliation_step(self, *, operation, attempt) -> str:  # noqa: ANN001
        workflow_type = get_workflow_type(self._session, workflow_type_key="parent_planning")
        classified = [item for item in self._classified_issues() if item.classification == ISSUE_CLASS_PARENT]
        created = 0
        existing = 0
        deactivated = 0
        for item in classified:
            result = run_work_unit(
                self._session,
                operation=operation,
                operation_attempt=attempt,
                unit_key=JIRA_PROJECT_RECONCILIATION_WU_PARENT_UPSERT,
                idempotency_key=f"{self._idempotency_scope}:parent:{item.issue.issue_id}:{item.issue.mb_work_state}",
                input_payload={"request_id": self._request_id, "classified_issue": item.to_payload()},
                execute=lambda _context, classified_issue=item: self._reconcile_parent_workflow(
                    workflow_type=workflow_type,
                    classified_issue=classified_issue,
                ),
                serialize=lambda result: result.to_payload(),
                deserialize=lambda payload: ParentWorkflowReconciliationResult.from_payload(payload),
            )
            if result.created:
                created += 1
            elif result.deactivated:
                deactivated += 1
            else:
                existing += 1
        return f"Queued {created} parent workflows, matched {existing}, and removed {deactivated} non-planning parents."

    def _run_summary_step(self, *, operation, attempt) -> str:  # noqa: ANN001
        summary = run_work_unit(
            self._session,
            operation=operation,
            operation_attempt=attempt,
            unit_key=JIRA_PROJECT_RECONCILIATION_WU_SUMMARY_COMPUTE,
            idempotency_key=f"{self._idempotency_scope}:summary:{workflow_work_unit_input_fingerprint([item.to_payload() for item in self._classified_issues()])}",
            input_payload={
                "request_id": self._request_id,
                "classified": [item.to_payload() for item in self._classified_issues()],
                "parent_results": [result.to_payload() for result in self._parent_reconciliation_results()],
            },
            execute=lambda _context: self._build_summary(),
            serialize=lambda result: result.to_payload(),
            deserialize=lambda payload: JiraProjectReconciliationSummary.from_payload(payload),
        )
        return (
            f"Scanned {summary.scanned} Jira issues. "
            f"{summary.labels_updated} labels updated. "
            f"{summary.parent_workflows_created} parents queued."
        )

    def _reconcile_parent_workflow(
        self,
        *,
        workflow_type,
        classified_issue: ClassifiedJiraIssue,
    ) -> ParentWorkflowReconciliationResult:  # noqa: ANN001
        issue = classified_issue.issue
        if issue.mb_work_state != MB_WORK_STATE_PLANNING_CANDIDATE:
            return self._deactivate_parent_workflow_if_present(
                workflow_type=workflow_type,
                classified_issue=classified_issue,
            )
        return self._upsert_parent_workflow(workflow_type=workflow_type, classified_issue=classified_issue)

    def _upsert_parent_workflow(
        self,
        *,
        workflow_type,
        classified_issue: ClassifiedJiraIssue,
    ) -> ParentWorkflowReconciliationResult:  # noqa: ANN001
        issue = classified_issue.issue
        workflow_id = f"{workflow_type.workflow_type_key}:{issue.key}"
        existed = _workflow_exists_by_key_or_external_id(
            session=self._session,
            workflow_id=workflow_id,
            tenant_id=self._tenant.tenant_id,
            source_external_id=issue.issue_id,
            dedupe_scope=workflow_type.system_key,
        )
        projection = ensure_workflow_execution(
            session=self._session,
            workflow_type=workflow_type,
            tenant_id=self._tenant.tenant_id,
            project_id=self._project.project_id,
            execution=WorkflowExecutionReference(
                key=issue.key,
                source=WorkflowSourceReference(
                    source_system="jira",
                    source_ref=issue.key,
                    external_id=issue.issue_id,
                    display_name=issue.summary,
                    description=issue.description,
                    attributes={"jira_issue_labels": list(classified_issue.desired_labels)},
                ),
            ),
            display_name=issue.summary,
            description=issue.description,
        )
        if not existed:
            projection.workflow.status = "queued"
            projection.workflow.started_at = None
            projection.workflow.updated_at = _now()
        return ParentWorkflowReconciliationResult(
            issue_key=issue.key,
            workflow_id=projection.workflow.workflow_id,
            created=not existed,
            mb_work_state=issue.mb_work_state,
            deactivated=False,
        )

    def _deactivate_parent_workflow_if_present(
        self,
        *,
        workflow_type,
        classified_issue: ClassifiedJiraIssue,
    ) -> ParentWorkflowReconciliationResult:
        issue = classified_issue.issue
        workflow_id = f"{workflow_type.workflow_type_key}:{issue.key}"
        existing = _parent_workflow_by_key_or_external_id(
            session=self._session,
            workflow_id=workflow_id,
            tenant_id=self._tenant.tenant_id,
            source_external_id=issue.issue_id,
            dedupe_scope=workflow_type.system_key,
        )
        if existing is None:
            return ParentWorkflowReconciliationResult(
                issue_key=issue.key,
                workflow_id=workflow_id,
                created=False,
                mb_work_state=issue.mb_work_state,
                deactivated=False,
            )
        if str(existing.status or "").strip().lower() in DEACTIVATABLE_PARENT_PLANNING_STATUSES:
            existing.status = "cancelled"
            existing.last_error = "Source item is no longer eligible for MB parent planning."
            existing.finished_at = _now()
            existing.updated_at = _now()
            return ParentWorkflowReconciliationResult(
                issue_key=issue.key,
                workflow_id=existing.workflow_id,
                created=False,
                mb_work_state=issue.mb_work_state,
                deactivated=True,
            )
        return ParentWorkflowReconciliationResult(
            issue_key=issue.key,
            workflow_id=existing.workflow_id,
            created=False,
            mb_work_state=issue.mb_work_state,
            deactivated=False,
        )

    def _scanned_issues(self) -> list[JiraReconciliationIssue]:
        operation = _operation_by_type(
            session=self._session,
            workflow_id=self.workflow.workflow_id,
            operation_type=JIRA_PROJECT_RECONCILIATION_STEP_SCAN,
        )
        issues = [
            JiraReconciliationIssue.from_payload(payload)
            for payload in _completed_unit_outputs(
                session=self._session,
                operation_id=operation.operation_id,
                unit_key=JIRA_PROJECT_RECONCILIATION_WU_ISSUE_DETAIL_FETCH,
                idempotency_key_prefix=f"{self._idempotency_scope}:issue:",
            )
        ]
        return sorted(issues, key=lambda item: item.key)

    def _classified_issues(self) -> list[ClassifiedJiraIssue]:
        operation = _operation_by_type(
            session=self._session,
            workflow_id=self.workflow.workflow_id,
            operation_type=JIRA_PROJECT_RECONCILIATION_STEP_CLASSIFICATION,
        )
        payload = _single_completed_unit_output(
            session=self._session,
            operation_id=operation.operation_id,
            unit_key=JIRA_PROJECT_RECONCILIATION_WU_CLASSIFICATION_COMPUTE,
            idempotency_key_prefix=f"{self._idempotency_scope}:classification:",
        )
        items = [
            ClassifiedJiraIssue.from_payload(item)
            for item in list(payload.get("items") or [])
            if isinstance(item, dict)
        ]
        return sorted(items, key=lambda item: item.issue.key)

    def _parent_reconciliation_results(self) -> list[ParentWorkflowReconciliationResult]:
        operation = _operation_by_type(
            session=self._session,
            workflow_id=self.workflow.workflow_id,
            operation_type=JIRA_PROJECT_RECONCILIATION_STEP_PARENT_RECONCILIATION,
        )
        return [
            ParentWorkflowReconciliationResult.from_payload(payload)
            for payload in _completed_unit_outputs(
                session=self._session,
                operation_id=operation.operation_id,
                unit_key=JIRA_PROJECT_RECONCILIATION_WU_PARENT_UPSERT,
                idempotency_key_prefix=f"{self._idempotency_scope}:parent:",
            )
        ]

    def _build_summary(self) -> JiraProjectReconciliationSummary:
        classified = self._classified_issues()
        parent_results = self._parent_reconciliation_results()
        return JiraProjectReconciliationSummary(
            scanned=len(classified),
            parent_workflows_created=sum(1 for result in parent_results if result.created),
            parent_workflows_existing=sum(1 for result in parent_results if not result.created and not result.deactivated),
            labels_updated=sum(1 for item in classified if item.labels_changed),
            parent_issue_keys=tuple(
                item.issue.key for item in classified if item.classification == ISSUE_CLASS_PARENT
            ),
            engineering_issue_keys=tuple(
                item.issue.key for item in classified if item.classification == ISSUE_CLASS_ENGINEERING_CHILD
            ),
        )
