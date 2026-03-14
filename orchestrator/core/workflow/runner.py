from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections.abc import Callable
from typing import Protocol

from orchestrator.core.followups import build_backlog_follow_up_draft

@dataclass(frozen=True)
class WorkflowRequest:
    tenant_id: str
    run_id: str
    issue_key: str
    issue_summary: str
    issue_description: str
    max_dev_test_review_loops: int
    allow_pr_creation: bool = False
    suggested_test_commands: list[str] = field(default_factory=list)
    execution_repo_dir: str | None = None
    workspace_key: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    github_repository: str | None = None
    jira_project_key: str | None = None
    current_worker_capability: str = "linux"
    available_worker_capabilities: list[str] = field(default_factory=list)
    base_branch: str | None = None
    integration_branch: str | None = None
    pr_target_branch: str | None = None
    execution_branch: str | None = None
    start_point_ref: str | None = None
    start_point_sha: str | None = None
    pr_number: int | None = None
    trigger_context: dict | None = None
    resume_mode: str | None = None
    resume_stage: str | None = None
    resume_session_id: str | None = None
    resume_source_plan: dict | None = None
    human_inputs: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class PmPlan:
    plan_steps: list[str]
    acceptance_criteria: list[str]
    risks: list[str]
    next_stage: str = "dev"
    execution_worker_capability: str = "linux"
    missing_evidence_sources: list[str] = field(default_factory=list)
    confirmed_external_blockers: list[str] = field(default_factory=list)
    resolved_prerequisites: list[str] = field(default_factory=list)
    unresolved_prerequisites: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DevResult:
    change_summary: list[str]
    pr_url: str | None
    blocker_category: str | None = None
    blocker_message: str | None = None


@dataclass(frozen=True)
class TestResult:
    passed: bool
    guidance: list[str]
    feedback: str | None = None
    blocker_category: str | None = None
    blocker_message: str | None = None


# Prevent pytest from collecting this dataclass as a test class.
TestResult.__test__ = False


@dataclass(frozen=True)
class ReviewResult:
    approved: bool
    summary: list[str]
    outcome: str = "needs_changes"
    feedback: str | None = None
    pr_url: str | None = None
    blocker_category: str | None = None
    blocker_message: str | None = None


@dataclass(frozen=True)
class WorkflowDiagnostics:
    stage: str
    message: str
    attempts: int
    history: list[dict[str, str]]
    classification: str = "workflow_failure"


@dataclass(frozen=True)
class WorkflowResult:
    succeeded: bool
    plan: PmPlan | None
    pr_url: str | None
    summary: list[str]
    test_guidance: list[str]
    attempts: int
    dev_rationale: list[str] = field(default_factory=list)
    review_summary: list[str] = field(default_factory=list)
    review_feedback: str | None = None
    orchestration_stage_trace: list[dict[str, object]] = field(default_factory=list)
    orchestration_workstream_trace: list[dict[str, object]] = field(default_factory=list)
    follow_up_issue: dict | None = None
    diagnostics: WorkflowDiagnostics | None = None

    def to_plan_payload(self) -> dict:
        payload = {
            "attempts": self.attempts,
            "succeeded": self.succeeded,
            "summary": self.summary,
            "test_guidance": self.test_guidance,
            "pr_url": self.pr_url,
            "dev_rationale": self.dev_rationale,
            "review_summary": self.review_summary,
            "review_feedback": self.review_feedback,
            "orchestration_stage_trace": self.orchestration_stage_trace,
            "orchestration_workstream_trace": self.orchestration_workstream_trace,
            "follow_up_issue": self.follow_up_issue,
        }
        if self.orchestration_stage_trace or self.orchestration_workstream_trace:
            payload["orchestration_trace"] = {
                "stage_events": self.orchestration_stage_trace,
                "workstream_events": self.orchestration_workstream_trace,
            }
        if self.plan is not None:
            payload["plan"] = asdict(self.plan)
        if self.diagnostics is not None:
            payload["diagnostics"] = asdict(self.diagnostics)
        return payload


class WorkflowAgents(Protocol):
    def execute(
        self,
        request: WorkflowRequest,
        *,
        test_feedback_hook: Callable[[int, str], None] | None = None,
    ) -> WorkflowResult:
        ...


class WorkflowRunner:
    def __init__(
        self,
        agents: WorkflowAgents,
    ):
        self._agents = agents

    def run(
        self,
        request: WorkflowRequest,
        *,
        test_feedback_hook: Callable[[int, str], None] | None = None,
    ) -> WorkflowResult:
        try:
            return self._agents.execute(request, test_feedback_hook=test_feedback_hook)
        except Exception as exc:  # pragma: no cover - exercised via tests
            return self._failure(
                plan=None,
                stage="workflow",
                message=f"Workflow execution failed: {exc}",
                attempts=0,
                history=[],
                request=request,
            )

    def _failure(
        self,
        *,
        plan: PmPlan | None,
        stage: str,
        message: str,
        attempts: int,
        history: list[dict[str, str]],
        request: WorkflowRequest | None,
        follow_up_issue: dict | None = None,
        skip_auto_follow_up: bool = False,
        dev_rationale: list[str] | None = None,
        review_summary: list[str] | None = None,
        review_feedback: str | None = None,
        classification: str = "workflow_failure",
    ) -> WorkflowResult:
        if follow_up_issue is None and request is not None and not skip_auto_follow_up:
            draft = build_backlog_follow_up_draft(
                title=f"Follow-up for {request.issue_key}: {stage} handling",
                why_it_matters=message,
                impact=(
                    f"Workflow run {request.run_id} for {request.issue_key} ended in stage '{stage}'"
                ),
                suggested_approach=(
                    "Address the reported stage failure and rerun from To Do after validation."
                ),
                origin_issue_key=request.issue_key,
            )
            follow_up_issue = draft.to_payload()
        return WorkflowResult(
            succeeded=False,
            plan=plan,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=attempts,
            dev_rationale=list(dev_rationale or ()),
            review_summary=list(review_summary or ()),
            review_feedback=review_feedback,
            follow_up_issue=follow_up_issue,
            diagnostics=WorkflowDiagnostics(
                stage=stage,
                message=message,
                attempts=attempts,
                history=history,
                classification=classification,
            ),
        )
