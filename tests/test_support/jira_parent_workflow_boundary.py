from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.observability.repository import (
    configure_product_event_repository_for_tests,
    reset_product_event_repository_for_tests,
)
from orchestrator.tools.atlassian_oauth import JiraIssueCreateInput, JiraIssueCreateResult, JiraIssueDetail, JiraIssuePreview
from tests.test_support.product_events import RecordingProductEventRepository


@dataclass(frozen=True)
class _IssueRecord:
    key: str
    summary: str
    description: str | dict[str, object]
    status: str
    issue_type: str
    labels: list[str]
    parent_issue_key: str | None = None


class BoundaryTelemetryProbe:
    def __init__(self, repository: RecordingProductEventRepository) -> None:
        self._repository = repository

    def contains_kind(self, event_kind: str) -> bool:
        return any(row.event_kind == event_kind for row in self._repository.inserted)

    def all_operation_events_have_attempt_ids(self) -> bool:
        return all(not row.operation_id or bool(row.attempt_id) for row in self._repository.inserted)


class BoundaryRuntime:
    command = "boundary-runtime"
    model = "boundary-model"

    def __init__(self, responses: list[dict[str, object]]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def run_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        working_dir: str | None = None,
        on_log_line=None,  # noqa: ANN001
        reasoning_effort: str | None = None,
        model_override: str | None = None,
        resume_session_id: str | None = None,
        on_session_id=None,  # noqa: ANN001
        on_usage=None,  # noqa: ANN001
    ) -> dict[str, object]:
        call_number = len(self.calls) + 1
        self.calls.append(
            {
                "system_prompt": system_prompt,
                "user_prompt": user_prompt,
                "working_dir": working_dir,
                "reasoning_effort": reasoning_effort,
                "model_override": model_override,
                "resume_session_id": resume_session_id,
            }
        )
        if on_session_id is not None:
            on_session_id(f"boundary-session-{call_number}")
        if on_usage is not None:
            on_usage({"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20})
        if on_log_line is not None:
            on_log_line("stdout", f"boundary runtime call {call_number} started")
            on_log_line("stdout", '{"type":"turn.completed","input_tokens":12,"output_tokens":8}')
        if not self._responses:
            raise AssertionError("Boundary runtime received more calls than expected")
        return self._responses.pop(0)


class BoundaryJiraClient:
    def __init__(
        self,
        *,
        issue_key: str,
        issue_summary: str,
        issue_description: str,
    ) -> None:
        self.parent_issue_key = issue_key
        self._next_issue_number = int(issue_key.rsplit("-", 1)[1]) + 1
        self.issues: dict[str, _IssueRecord] = {
            issue_key: _IssueRecord(
                key=issue_key,
                summary=issue_summary,
                description=issue_description,
                status="Backlog",
                issue_type="Epic",
                labels=["pm-parent"],
            )
        }
        self.created_issue_keys: list[str] = []
        self.updated_parent_labels: list[list[str]] = []
        self.remote_links: list[tuple[str, str]] = []
        self.comments: list[tuple[str, str | dict[str, object]]] = []

    def get_issue_detail(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
    ) -> JiraIssueDetail:
        del access_token, cloud_id
        record = self.issues[issue_id_or_key]
        return JiraIssueDetail(
            key=record.key,
            summary=record.summary,
            status=record.status,
            description=str(record.description),
            status_category_key="indeterminate",
            issue_type=record.issue_type,
            labels=list(record.labels),
        )

    def add_issue_labels(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        labels: list[str],
    ) -> None:
        del access_token, cloud_id
        record = self.issues[issue_id_or_key]
        merged = [*record.labels, *[label for label in labels if label not in record.labels]]
        self.issues[issue_id_or_key] = _replace_issue(record, labels=merged)

    def list_project_issue_types_for_create(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
    ) -> list[str]:
        del access_token, cloud_id, project_key
        return ["Epic", "Story", "Sub-task"]

    def search_issues_by_jql(
        self,
        *,
        access_token: str,
        cloud_id: str,
        jql: str,
        max_results: int = 20,
        start_at: int = 0,
    ) -> list[JiraIssuePreview]:
        del access_token, cloud_id, max_results, start_at
        normalized_jql = jql.casefold()
        if "parent =" in normalized_jql or "labels =" in normalized_jql:
            parent_key = _quoted_value(jql, "parent")
            if parent_key is None and f"parent-{self.parent_issue_key.casefold()}" in normalized_jql:
                parent_key = self.parent_issue_key
            return [
                JiraIssuePreview(key=record.key, summary=record.summary, status=record.status)
                for record in self.issues.values()
                if record.parent_issue_key == parent_key
            ]
        return [
            JiraIssuePreview(key=record.key, summary=record.summary, status=record.status)
            for record in self.issues.values()
        ]

    def create_issue(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
        issue: JiraIssueCreateInput,
    ) -> JiraIssueCreateResult:
        del access_token, cloud_id
        issue_key = f"{project_key}-{self._next_issue_number}"
        self._next_issue_number += 1
        self.created_issue_keys.append(issue_key)
        self.issues[issue_key] = _IssueRecord(
            key=issue_key,
            summary=issue.summary,
            description=issue.description,
            status="Backlog",
            issue_type=issue.issue_type,
            labels=list(issue.labels),
            parent_issue_key=issue.parent_issue_key,
        )
        return JiraIssueCreateResult(key=issue_key, issue_id=f"id-{issue_key}")

    def update_issue_fields(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
        description: str | dict[str, object],
        labels: list[str],
    ) -> None:
        del access_token, cloud_id
        record = self.issues[issue_id_or_key]
        self.issues[issue_id_or_key] = _replace_issue(
            record,
            summary=summary,
            description=description,
            labels=list(labels),
        )
        if issue_id_or_key == self.parent_issue_key:
            self.updated_parent_labels.append(list(labels))

    def update_issue_summary(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
    ) -> None:
        del access_token, cloud_id
        record = self.issues[issue_id_or_key]
        self.issues[issue_id_or_key] = _replace_issue(record, summary=summary)

    def replace_issue_labels(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        labels: list[str],
    ) -> None:
        del access_token, cloud_id
        record = self.issues[issue_id_or_key]
        self.issues[issue_id_or_key] = _replace_issue(record, labels=list(labels))
        if issue_id_or_key == self.parent_issue_key:
            self.updated_parent_labels.append(list(labels))

    def upsert_remote_issue_link(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        global_id: str,
        relationship: str,
        title: str,
        url: str,
    ) -> dict[str, object]:
        del access_token, cloud_id, relationship, url
        self.remote_links.append((issue_id_or_key, title or global_id))
        return {}

    def add_issue_comment(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        comment: str | dict[str, object],
    ) -> dict[str, object]:
        del access_token, cloud_id
        self.comments.append((issue_id_or_key, comment))
        return {"id": f"comment-{issue_id_or_key}-{len(self.comments)}", "body": comment}

    def transition_issue(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        target_status: str,
    ) -> dict[str, object]:
        del access_token, cloud_id
        record = self.issues[issue_id_or_key]
        self.issues[issue_id_or_key] = _replace_issue(record, status=target_status)
        return {}


class JiraParentWorkflowBoundaryHarness:
    def __init__(
        self,
        *,
        tenant_id: str,
        issue_key: str,
        issue_summary: str,
        issue_description: str,
    ) -> None:
        self.tenant_id = tenant_id
        self.issue_key = issue_key
        self.jira = BoundaryJiraClient(
            issue_key=issue_key,
            issue_summary=issue_summary,
            issue_description=issue_description,
        )
        self.runtime = BoundaryRuntime(_runtime_responses(issue_key=issue_key, issue_summary=issue_summary))
        self.event_repository = RecordingProductEventRepository()
        self.telemetry = BoundaryTelemetryProbe(self.event_repository)

    @contextmanager
    def installed(self):
        oauth_object = SimpleNamespace(
            client=self.jira,
            access_token="boundary-token",
            connection=SimpleNamespace(
                cloud_id="boundary-cloud",
                site_url="https://example.atlassian.net",
            ),
        )
        with ExitStack() as stack:
            configure_product_event_repository_for_tests(self.event_repository)
            stack.callback(reset_product_event_repository_for_tests)
            for target in (
                "orchestrator.api.webhooks.jira_admission_flow.tenant_atlassian_oauth_context",
                "orchestrator.api.webhooks.jira_application.tenant_atlassian_oauth_context",
                "orchestrator.core.worker.webhook_job_service.tenant_atlassian_oauth_context",
                "orchestrator.api.webhooks.jira_parent_child_sync.tenant_atlassian_oauth_context",
            ):
                stack.enter_context(patch(target, return_value=oauth_object))
            stack.enter_context(
                patch("orchestrator.runtime.issue_fanout.tenant_atlassian_oauth_context", return_value=oauth_object)
            )
            stack.enter_context(
                patch("orchestrator.api.webhooks.contracts.tenant_atlassian_oauth_context", return_value=oauth_object)
            )
            stack.enter_context(
                patch("orchestrator.api.webhooks.jira_parent_child_sync.build_runtime_for_selector", return_value=self.runtime)
            )
            stack.enter_context(
                patch("orchestrator.runtime.issue_fanout.build_runtime_for_selector", return_value=self.runtime)
            )
            yield self


def _replace_issue(record: _IssueRecord, **changes) -> _IssueRecord:  # noqa: ANN003
    values = {
        "key": record.key,
        "summary": record.summary,
        "description": record.description,
        "status": record.status,
        "issue_type": record.issue_type,
        "labels": record.labels,
        "parent_issue_key": record.parent_issue_key,
    }
    values.update(changes)
    return _IssueRecord(**values)


def _quoted_value(jql: str, field_name: str) -> str | None:
    marker = f'{field_name} = "'
    start = jql.find(marker)
    if start == -1:
        return None
    start += len(marker)
    end = jql.find('"', start)
    if end == -1:
        return None
    return jql[start:end]


def _child_spec() -> dict[str, object]:
    return {
        "summary": "Implement workflow boundary harness",
        "capability": "Workflow boundary verification",
        "delivery": "Build a boundary harness that proves the webhook workflow path.",
        "expected_outcome": "Parent planning regressions fail at the behavior boundary.",
        "acceptance_criteria": ["Webhook processing creates durable attempts and Jira children."],
        "how_to_test": ["Run workflow boundary path tests."],
        "done_means": ["The boundary test proves attempt-scoped telemetry and Jira fanout."],
        "dependencies": [],
        "risks": [],
        "labels": ["engineering-child"],
    }


def _runtime_responses(*, issue_key: str, issue_summary: str) -> list[dict[str, object]]:
    child_spec = _child_spec()
    technical_decision = {
        "decision_id": "workflow-boundary-harness",
        "area": "architecture",
        "question": "How should workflow boundary behavior be validated?",
        "options": [
            {
                "option_id": "boundary-harness",
                "title": "Boundary harness",
                "description": "Exercise webhook, workflow, attempts, telemetry, and Jira fanout together.",
                "benefits": ["Catches production wiring regressions"],
                "risks": ["Requires controlled fake integrations"],
                "rejected_reason": "",
            }
        ],
        "selected_option_id": "boundary-harness",
        "rationale": "The boundary harness validates the real behavior path without patch-heavy seams.",
        "evidence": ["Parent planning needs durable workflow coverage."],
        "confidence": "high",
        "product_impact": "none",
    }
    return [
        {
            "brief": {
                "objective": issue_summary,
                "user_value": "Operators can trust parent planning execution state.",
                "acceptance_criteria": ["Workflow attempts and telemetry are persisted."],
                "scope_in": ["Webhook to workflow to fanout boundary"],
                "scope_out": ["External Atlassian network access"],
                "constraints": ["No internal workflow methods are patched."],
                "risks": ["Patch-heavy tests can miss production wiring regressions."],
                "success_outcomes": ["A Jira parent feature creates a completed workflow with a child ticket."],
                "recommendation": "Create one engineering child story.",
                "open_questions": [],
                "next_steps": ["Seed the child ticket."],
            },
            "open_questions": [],
        },
        {
            "findings": ["Parent planning needs boundary coverage."],
            "recommendations": ["Create one executable child story."],
            "required_tasks": ["Implement workflow boundary harness"],
            "child_ticket_specs": [child_spec],
            "technical_decisions": [technical_decision],
            "pm_decision_requests": [],
            "acceptance_impacts": ["Durable workflow behavior is regression-tested."],
            "mermaid_diagram": "flowchart TD\n  Webhook --> Workflow --> Fanout",
        },
        {
            "findings": ["No new security boundary is introduced."],
            "recommendations": ["Keep tenant-scoped workflow records verified."],
            "required_tasks": ["Assert attempt ownership for telemetry."],
            "technical_decisions": [technical_decision],
            "pm_decision_requests": [],
            "acceptance_impacts": ["Telemetry rows carry attempt identity."],
        },
        {
            "findings": ["The acceptance path requires end-to-end verification."],
            "recommendations": ["Exercise real webhook worker and persistence boundaries."],
            "required_tasks": ["Add boundary test coverage."],
            "technical_decisions": [technical_decision],
            "pm_decision_requests": [],
            "acceptance_impacts": ["Regressions are caught at the behavior boundary."],
        },
        {
            "project_key": "TP",
            "parent_issue": {
                "summary": issue_summary,
                "issue_type": "Epic",
                "objective": issue_summary,
                "user_value": "Operators can trust parent planning execution state.",
                "recommendation": "Create one engineering child story.",
                "scope_in": ["Webhook to workflow to fanout boundary"],
                "scope_out": ["External Atlassian network access"],
                "acceptance_criteria": ["Workflow attempts and telemetry are persisted."],
                "ui_references": [],
                "success_outcomes": ["A Jira parent feature creates a completed workflow with a child ticket."],
                "dependencies": [],
                "risks": ["Patch-heavy tests can miss production wiring regressions."],
                "open_questions": [],
                "labels": ["pm-parent"],
                "issue_key": issue_key,
            },
            "engineering_children": [
                {
                    **child_spec,
                    "issue_type": "Story",
                }
            ],
            "questions": [],
        },
    ]
