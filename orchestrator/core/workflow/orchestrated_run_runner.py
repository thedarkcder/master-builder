from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from typing import Any
from typing import Protocol

from orchestrator.core.runtime.agents import CodexWorkflowAgents
from orchestrator.core.runtime.runtime import CodexRuntime
from orchestrator.core.worker.capability_normalization import WorkerCapability
from orchestrator.core.worker.capability_normalization import parse_worker_capability
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    StageOutcome,
    TestResult,
    WorkflowDiagnostics,
    WorkflowOutcome,
    WorkflowRequest,
    WorkflowResult,
    WorkflowStageCheckpoint,
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
        capture_target_constraints_json: str = "[]",
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
        pm_capture_target_constraints_json: str = "[]",
        runtime_resolver: Callable[[str, WorkflowRequest], CodexRuntime] | None = None,
        log_sink: Callable[[dict], None] | None = None,
        stage_agents: StageAgents | None = None,
        execute_tool: Callable[
            [str, str | None, str | None, str, str, str, dict[str, object], str | None],
            dict[str, Any],
        ] | None = None,
    ):
        self._runtime = runtime
        self._pm_capture_target_constraints_json = pm_capture_target_constraints_json
        self._runtime_resolver = runtime_resolver
        self._log_sink = log_sink
        self._stage_agents = stage_agents
        self._execute_tool = execute_tool

    def execute(
        self,
        request: WorkflowRequest,
        *,
        test_feedback_hook: Callable[[int, str], None] | None = None,
        stage_checkpoint_hook: Callable[[WorkflowStageCheckpoint], None] | None = None,
    ) -> WorkflowResult:
        agents = self._stage_agents or CodexWorkflowAgents(
            runtime=self._runtime,
            runtime_resolver=self._runtime_resolver,
            log_sink=self._log_sink,
            execute_tool=(
                None
                if self._execute_tool is None
                else lambda context, tool_name, tool_args: self._execute_tool(
                    context.tenant_id or "",
                    context.project_id,
                    context.run_id,
                    context.issue_key or "",
                    context.stage,
                    tool_name,
                    tool_args,
                    context.worker_platform,
                )
            ),
        )
        history: list[dict[str, str]] = []
        stage_trace: list[dict[str, object]] = []
        test_guidance = list(request.suggested_test_commands or ["Run relevant project tests"])
        last_dev_result: DevResult | None = None
        last_test_result: TestResult | None = None
        last_review_result: ReviewResult | None = None

        resume_mode = str(request.entry_mode or "").strip().lower() == "resume"
        resume_from_pm = _should_resume_from_pm(request)
        resume_from_dev = _should_resume_from_dev(request)
        resume_from_test = _should_resume_from_test(request)
        resume_from_review = _should_resume_from_review(request)
        resume_snapshot = ExecutionSnapshot.load(request.checkpoint_payload) if resume_mode else None
        resumed_from_persisted_pm = False

        if resume_from_pm or resume_from_dev or resume_from_test:
            plan = resume_snapshot.plan() if resume_snapshot is not None else None
            if plan is None:
                if resume_from_pm:
                    # PM can request transient human input before emitting a persisted PM artifact.
                    # In that case, resume by re-entering PM with the answered input instead of failing.
                    try:
                        plan = agents.pm(
                            request,
                            1,
                            None,
                            history,
                            last_dev_result,
                            last_test_result,
                            last_review_result,
                            self._pm_capture_target_constraints_json,
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
                            status=_checkpoint_status_for_outcome(plan.outcome),
                            attempt=1,
                            summary=plan.blocker_message or _summarize_pm_plan(plan),
                        )
                    )
                else:
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
                        stage="dev" if resume_from_dev else "test",
                        attempts=1,
                        message=(
                            "Cannot resume dev stage because no valid persisted PM plan is available."
                            if resume_from_dev
                            else "Cannot resume test stage because no valid persisted PM plan is available."
                        ),
                    )
            else:
                resumed_from_persisted_pm = True
                stage_trace.append(
                    _stage_trace_entry(
                        stage="pm",
                        status="completed",
                        attempt=1,
                        summary="Resumed from persisted PM plan.",
                    )
                )
        elif resume_from_review:
            plan = resume_snapshot.plan() if resume_snapshot is not None else None
            if plan is None:
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
                    stage="review",
                    attempts=1,
                    message="Cannot resume review stage because no valid persisted PM plan is available.",
                )
            stage_trace.extend(
                [
                    _stage_trace_entry(stage="pm", status="completed", attempt=1, summary="Resumed from persisted PM plan."),
                ]
            )
        else:
            try:
                plan = agents.pm(
                    request,
                    1,
                    None,
                    history,
                    last_dev_result,
                    last_test_result,
                    last_review_result,
                    self._pm_capture_target_constraints_json,
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
            plan = _normalize_mixed_demo_target_pm_requeue(
                plan=plan,
                current_worker_capability=request.current_worker_capability,
                capture_target_constraints_json=self._pm_capture_target_constraints_json,
            )
            stage_trace.append(
                _stage_trace_entry(
                    stage="pm",
                    status=_checkpoint_status_for_outcome(plan.outcome),
                    attempt=1,
                    summary=plan.blocker_message or _summarize_pm_plan(plan),
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

        def _persist_stage_checkpoint(checkpoint: WorkflowStageCheckpoint) -> WorkflowResult | None:
            if stage_checkpoint_hook is None:
                return None
            try:
                stage_checkpoint_hook(checkpoint)
            except Exception as exc:  # noqa: BLE001
                return self._failure_result(
                    request=request,
                    state=state,
                    stage=checkpoint.stage,
                    attempts=checkpoint.attempt,
                    message=f"{checkpoint.stage.upper()} checkpoint persistence failed: {type(exc).__name__}: {exc}",
                )
            return None

        if resumed_from_persisted_pm:
            checkpoint_failure = _persist_stage_checkpoint(
                WorkflowStageCheckpoint(
                    stage="pm",
                    attempt=1,
                    status="completed",
                    summary="Resumed from persisted PM plan.",
                    plan=plan,
                )
            )
            if checkpoint_failure is not None:
                return checkpoint_failure

        if _should_resume_from_review(request):
            resumed_dev_result = resume_snapshot.dev_result() if resume_snapshot is not None else None
            resumed_test_result = resume_snapshot.test_result() if resume_snapshot is not None else None
            if resumed_dev_result is None or resumed_test_result is None:
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="review",
                    attempts=1,
                    message="Cannot resume review stage because no valid persisted dev/test artifacts are available.",
                )
            state.dev_rationale[:] = list(resumed_dev_result.change_summary)
            state.test_guidance[:] = list(resumed_test_result.guidance or state.test_guidance)
            previous_review = resume_snapshot.review_result() if resume_snapshot is not None else None
            if previous_review is not None:
                state.review_summary[:] = list(previous_review.summary)
                state.review_feedback = previous_review.feedback
            stage_trace.extend(
                [
                    _stage_trace_entry(stage="dev", status="completed", attempt=1, summary="Resumed from persisted dev result."),
                    _stage_trace_entry(stage="test", status="completed", attempt=1, summary="Resumed from persisted passing test result."),
                ]
            )
            for checkpoint in (
                WorkflowStageCheckpoint(stage="pm", attempt=1, status="completed", summary="Resumed from persisted PM plan.", plan=plan),
                WorkflowStageCheckpoint(stage="dev", attempt=1, status="completed", summary="Resumed from persisted dev result.", dev_result=resumed_dev_result),
                WorkflowStageCheckpoint(stage="test", attempt=1, status="completed", summary="Resumed from persisted passing test result.", test_result=resumed_test_result),
            ):
                checkpoint_failure = _persist_stage_checkpoint(checkpoint)
                if checkpoint_failure is not None:
                    return checkpoint_failure
            try:
                review_result = agents.review(request, plan, resumed_dev_result, resumed_test_result, 1)
            except Exception as exc:  # noqa: BLE001
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="review",
                    attempts=1,
                    message=f"Review stage failed: {exc}",
                )
            state.review_summary[:] = list(review_result.summary)
            state.review_feedback = review_result.feedback
            review_message = _summarize_review_result(review_result)
            if review_result.outcome == "continue":
                pr_url, publication_failure = self._ensure_required_pull_request(
                    request=request,
                    state=state,
                    plan=plan,
                    dev_result=resumed_dev_result,
                    test_result=resumed_test_result,
                    review_result=review_result,
                    attempt=1,
                )
                if publication_failure is not None:
                    return publication_failure
                checkpoint_failure = _persist_stage_checkpoint(
                    WorkflowStageCheckpoint(stage="review", attempt=1, status="completed", summary=review_message, review_result=review_result)
                )
                if checkpoint_failure is not None:
                    return checkpoint_failure
                stage_trace.append(_stage_trace_entry(stage="review", status="completed", attempt=1, summary=review_message))
                return WorkflowResult(
                    outcome="success",
                    plan=plan,
                    pr_url=pr_url,
                    summary=list(review_result.summary or resumed_dev_result.change_summary or ["Workflow completed"]),
                    test_guidance=list(state.test_guidance),
                    attempts=1,
                    dev_rationale=list(state.dev_rationale),
                    review_summary=list(state.review_summary),
                    review_feedback=None,
                    orchestration_stage_trace=list(stage_trace),
                    orchestration_workstream_trace=[],
                )
            history.append({"stage": "review", "attempt": "1", "event": review_message})
            checkpoint_failure = _persist_stage_checkpoint(
                WorkflowStageCheckpoint(
                    stage="review",
                    attempt=1,
                    status=_checkpoint_status_for_outcome(review_result.outcome),
                    summary=review_result.blocker_message or review_result.feedback or review_message,
                    review_result=review_result,
                )
            )
            if checkpoint_failure is not None:
                return checkpoint_failure
            stage_trace.append(
                _stage_trace_entry(
                    stage="review",
                    status=_checkpoint_status_for_outcome(review_result.outcome),
                    attempt=1,
                    summary=review_result.blocker_message or review_result.feedback or review_message,
                )
            )
            return self._failure_result(
                request=request,
                state=state,
                stage="review",
                attempts=1,
                message=f"Review resume ended without approval. Last feedback: {review_result.feedback or review_message}",
                outcome=_workflow_outcome_for_stage_outcome(review_result.outcome),
            )
        resumed_dev_result_for_test: DevResult | None = None
        if _should_resume_from_test(request):
            resumed_dev_result_for_test = resume_snapshot.dev_result() if resume_snapshot is not None else None
            if resumed_dev_result_for_test is None:
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="test",
                    attempts=1,
                    message="Cannot resume test stage because no valid persisted dev artifact is available.",
                )
            state.dev_rationale[:] = list(resumed_dev_result_for_test.change_summary)
            stage_trace.append(
                _stage_trace_entry(
                    stage="dev",
                    status="completed",
                    attempt=1,
                    summary="Resumed from persisted dev result.",
                )
            )
            checkpoint_failure = _persist_stage_checkpoint(
                WorkflowStageCheckpoint(
                    stage="dev",
                    attempt=1,
                    status="completed",
                    summary="Resumed from persisted dev result.",
                    dev_result=resumed_dev_result_for_test,
                )
            )
            if checkpoint_failure is not None:
                return checkpoint_failure

        capability_mismatch_message = _capability_mismatch_message(request=request, plan=plan)
        required_worker_capability = _required_worker_capability(plan)
        if (
            plan.outcome == "continue"
            and capability_mismatch_message is not None
            and required_worker_capability is not None
        ):
            history.append({"stage": "pm", "attempt": "1", "event": capability_mismatch_message})
            stage_trace[-1]["status"] = "requeue"
            stage_trace[-1]["summary"] = capability_mismatch_message
            checkpoint_failure = _persist_stage_checkpoint(
                WorkflowStageCheckpoint(stage="pm", attempt=1, status="requeue", summary=capability_mismatch_message, plan=plan)
            )
            if checkpoint_failure is not None:
                return checkpoint_failure
            return self._workflow_result(
                state=state,
                outcome="requeue",
                attempts=1,
                requeue_target=required_worker_capability,
                requeue_reason=capability_mismatch_message,
            )

        if plan.outcome != "continue":
            pm_message = plan.blocker_message or stage_trace[-1]["summary"]
            history.append({"stage": "pm", "attempt": "1", "event": pm_message})
            stage_trace[-1]["status"] = _checkpoint_status_for_outcome(plan.outcome)
            stage_trace[-1]["summary"] = pm_message
            checkpoint_failure = _persist_stage_checkpoint(
                WorkflowStageCheckpoint(
                    stage="pm",
                    attempt=1,
                    status=_checkpoint_status_for_outcome(plan.outcome),
                    summary=pm_message,
                    plan=plan,
                )
            )
            if checkpoint_failure is not None:
                return checkpoint_failure
            if plan.outcome == "requeue":
                requeue_target = parse_worker_capability(plan.requeue_target) or _required_worker_capability(plan)
                if requeue_target is None:
                    return self._failure_result(
                        request=request,
                        state=state,
                        stage="pm",
                        attempts=1,
                        message=f"PM requested requeue but did not provide a valid worker capability. {pm_message}",
                        outcome="blocked",
                    )
                return self._workflow_result(
                    state=state,
                    outcome="requeue",
                    attempts=1,
                    requeue_target=requeue_target,
                    requeue_reason=plan.requeue_reason or pm_message,
                )
            return self._failure_result(
                request=request,
                state=state,
                stage="pm",
                attempts=1,
                message=pm_message,
                outcome=_workflow_outcome_for_stage_outcome(plan.outcome),
            )

        checkpoint_failure = _persist_stage_checkpoint(
            WorkflowStageCheckpoint(stage="pm", attempt=1, status="completed", summary=_summarize_pm_plan(plan), plan=plan)
        )
        if checkpoint_failure is not None:
            return checkpoint_failure

        next_feedback: str | None = None
        max_loops = max(1, int(request.max_dev_test_review_loops or 1))
        for attempt in range(1, max_loops + 1):
            if attempt == 1 and resumed_dev_result_for_test is not None:
                dev_result = resumed_dev_result_for_test
                last_dev_result = dev_result
            elif attempt == 1 and plan.next_stage == "test":
                dev_result = _build_direct_test_dev_result(request)
                last_dev_result = dev_result
                dev_summary = _summarize_direct_test_dev_result(request)
                stage_trace.append(
                    _stage_trace_entry(
                        stage="dev",
                        status="completed",
                        attempt=attempt,
                        summary=dev_summary,
                    )
                )
                checkpoint_failure = _persist_stage_checkpoint(
                    WorkflowStageCheckpoint(
                        stage="dev",
                        attempt=attempt,
                        status="completed",
                        summary=dev_summary,
                        dev_result=dev_result,
                    )
                )
                if checkpoint_failure is not None:
                    return checkpoint_failure
                state.dev_rationale[:] = list(dev_result.change_summary)
            else:
                try:
                    dev_result = agents.dev(request, plan, attempt, next_feedback)
                except Exception as exc:  # noqa: BLE001
                    return self._failure_result(
                        request=request,
                        state=state,
                        stage="dev",
                        attempts=attempt,
                        message=f"Dev stage failed: {exc}",
                    )

                last_dev_result = dev_result
                state.dev_rationale[:] = list(dev_result.change_summary)
                dev_message = dev_result.blocker_message or _summarize_dev_result(dev_result)
                if dev_result.outcome != "continue":
                    history.append({"stage": "dev", "attempt": str(attempt), "event": dev_message})
                    checkpoint_failure = _persist_stage_checkpoint(
                        WorkflowStageCheckpoint(
                            stage="dev",
                            attempt=attempt,
                            status=_checkpoint_status_for_outcome(dev_result.outcome),
                            summary=dev_message,
                            dev_result=dev_result,
                        )
                    )
                    if checkpoint_failure is not None:
                        return checkpoint_failure
                    stage_trace.append(
                        _stage_trace_entry(
                            stage="dev",
                            status=_checkpoint_status_for_outcome(dev_result.outcome),
                            attempt=attempt,
                            summary=dev_message,
                        )
                    )
                    return self._failure_result(
                        request=request,
                        state=state,
                        stage="dev",
                        attempts=attempt,
                        message=dev_message,
                        outcome=_workflow_outcome_for_stage_outcome(dev_result.outcome),
                    )
                stage_trace.append(
                    _stage_trace_entry(
                        stage="dev",
                        status="completed",
                        attempt=attempt,
                        summary=_summarize_dev_result(dev_result),
                    )
                )
                checkpoint_failure = _persist_stage_checkpoint(
                    WorkflowStageCheckpoint(
                        stage="dev",
                        attempt=attempt,
                        status="completed",
                        summary=_summarize_dev_result(dev_result),
                        dev_result=dev_result,
                    )
                )
                if checkpoint_failure is not None:
                    return checkpoint_failure
                tracked_worktree_changes = _tracked_worktree_change_paths(request.execution_repo_dir)
                if tracked_worktree_changes:
                    feedback = _summarize_unpublished_dev_changes(tracked_worktree_changes)
                    history.append({"stage": "dev", "attempt": str(attempt), "event": feedback})
                    checkpoint_failure = _persist_stage_checkpoint(
                        WorkflowStageCheckpoint(
                            stage="dev",
                            attempt=attempt,
                            status="failed",
                            summary=feedback,
                            dev_result=dev_result,
                        )
                    )
                    if checkpoint_failure is not None:
                        return checkpoint_failure
                    stage_trace.append(
                        _stage_trace_entry(
                            stage="dev",
                            status="failed",
                            attempt=attempt,
                            summary=feedback,
                        )
                    )
                    if attempt >= max_loops:
                        return self._failure_result(
                            request=request,
                            state=state,
                            stage="dev",
                            attempts=attempt,
                            message=f"Max workflow attempts reached after dev publication failures. Last feedback: {feedback}",
                            outcome="failed",
                        )
                    next_feedback = feedback
                    continue
                mixed_housekeeping_paths = _mixed_housekeeping_source_paths(
                    request.execution_repo_dir,
                    request.base_branch,
                )
                if mixed_housekeeping_paths:
                    feedback = _summarize_mixed_housekeeping_source_paths(mixed_housekeeping_paths)
                    history.append({"stage": "dev", "attempt": str(attempt), "event": feedback})
                    checkpoint_failure = _persist_stage_checkpoint(
                        WorkflowStageCheckpoint(
                            stage="dev",
                            attempt=attempt,
                            status="failed",
                            summary=feedback,
                            dev_result=dev_result,
                        )
                    )
                    if checkpoint_failure is not None:
                        return checkpoint_failure
                    stage_trace.append(
                        _stage_trace_entry(
                            stage="dev",
                            status="failed",
                            attempt=attempt,
                            summary=feedback,
                        )
                    )
                    if attempt >= max_loops:
                        return self._failure_result(
                            request=request,
                            state=state,
                            stage="dev",
                            attempts=attempt,
                            message=f"Max workflow attempts reached after dev scope violations. Last feedback: {feedback}",
                            outcome="failed",
                        )
                    next_feedback = feedback
                    continue

            try:
                test_result = agents.test(request, plan, dev_result, attempt)
            except Exception as exc:  # noqa: BLE001
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="test",
                    attempts=attempt,
                    message=f"Test stage failed: {exc}",
                )

            last_test_result = test_result
            state.test_guidance[:] = list(test_result.guidance or state.test_guidance)
            test_message = test_result.blocker_message or _summarize_test_feedback(test_result)
            if test_result.outcome in {"blocked", "waiting_for_input", "requeue"}:
                history.append({"stage": "test", "attempt": str(attempt), "event": test_message})
                checkpoint_failure = _persist_stage_checkpoint(
                    WorkflowStageCheckpoint(
                        stage="test",
                        attempt=attempt,
                        status=_checkpoint_status_for_outcome(test_result.outcome),
                        summary=test_message,
                        test_result=test_result,
                    )
                )
                if checkpoint_failure is not None:
                    return checkpoint_failure
                stage_trace.append(_stage_trace_entry(stage="test", status=_checkpoint_status_for_outcome(test_result.outcome), attempt=attempt, summary=test_message))
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="test",
                    attempts=attempt,
                    message=test_message,
                    outcome=_workflow_outcome_for_stage_outcome(test_result.outcome),
                )
            if test_result.outcome == "failed":
                feedback = _summarize_test_feedback(test_result)
                history.append({"stage": "test", "attempt": str(attempt), "event": feedback})
                checkpoint_failure = _persist_stage_checkpoint(
                    WorkflowStageCheckpoint(stage="test", attempt=attempt, status="failed", summary=feedback, test_result=test_result)
                )
                if checkpoint_failure is not None:
                    return checkpoint_failure
                stage_trace.append(_stage_trace_entry(stage="test", status="failed", attempt=attempt, summary=feedback))
                if test_feedback_hook is not None:
                    test_feedback_hook(attempt, feedback)
                if attempt >= max_loops:
                    return self._failure_result(
                        request=request,
                        state=state,
                        stage="test",
                        attempts=attempt,
                        message=f"Max workflow attempts reached after test failures. Last feedback: {feedback}",
                        outcome="failed",
                    )
                next_feedback = feedback
                continue
            stage_trace.append(_stage_trace_entry(stage="test", status="completed", attempt=attempt, summary=_summarize_test_result(test_result)))
            checkpoint_failure = _persist_stage_checkpoint(
                WorkflowStageCheckpoint(stage="test", attempt=attempt, status="completed", summary=_summarize_test_result(test_result), test_result=test_result)
            )
            if checkpoint_failure is not None:
                return checkpoint_failure

            try:
                review_result = agents.review(request, plan, dev_result, test_result, attempt)
            except Exception as exc:  # noqa: BLE001
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="review",
                    attempts=attempt,
                    message=f"Review stage failed: {exc}",
                )

            last_review_result = review_result
            state.review_summary[:] = list(review_result.summary)
            state.review_feedback = review_result.feedback
            review_message = _summarize_review_result(review_result)
            if review_result.outcome == "continue":
                pr_url, publication_failure = self._ensure_required_pull_request(
                    request=request,
                    state=state,
                    plan=plan,
                    dev_result=dev_result,
                    test_result=test_result,
                    review_result=review_result,
                    attempt=attempt,
                )
                if publication_failure is not None:
                    return publication_failure
                checkpoint_failure = _persist_stage_checkpoint(
                    WorkflowStageCheckpoint(stage="review", attempt=attempt, status="completed", summary=review_message, review_result=review_result)
                )
                if checkpoint_failure is not None:
                    return checkpoint_failure
                stage_trace.append(_stage_trace_entry(stage="review", status="completed", attempt=attempt, summary=review_message))
                return WorkflowResult(
                    outcome="success",
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
            if review_result.outcome in {"blocked", "waiting_for_input", "requeue"}:
                review_message = review_result.blocker_message or review_result.feedback or review_message
                checkpoint_failure = _persist_stage_checkpoint(
                    WorkflowStageCheckpoint(
                        stage="review",
                        attempt=attempt,
                        status=_checkpoint_status_for_outcome(review_result.outcome),
                        summary=review_message,
                        review_result=review_result,
                    )
                )
                if checkpoint_failure is not None:
                    return checkpoint_failure
                stage_trace.append(_stage_trace_entry(stage="review", status=_checkpoint_status_for_outcome(review_result.outcome), attempt=attempt, summary=review_message))
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="review",
                    attempts=attempt,
                    message=review_message,
                    outcome=_workflow_outcome_for_stage_outcome(review_result.outcome),
                )

            stage_trace.append(_stage_trace_entry(stage="review", status="failed", attempt=attempt, summary=review_message))
            checkpoint_failure = _persist_stage_checkpoint(
                WorkflowStageCheckpoint(stage="review", attempt=attempt, status="failed", summary=review_message, review_result=review_result)
            )
            if checkpoint_failure is not None:
                return checkpoint_failure
            if attempt >= max_loops:
                return self._failure_result(
                    request=request,
                    state=state,
                    stage="review",
                    attempts=attempt,
                    message=f"Max workflow attempts reached after review feedback. Last feedback: {review_message}",
                    outcome="failed",
                )
            next_feedback = review_message

        return self._failure_result(
            request=request,
            state=state,
            stage="workflow",
            attempts=max_loops,
            message="Workflow ended without approval.",
            outcome="failed",
        )

    def _ensure_required_pull_request(
        self,
        *,
        request: WorkflowRequest,
        state: _ExecutionState,
        plan: PmPlan,
        dev_result: DevResult,
        test_result: TestResult,
        review_result: ReviewResult,
        attempt: int,
    ) -> tuple[str | None, WorkflowResult | None]:
        pr_url = review_result.pr_url or dev_result.pr_url
        if pr_url or not request.allow_pr_creation:
            return pr_url, None

        if self._execute_tool is None:
            message = "PR creation is required, but the workflow runner has no governed GitHub tool executor configured."
            state.history.append({"stage": "review", "attempt": str(attempt), "event": message})
            state.stage_trace.append(_stage_trace_entry(stage="review", status="blocked", attempt=attempt, summary=message))
            return None, self._failure_result(
                request=request,
                state=state,
                stage="review",
                attempts=attempt,
                message=message,
                outcome="blocked",
            )

        try:
            push_result = self._execute_tool(
                request.tenant_id,
                request.project_id,
                request.run_id,
                request.issue_key,
                "review",
                "github.push_branch",
                {"branch_name": request.integration_branch or request.execution_branch or ""},
                request.current_worker_capability.value,
            )
            head_branch = str(
                push_result.get("branch_name") or request.integration_branch or request.execution_branch or ""
            ).strip()
            pr_result = self._execute_tool(
                request.tenant_id,
                request.project_id,
                request.run_id,
                request.issue_key,
                "review",
                "github.open_pr",
                {
                    "title": _required_pr_title(request),
                    "head_branch": head_branch,
                    "base_branch": request.pr_target_branch or request.base_branch or "main",
                    "body": _required_pr_body(
                        request=request,
                        plan=plan,
                        dev_result=dev_result,
                        test_result=test_result,
                        review_result=review_result,
                    ),
                },
                request.current_worker_capability.value,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"Mandatory PR publication failed through governed GitHub tools: {type(exc).__name__}: {exc}"
            state.history.append({"stage": "review", "attempt": str(attempt), "event": message})
            state.stage_trace.append(_stage_trace_entry(stage="review", status="blocked", attempt=attempt, summary=message))
            return None, self._failure_result(
                request=request,
                state=state,
                stage="review",
                attempts=attempt,
                message=message,
                outcome="blocked",
            )

        pr_url = str(pr_result.get("pr_url") or "").strip()
        if not pr_url:
            message = "Mandatory PR publication completed without a pr_url from github.open_pr."
            state.history.append({"stage": "review", "attempt": str(attempt), "event": message})
            state.stage_trace.append(_stage_trace_entry(stage="review", status="blocked", attempt=attempt, summary=message))
            return None, self._failure_result(
                request=request,
                state=state,
                stage="review",
                attempts=attempt,
                message=message,
                outcome="blocked",
            )

        return pr_url, None

    def _failure_result(
        self,
        *,
        request: WorkflowRequest,
        state: _ExecutionState,
        stage: str,
        attempts: int,
        message: str,
        outcome: WorkflowOutcome = "blocked",
    ) -> WorkflowResult:
        return WorkflowResult(
            outcome=outcome,
            plan=state.plan,
            pr_url=None,
            summary=[],
            test_guidance=list(state.test_guidance),
            attempts=attempts,
            dev_rationale=list(state.dev_rationale),
            review_summary=list(state.review_summary),
            review_feedback=state.review_feedback,
            blocker_message=message if outcome in {"blocked", "waiting_for_input"} else None,
            diagnostics=WorkflowDiagnostics(
                stage=stage,
                message=message,
                attempts=attempts,
                history=list(state.history),
            ),
            orchestration_stage_trace=list(state.stage_trace),
            orchestration_workstream_trace=[],
        )

    def _workflow_result(
        self,
        *,
        state: _ExecutionState,
        outcome: WorkflowOutcome,
        attempts: int,
        requeue_target: WorkerCapability | None = None,
        requeue_reason: str | None = None,
        blocker_message: str | None = None,
    ) -> WorkflowResult:
        return WorkflowResult(
            outcome=outcome,
            plan=state.plan,
            pr_url=None,
            summary=[],
            test_guidance=list(state.test_guidance),
            attempts=attempts,
            dev_rationale=list(state.dev_rationale),
            review_summary=list(state.review_summary),
            review_feedback=state.review_feedback,
            diagnostics=None,
            orchestration_stage_trace=list(state.stage_trace),
            orchestration_workstream_trace=[],
            requeue_target=requeue_target,
            requeue_reason=requeue_reason,
            blocker_message=blocker_message,
        )


def _should_resume_from_pm(request: WorkflowRequest) -> bool:
    return (
        str(request.entry_mode or "").strip().lower() == "resume"
        and str(request.checkpoint_kind or "").strip().lower() == "pm"
    )


def _should_resume_from_dev(request: WorkflowRequest) -> bool:
    return (
        str(request.entry_mode or "").strip().lower() == "resume"
        and str(request.checkpoint_kind or "").strip().lower() == "execution"
        and str(request.entry_stage or "").strip().lower() == "dev"
    )


def _should_resume_from_review(request: WorkflowRequest) -> bool:
    return (
        str(request.entry_mode or "").strip().lower() == "resume"
        and str(request.checkpoint_kind or "").strip().lower() == "execution"
        and str(request.entry_stage or "").strip().lower() == "review"
    )


def _should_resume_from_test(request: WorkflowRequest) -> bool:
    return (
        str(request.entry_mode or "").strip().lower() == "resume"
        and str(request.checkpoint_kind or "").strip().lower() == "execution"
        and str(request.entry_stage or "").strip().lower() == "test"
    )


def _stage_trace_entry(*, stage: str, status: str, attempt: int, summary: str) -> dict[str, object]:
    return {
        "stage": stage,
        "status": status,
        "attempt": attempt,
        "summary": summary,
    }


def _capability_mismatch_message(*, request: WorkflowRequest, plan: PmPlan) -> str | None:
    required = _required_worker_capability(plan)
    current = parse_worker_capability(request.current_worker_capability)
    if required is None or current is None or required == current:
        return None
    return (
        f"Execution capability mismatch: PM selected {required.value} but current worker is {current.value}. "
        f"Requeue on worker:{required.value} before dev/test/review."
    )


def _required_worker_capability(plan: PmPlan) -> WorkerCapability | None:
    return parse_worker_capability(plan.execution_worker_capability)


def _normalize_mixed_demo_target_pm_requeue(
    *,
    plan: PmPlan,
    current_worker_capability: WorkerCapability,
    capture_target_constraints_json: str,
) -> PmPlan:
    if plan.outcome != "requeue" or not plan.demo_requirements:
        return plan
    target = parse_worker_capability(plan.requeue_target) or _required_worker_capability(plan)
    if target is None:
        return plan
    required_platforms_by_target = _capture_target_required_platforms(capture_target_constraints_json)
    demo_platforms: set[str | None] = set()
    for requirement in plan.demo_requirements:
        demo_platforms.add(required_platforms_by_target.get(str(requirement.capture_target), None))
    if len(demo_platforms) <= 1 or target.value not in demo_platforms:
        return plan
    return replace(
        plan,
        outcome="continue",
        execution_worker_capability=current_worker_capability.value,
        blocker_message=None,
        requeue_target=None,
        requeue_reason=None,
    )


def _capture_target_required_platforms(capture_target_constraints_json: str) -> dict[str, str | None]:
    try:
        constraints = json.loads(str(capture_target_constraints_json or "[]"))
    except json.JSONDecodeError:
        return {}
    if not isinstance(constraints, list):
        return {}
    platforms: dict[str, str | None] = {}
    for constraint in constraints:
        if not isinstance(constraint, dict):
            continue
        capture_target = str(constraint.get("capture_target") or "").strip()
        if not capture_target:
            continue
        raw_platform = constraint.get("required_worker_platform")
        platform = str(raw_platform).strip() if raw_platform is not None else None
        platforms[capture_target] = platform or None
    return platforms


def _tracked_worktree_change_paths(repo_dir: str | None) -> tuple[str, ...]:
    repo_path = Path(str(repo_dir or "").strip())
    if not repo_path.exists() or not (repo_path / ".git").exists():
        return ()
    paths: set[str] = set()
    for args in (
        ["git", "diff", "--name-only"],
        ["git", "diff", "--cached", "--name-only"],
    ):
        completed = subprocess.run(
            args,
            cwd=repo_path,
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return ()
        for raw_line in completed.stdout.splitlines():
            line = raw_line.strip()
            if line:
                paths.add(line)
    return tuple(sorted(paths))


def _build_direct_test_dev_result(request: WorkflowRequest) -> DevResult:
    return DevResult(
        change_summary=_direct_test_dev_summary(request),
        pr_url=_trigger_context_pr_url(request),
        outcome="continue",
    )


def _summarize_direct_test_dev_result(request: WorkflowRequest) -> str:
    return _direct_test_dev_summary(request)[0]


def _direct_test_dev_summary(request: WorkflowRequest) -> list[str]:
    diff_paths = _current_head_diff_paths(request.execution_repo_dir, request.base_branch)
    if diff_paths:
        listed_paths = ", ".join(diff_paths[:8])
        if len(diff_paths) > 8:
            listed_paths = f"{listed_paths}, +{len(diff_paths) - 8} more"
        return [
            "PM directed the workflow to start at test using the current published head.",
            f"Current published diff paths selected for validation: {listed_paths}.",
        ]
    return [
        "PM directed the workflow to start at test without a fresh dev attempt.",
    ]


def _current_head_diff_paths(repo_dir: str | None, base_branch: str | None) -> list[str]:
    repo_path = Path(str(repo_dir or "").strip())
    resolved_base_branch = str(base_branch or "").strip()
    if not resolved_base_branch or not repo_path.exists() or not (repo_path / ".git").exists():
        return []
    try:
        completed = subprocess.run(
            ["git", "diff", "--name-only", f"{resolved_base_branch}...HEAD"],
            cwd=repo_path,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return [line.strip() for line in completed.stdout.splitlines() if line.strip()]


def _trigger_context_pr_url(request: WorkflowRequest) -> str | None:
    if not isinstance(request.trigger_context, dict):
        return None
    raw_pr_url = request.trigger_context.get("pr_url")
    normalized_pr_url = str(raw_pr_url or "").strip()
    return normalized_pr_url or None


def _mixed_housekeeping_source_paths(repo_dir: str | None, base_branch: str | None) -> tuple[str, ...]:
    repo_path = Path(str(repo_dir or "").strip())
    normalized_base_branch = str(base_branch or "").strip()
    if not normalized_base_branch or not repo_path.exists() or not (repo_path / ".git").exists():
        return ()
    completed = subprocess.run(
        ["git", "diff", "--name-only", f"{normalized_base_branch}...HEAD"],
        cwd=repo_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ()
    paths = tuple(line.strip() for line in completed.stdout.splitlines() if line.strip())
    if "tasks/lessons.md" not in paths:
        return ()
    if not any(_is_source_path(path) for path in paths):
        return ()
    return ("tasks/lessons.md",)


def _is_source_path(path: str) -> bool:
    normalized = path.strip().lower()
    if not normalized or normalized == "tasks/lessons.md":
        return False
    if normalized.endswith((".md", ".txt", ".json", ".yaml", ".yml", ".lock")):
        return False
    if normalized.startswith("tests/") or "/tests/" in normalized or normalized.endswith("_test.py"):
        return False
    return normalized.endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".swift", ".go", ".rb", ".rs", ".cs"))


def _summarize_unpublished_dev_changes(paths: tuple[str, ...]) -> str:
    listed = ", ".join(paths[:4])
    if len(paths) > 4:
        listed += f", +{len(paths) - 4} more"
    return (
        "Dev stage returned continue with tracked changes still unpublished from the local worktree. "
        f"Commit and publish the validated delta before handoff to test/review. Tracked paths: {listed}."
    )


def _summarize_mixed_housekeeping_source_paths(paths: tuple[str, ...]) -> str:
    listed = ", ".join(paths)
    return (
        "Dev stage mixed repo housekeeping files into the product diff. "
        f"Remove those files from the publication candidate before handoff to test/review. Tracked paths: {listed}."
    )


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
    if result.feedback:
        return result.feedback
    if result.guidance:
        return "; ".join(result.guidance[:2])
    return "Test stage completed."


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
    if result.outcome == "blocked":
        return "Review blocked the workflow."
    if result.outcome == "continue":
        return "Review approved the workflow."
    return "Review requested changes."


def _required_pr_title(request: WorkflowRequest) -> str:
    issue_key = str(request.issue_key or "").strip()
    summary = str(request.issue_summary or "").strip()
    if issue_key and summary:
        return f"{issue_key}: {summary}"
    return issue_key or summary or "Automated implementation"


def _required_pr_body(
    *,
    request: WorkflowRequest,
    plan: PmPlan,
    dev_result: DevResult,
    test_result: TestResult,
    review_result: ReviewResult,
) -> str:
    lines = [
        "## Summary",
        f"- Issue: {request.issue_key}",
    ]
    for item in dev_result.change_summary[:3]:
        lines.append(f"- {item}")
    if review_result.summary:
        lines.append("")
        lines.append("## Review")
        for item in review_result.summary[:3]:
            lines.append(f"- {item}")
    lines.append("")
    lines.append("## Acceptance Criteria")
    for item in plan.acceptance_criteria[:5]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## How To Test")
    for item in test_result.guidance[:5]:
        lines.append(f"- {item}")
    return "\n".join(lines).strip()


def _workflow_outcome_for_stage_outcome(outcome: StageOutcome) -> WorkflowOutcome:
    if outcome == "continue":
        return "success"
    if outcome == "requeue":
        return "requeue"
    if outcome == "waiting_for_input":
        return "waiting_for_input"
    if outcome == "blocked":
        return "blocked"
    return "failed"


def _checkpoint_status_for_outcome(outcome: StageOutcome) -> str:
    if outcome == "continue":
        return "completed"
    if outcome == "waiting_for_input":
        return "waiting_for_input"
    if outcome == "requeue":
        return "requeue"
    return outcome
