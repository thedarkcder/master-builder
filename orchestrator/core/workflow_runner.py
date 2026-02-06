from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from collections.abc import Callable
from typing import Protocol


@dataclass(frozen=True)
class WorkflowRequest:
    tenant_id: str
    run_id: str
    issue_key: str
    issue_summary: str
    issue_description: str
    max_dev_test_review_loops: int
    max_runtime_minutes: int = 30
    suggested_test_commands: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PmPlan:
    plan_steps: list[str]
    acceptance_criteria: list[str]
    risks: list[str]


@dataclass(frozen=True)
class DevResult:
    change_summary: list[str]
    pr_url: str | None


@dataclass(frozen=True)
class TestResult:
    passed: bool
    guidance: list[str]
    feedback: str | None = None


@dataclass(frozen=True)
class ReviewResult:
    approved: bool
    summary: list[str]
    feedback: str | None = None
    pr_url: str | None = None


@dataclass(frozen=True)
class WorkflowDiagnostics:
    stage: str
    message: str
    attempts: int
    history: list[dict[str, str]]


@dataclass(frozen=True)
class WorkflowResult:
    succeeded: bool
    plan: PmPlan | None
    pr_url: str | None
    summary: list[str]
    test_guidance: list[str]
    attempts: int
    diagnostics: WorkflowDiagnostics | None = None

    def to_plan_payload(self) -> dict:
        payload = {
            "attempts": self.attempts,
            "succeeded": self.succeeded,
            "summary": self.summary,
            "test_guidance": self.test_guidance,
            "pr_url": self.pr_url,
        }
        if self.plan is not None:
            payload["plan"] = asdict(self.plan)
        if self.diagnostics is not None:
            payload["diagnostics"] = asdict(self.diagnostics)
        return payload


class WorkflowAgents(Protocol):
    def pm(self, request: WorkflowRequest) -> PmPlan:
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


class WorkflowRunner:
    def __init__(
        self,
        agents: WorkflowAgents,
        *,
        monotonic_fn: Callable[[], float] | None = None,
    ):
        self._agents = agents
        self._monotonic = monotonic_fn or time.monotonic

    def run(self, request: WorkflowRequest) -> WorkflowResult:
        history: list[dict[str, str]] = []
        max_attempts = max(1, request.max_dev_test_review_loops)
        max_runtime_seconds = max(1, request.max_runtime_minutes) * 60
        started_at = self._monotonic()

        runtime_failure = self._runtime_failure(
            request=request,
            started_at=started_at,
            max_runtime_seconds=max_runtime_seconds,
            history=history,
            attempts=0,
        )
        if runtime_failure is not None:
            return runtime_failure

        try:
            plan = self._agents.pm(request)
        except Exception as exc:  # pragma: no cover - exercised via tests
            return self._failure(
                plan=None,
                stage="pm",
                message=f"PM stage failed: {exc}",
                attempts=0,
                history=history,
            )

        runtime_failure = self._runtime_failure(
            request=request,
            started_at=started_at,
            max_runtime_seconds=max_runtime_seconds,
            history=history,
            attempts=0,
        )
        if runtime_failure is not None:
            return runtime_failure

        feedback: str | None = None

        for attempt in range(1, max_attempts + 1):
            runtime_failure = self._runtime_failure(
                request=request,
                started_at=started_at,
                max_runtime_seconds=max_runtime_seconds,
                history=history,
                attempts=attempt,
            )
            if runtime_failure is not None:
                return runtime_failure

            try:
                dev_result = self._agents.dev(request, plan, attempt, feedback)
            except Exception as exc:  # pragma: no cover - exercised via tests
                history.append(
                    {"stage": "dev", "attempt": str(attempt), "event": f"exception:{exc}"}
                )
                return self._failure(
                    plan=plan,
                    stage="dev",
                    message=f"Dev stage failed: {exc}",
                    attempts=attempt,
                    history=history,
                )

            runtime_failure = self._runtime_failure(
                request=request,
                started_at=started_at,
                max_runtime_seconds=max_runtime_seconds,
                history=history,
                attempts=attempt,
            )
            if runtime_failure is not None:
                return runtime_failure

            try:
                test_result = self._agents.test(request, plan, dev_result, attempt)
            except Exception as exc:  # pragma: no cover - exercised via tests
                history.append(
                    {"stage": "test", "attempt": str(attempt), "event": f"exception:{exc}"}
                )
                return self._failure(
                    plan=plan,
                    stage="test",
                    message=f"Test stage failed: {exc}",
                    attempts=attempt,
                    history=history,
                )

            runtime_failure = self._runtime_failure(
                request=request,
                started_at=started_at,
                max_runtime_seconds=max_runtime_seconds,
                history=history,
                attempts=attempt,
            )
            if runtime_failure is not None:
                return runtime_failure

            if not test_result.passed:
                feedback = test_result.feedback or "Tests failed with no feedback"
                history.append({"stage": "test", "attempt": str(attempt), "event": feedback})
                if attempt >= max_attempts:
                    return self._failure(
                        plan=plan,
                        stage="test",
                        message="Max workflow attempts reached after test failures",
                        attempts=attempt,
                        history=history,
                    )
                continue

            try:
                review_result = self._agents.review(
                    request,
                    plan,
                    dev_result,
                    test_result,
                    attempt,
                )
            except Exception as exc:  # pragma: no cover - exercised via tests
                history.append(
                    {"stage": "review", "attempt": str(attempt), "event": f"exception:{exc}"}
                )
                return self._failure(
                    plan=plan,
                    stage="review",
                    message=f"Review stage failed: {exc}",
                    attempts=attempt,
                    history=history,
                )

            runtime_failure = self._runtime_failure(
                request=request,
                started_at=started_at,
                max_runtime_seconds=max_runtime_seconds,
                history=history,
                attempts=attempt,
            )
            if runtime_failure is not None:
                return runtime_failure

            if not review_result.approved:
                feedback = review_result.feedback or "Review requested changes"
                history.append({"stage": "review", "attempt": str(attempt), "event": feedback})
                if attempt >= max_attempts:
                    return self._failure(
                        plan=plan,
                        stage="review",
                        message="Max workflow attempts reached after review feedback",
                        attempts=attempt,
                        history=history,
                    )
                continue

            pr_url = dev_result.pr_url or review_result.pr_url
            if not pr_url:
                history.append(
                    {
                        "stage": "review",
                        "attempt": str(attempt),
                        "event": "missing_pr_url",
                    }
                )
                return self._failure(
                    plan=plan,
                    stage="review",
                    message="Workflow succeeded but no PR URL was produced",
                    attempts=attempt,
                    history=history,
                )

            return WorkflowResult(
                succeeded=True,
                plan=plan,
                pr_url=pr_url,
                summary=review_result.summary or dev_result.change_summary,
                test_guidance=test_result.guidance,
                attempts=attempt,
            )

        return self._failure(
            plan=plan,
            stage="runner",
            message="Workflow stopped before producing a terminal result",
            attempts=max_attempts,
            history=history,
        )

    def _failure(
        self,
        *,
        plan: PmPlan | None,
        stage: str,
        message: str,
        attempts: int,
        history: list[dict[str, str]],
    ) -> WorkflowResult:
        return WorkflowResult(
            succeeded=False,
            plan=plan,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=attempts,
            diagnostics=WorkflowDiagnostics(
                stage=stage,
                message=message,
                attempts=attempts,
                history=history,
            ),
        )

    def _runtime_failure(
        self,
        *,
        request: WorkflowRequest,
        started_at: float,
        max_runtime_seconds: int,
        history: list[dict[str, str]],
        attempts: int,
    ) -> WorkflowResult | None:
        elapsed_seconds = self._monotonic() - started_at
        if elapsed_seconds <= max_runtime_seconds:
            return None

        history.append(
            {
                "stage": "runtime",
                "attempt": str(attempts),
                "event": f"exceeded_{request.max_runtime_minutes}_minutes",
            }
        )
        return self._failure(
            plan=None,
            stage="runtime",
            message=f"Run exceeded max runtime of {request.max_runtime_minutes} minute(s)",
            attempts=attempts,
            history=history,
        )
