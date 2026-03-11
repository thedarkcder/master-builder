from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from orchestrator.core.codex_agents import CodexWorkflowAgents
from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.worker_capabilities import normalize_worker_capability
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
    WorkflowDiagnostics,
    WorkflowRequest,
    WorkflowResult,
)


class StageAgents(Protocol):
    def pm(
        self,
        request: WorkflowRequest,
        attempt: int,
        feedback: str | None,
        history: list[dict[str, str]],
        last_dev_result: DevResult | None,
        last_test_result: TestResult | None,
        last_review_result: ReviewResult | None,
    ) -> PmPlan:
        ...

    def dev(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        attempt: int,
        feedback: str | None,
    ) -> DevResult:
        ...

    def test(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        attempt: int,
    ) -> TestResult:
        ...

    def review(
        self,
        request: WorkflowRequest,
        plan: PmPlan,
        dev_result: DevResult,
        test_result: TestResult,
        attempt: int,
    ) -> ReviewResult:
        ...


@dataclass
class _ExecutionState:
    plan: PmPlan | None
    stage_trace: list[dict[str, object]]
    history: list[dict[str, str]]
    dev_rationale: list[str]
    review_summary: list[str]
    review_feedback: str | None
    test_guidance: list[str]


class OrchestratedRunWorkflowExecutor:
    def __init__(
        self,
        *,
        runtime: CodexRuntime,
        log_sink: Callable[[dict], None] | None = None,
        stage_agents: StageAgents | None = None,
    ):
        self._runtime = runtime
        self._log_sink = log_sink
        self._stage_agents = stage_agents

    def execute(
        self,
        request: WorkflowRequest,
        *,
        test_feedback_hook: Callable[[int, str], None] | None = None,
    ) -> WorkflowResult:
        agents = self._stage_agents or CodexWorkflowAgents(runtime=self._runtime, log_sink=self._log_sink)
        history: list[dict[str, str]] = []
        stage_trace: list[dict[str, object]] = []
        test_guidance = list(request.suggested_test_commands or ["Run relevant project tests"])
        last_dev_result: DevResult | None = None
        last_test_result: TestResult | None = None
        last_review_result: ReviewResult | None = None

        try:
            plan = agents.pm(
                request,
                1,
                None,
                history,
                last_dev_result,
                last_test_result,
                last_review_result,
            )
        except Exception as exc:  # noqa: BLE001
            return self._failure_result(
                request=request,
                state=_ExecutionState(
                    plan=None,
                    stage_trace=stage_trace,
                    history=history,
                    dev_rationale=[],
                    review_summary=[],
                    review_feedback=None,
                    test_guidance=test_guidance,
                ),
                stage="pm",
                attempts=1,
                message=f"PM stage failed: {exc}",
            )

        stage_trace.append(
            _stage_trace_entry(
                stage="pm",
                status="completed",
                attempt=1,
                summary=_summarize_pm_plan(plan),
            )
        )
        state = _ExecutionState(
            plan=plan,
            stage_trace=stage_trace,
            history=history,
            dev_rationale=[],
            review_summary=[],
            review_feedback=None,
            test_guidance=test_guidance,
        )

        capability_mismatch_message = _capability_mismatch_message(request=request, plan=plan)
        if capability_mismatch_message is not None:
            history.append({"stage": "pm", "attempt": "1", "event": capability_mismatch_message})
            stage_trace[-1]["status"] = "blocked"
            stage_trace[-1]["summary"] = capability_mismatch_message
            return self._failure_result(
                request=request,
                state=state,
                stage="pm",
                attempts=1,
                message=capability_mismatch_message,
                classification="capability_mismatch",
            )

        missing_evidence_message = _missing_evidence_message(plan)
        if missing_evidence_message is not None:
            history.append({"stage": "pm", "attempt": "1", "event": missing_evidence_message})
            stage_trace[-1]["status"] = "blocked"
            stage_trace[-1]["summary"] = missing_evidence_message
            return self._failure_result(
                request=request,
                state=state,
                stage="pm",
                attempts=1,
                message=missing_evidence_message,
                classification="missing_context",
            )

        external_blocker_message = _external_blocker_message(plan)
        if external_blocker_message is not None:
            history.append({"stage": "pm", "attempt": "1", "event": external_blocker_message})
            stage_trace[-1]["status"] = "blocked"
            stage_trace[-1]["summary"] = external_blocker_message
            return self._failure_result(
                request=request,
                state=state,
                stage="pm",
                attempts=1,
                message=external_blocker_message,
                classification="external_blocker",
            )

        next_feedback: str | None = None
        max_loops = max(1, int(request.max_dev_test_review_loops or 1))
        for attempt in range(1, max_loops + 1):
            try:
                dev_result = agents.dev(request, plan, attempt, next_feedback)
            except Exception as exc:  # noqa: BLE001
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="dev",
                    attempts=attempt,
                    message=f"Dev stage failed: {exc}",
                    classification="implementation_failure",
                )

            last_dev_result = dev_result
            state.dev_rationale[:] = list(dev_result.change_summary)
            if dev_result.hard_stop_reason:
                history.append({"stage": "dev", "attempt": str(attempt), "event": dev_result.hard_stop_reason})
                stage_trace.append(
                    _stage_trace_entry(
                        stage="dev",
                        status="blocked",
                        attempt=attempt,
                        summary=dev_result.hard_stop_reason,
                    )
                )
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="dev",
                    attempts=attempt,
                    message=dev_result.hard_stop_reason,
                    classification="implementation_blocked",
                )
            stage_trace.append(
                _stage_trace_entry(
                    stage="dev",
                    status="completed",
                    attempt=attempt,
                    summary=_summarize_dev_result(dev_result),
                )
            )

            try:
                test_result = agents.test(request, plan, dev_result, attempt)
            except Exception as exc:  # noqa: BLE001
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="test",
                    attempts=attempt,
                    message=f"Test stage failed: {exc}",
                    classification="verification_failure",
                )

            last_test_result = test_result
            state.test_guidance[:] = list(test_result.guidance or state.test_guidance)
            if not test_result.passed:
                feedback = _summarize_test_feedback(test_result)
                history.append({"stage": "test", "attempt": str(attempt), "event": feedback})
                stage_trace.append(
                    _stage_trace_entry(
                        stage="test",
                        status="failed",
                        attempt=attempt,
                        summary=feedback,
                    )
                )
                if test_feedback_hook is not None:
                    test_feedback_hook(attempt, feedback)
                if attempt >= max_loops:
                    return self._failure_result(
                        request=request,
                        state=state,
                        stage="test",
                        attempts=attempt,
                        message=f"Max workflow attempts reached after test failures. Last feedback: {feedback}",
                        classification="verification_failure",
                    )
                next_feedback = feedback
                continue
            stage_trace.append(
                _stage_trace_entry(
                    stage="test",
                    status="completed",
                    attempt=attempt,
                    summary=_summarize_test_result(test_result),
                )
            )

            try:
                review_result = agents.review(request, plan, dev_result, test_result, attempt)
            except Exception as exc:  # noqa: BLE001
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="review",
                    attempts=attempt,
                    message=f"Review stage failed: {exc}",
                    classification="review_failure",
                )

            last_review_result = review_result
            state.review_summary[:] = list(review_result.summary)
            state.review_feedback = review_result.feedback
            review_message = _summarize_review_result(review_result)
            review_outcome = _normalize_review_outcome(review_result)
            if review_outcome == "approved":
                stage_trace.append(
                    _stage_trace_entry(
                        stage="review",
                        status="completed",
                        attempt=attempt,
                        summary=review_message,
                    )
                )
                pr_url = review_result.pr_url or dev_result.pr_url
                return WorkflowResult(
                    succeeded=True,
                    plan=plan,
                    pr_url=pr_url,
                    summary=list(review_result.summary or dev_result.change_summary or ["Workflow completed"]),
                    test_guidance=list(state.test_guidance),
                    attempts=attempt,
                    dev_rationale=list(state.dev_rationale),
                    review_summary=list(state.review_summary),
                    review_feedback=None,
                    orchestration_stage_trace=list(stage_trace),
                    orchestration_workstream_trace=[],
                )

            history.append({"stage": "review", "attempt": str(attempt), "event": review_message})
            if review_outcome == "blocked":
                stage_trace.append(
                    _stage_trace_entry(
                        stage="review",
                        status="blocked",
                        attempt=attempt,
                        summary=review_message,
                    )
                )
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="review",
                    attempts=attempt,
                    message=review_message,
                    classification="review_blocked",
                )

            stage_trace.append(
                _stage_trace_entry(
                    stage="review",
                    status="failed",
                    attempt=attempt,
                    summary=review_message,
                )
            )
            if attempt >= max_loops:
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="review",
                    attempts=attempt,
                    message=f"Max workflow attempts reached after review feedback. Last feedback: {review_message}",
                    classification="review_needs_changes",
                )
            next_feedback = review_message

        return self._failure_result(
            request=request,
            state=state,
            stage="workflow",
            attempts=max_loops,
            message="Workflow ended without approval.",
            classification="workflow_incomplete",
        )

    def _failure_result(
        self,
        *,
        request: WorkflowRequest,
        state: _ExecutionState,
        stage: str,
        attempts: int,
        message: str,
        classification: str = "workflow_failure",
    ) -> WorkflowResult:
        return WorkflowResult(
            succeeded=False,
            plan=state.plan,
            pr_url=None,
            summary=[],
            test_guidance=list(state.test_guidance),
            attempts=attempts,
            dev_rationale=list(state.dev_rationale),
            review_summary=list(state.review_summary),
            review_feedback=state.review_feedback,
            diagnostics=WorkflowDiagnostics(
                stage=stage,
                message=message,
                attempts=attempts,
                history=list(state.history),
                classification=classification,
            ),
            orchestration_stage_trace=list(state.stage_trace),
            orchestration_workstream_trace=[],
        )


def _stage_trace_entry(*, stage: str, status: str, attempt: int, summary: str) -> dict[str, object]:
    return {
        "stage": stage,
        "status": status,
        "attempt": attempt,
        "summary": summary,
    }


def _capability_mismatch_message(*, request: WorkflowRequest, plan: PmPlan) -> str | None:
    required = normalize_worker_capability(plan.execution_worker_capability)
    current = normalize_worker_capability(request.current_worker_capability)
    if required is None or current is None or required == current:
        return None
    return (
        f"Execution capability mismatch: PM selected {required} but current worker is {current}. "
        f"Requeue on worker:{required} before dev/test/review."
    )


def _missing_evidence_message(plan: PmPlan) -> str | None:
    missing_sources = [value for value in plan.missing_evidence_sources if str(value).strip()]
    if not missing_sources:
        return None
    joined_sources = ", ".join(missing_sources)
    return f"PM could not load required evidence sources before implementation: {joined_sources}."


def _external_blocker_message(plan: PmPlan) -> str | None:
    blockers = [value for value in plan.confirmed_external_blockers if str(value).strip()]
    if not blockers:
        return None
    return "; ".join(blockers[:3])


def _summarize_pm_plan(plan: PmPlan) -> str:
    if plan.plan_steps:
        return f"PM produced {len(plan.plan_steps)} execution steps and {len(plan.acceptance_criteria)} acceptance criteria."
    if plan.acceptance_criteria:
        return f"PM captured {len(plan.acceptance_criteria)} acceptance criteria."
    return "PM planning completed."


def _summarize_dev_result(result: DevResult) -> str:
    summary = "; ".join(result.change_summary[:2]).strip()
    return summary or "Dev stage completed."


def _summarize_test_result(result: TestResult) -> str:
    if result.guidance:
        return "; ".join(result.guidance[:2])
    return "Test stage passed."


def _summarize_test_feedback(result: TestResult) -> str:
    feedback = str(result.feedback or "").strip()
    if feedback:
        return feedback
    if result.guidance:
        return "; ".join(result.guidance[:3])
    return "Test stage reported failures."


def _summarize_review_result(result: ReviewResult) -> str:
    feedback = str(result.feedback or "").strip()
    if feedback:
        return feedback
    if result.summary:
        return "; ".join(result.summary[:3])
    outcome = _normalize_review_outcome(result)
    if outcome == "blocked":
        return "Review blocked the workflow."
    if outcome == "approved":
        return "Review approved the workflow."
    return "Review requested changes."


def _normalize_review_outcome(result: ReviewResult) -> str:
    outcome = str(result.outcome or "").strip().lower()
    if outcome in {"approved", "needs_changes", "blocked"}:
        return outcome
    return "approved" if result.approved else "needs_changes"
