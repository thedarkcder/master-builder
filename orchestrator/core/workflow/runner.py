from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Literal, Protocol

from orchestrator.core.followups import build_backlog_follow_up_draft
from orchestrator.core.worker_capability_normalization import WorkerCapability

StageOutcome = Literal["continue", "requeue", "waiting_for_input", "blocked", "failed"]
WorkflowOutcome = Literal["success", "requeue", "waiting_for_input", "blocked", "failed"]


@dataclass(frozen=True)
class WorkflowRequest:
    tenant_id: str
    run_id: str
    issue_key: str
    issue_summary: str
    issue_description: str
    max_dev_test_review_loops: int
    workflow_id: str | None = None
    attempt_number: int = 1
    allow_pr_creation: bool = False
    suggested_test_commands: list[str] = field(default_factory=list)
    execution_repo_dir: str | None = None
    workspace_key: str | None = None
    project_id: str | None = None
    project_name: str | None = None
    github_repository: str | None = None
    jira_project_key: str | None = None
    current_worker_capability: WorkerCapability = WorkerCapability.LINUX
    available_worker_capabilities: tuple[WorkerCapability, ...] = field(default_factory=lambda: (WorkerCapability.LINUX,))
    base_branch: str | None = None
    integration_branch: str | None = None
    pr_target_branch: str | None = None
    execution_branch: str | None = None
    start_point_ref: str | None = None
    start_point_sha: str | None = None
    pr_number: int | None = None
    trigger_context: dict | None = None
    entry_mode: str = "fresh"
    entry_stage: str | None = None
    checkpoint_kind: str | None = None
    checkpoint_id: str | None = None
    checkpoint_payload: dict | None = None
    checkpoint_session_id: str | None = None
    human_inputs: list[dict[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class PmPlan:
    plan_steps: list[str]
    acceptance_criteria: list[str]
    risks: list[str]
    outcome: StageOutcome = "continue"
    next_stage: str = "dev"
    execution_worker_capability: str = "linux"
    blocker_message: str | None = None
    requeue_target: str | None = None
    requeue_reason: str | None = None
    resolved_prerequisites: list[str] = field(default_factory=list)
    unresolved_prerequisites: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DevResult:
    change_summary: list[str]
    pr_url: str | None
    outcome: StageOutcome = "continue"
    blocker_message: str | None = None


@dataclass(frozen=True)
class TestResult:
    guidance: list[str]
    outcome: StageOutcome = "continue"
    feedback: str | None = None
    blocker_message: str | None = None


# Prevent pytest from collecting this dataclass as a test class.
TestResult.__test__ = False


@dataclass(frozen=True)
class ReviewResult:
    summary: list[str]
    outcome: StageOutcome = "continue"
    feedback: str | None = None
    pr_url: str | None = None
    blocker_message: str | None = None


@dataclass(frozen=True)
class WorkflowDiagnostics:
    stage: str
    message: str
    attempts: int
    history: list[dict[str, str]]


@dataclass(frozen=True)
class WorkflowStageCheckpoint:
    stage: str
    attempt: int
    status: str
    summary: str
    plan: PmPlan | None = None
    dev_result: DevResult | None = None
    test_result: TestResult | None = None
    review_result: ReviewResult | None = None


@dataclass(frozen=True)
class WorkflowResult:
    outcome: WorkflowOutcome
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
    requeue_target: WorkerCapability | None = None
    requeue_reason: str | None = None
    blocker_message: str | None = None


class WorkflowAgents(Protocol):
    def execute(
        self,
        request: WorkflowRequest,
        *,
        test_feedback_hook: Callable[[int, str], None] | None = None,
        stage_checkpoint_hook: Callable[[WorkflowStageCheckpoint], None] | None = None,
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
        stage_checkpoint_hook: Callable[[WorkflowStageCheckpoint], None] | None = None,
    ) -> WorkflowResult:
        try:
            return self._agents.execute(
                request,
                test_feedback_hook=test_feedback_hook,
                stage_checkpoint_hook=stage_checkpoint_hook,
            )
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
            outcome="blocked",
            plan=plan,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=attempts,
            dev_rationale=list(dev_rationale or ()),
            review_summary=list(review_summary or ()),
            review_feedback=review_feedback,
            follow_up_issue=follow_up_issue,
            blocker_message=message,
            diagnostics=WorkflowDiagnostics(
                stage=stage,
                message=message,
                attempts=attempts,
                history=history,
            ),
        )
