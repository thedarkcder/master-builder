import unittest
from dataclasses import replace

from orchestrator.core.codex_runtime import CodexRuntime
from orchestrator.core.worker_capability_normalization import WorkerCapability
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.orchestrated_run_runner import OrchestratedRunWorkflowExecutor
from orchestrator.core.workflow.runner import (
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
    WorkflowRequest,
    WorkflowStageCheckpoint,
)


class _RuntimeNoop:
    def __call__(self, _system: str, _user: str, _working_dir: str | None = None, _on_log_line=None) -> str:
        return "{}"


class _StubStageAgents:
    def __init__(
        self,
        *,
        plan: PmPlan,
        dev_results: list[DevResult],
        test_results: list[TestResult],
        review_results: list[ReviewResult],
    ) -> None:
        self.plan = plan
        self.dev_results = list(dev_results)
        self.test_results = list(test_results)
        self.review_results = list(review_results)
        self.dev_feedback: list[str | None] = []
        self.pm_calls = 0
        self.dev_calls = 0
        self.test_calls = 0
        self.review_calls = 0

    def pm(self, request, attempt, feedback, history, last_dev_result, last_test_result, last_review_result):  # noqa: ANN001
        _ = (request, attempt, feedback, history, last_dev_result, last_test_result, last_review_result)
        self.pm_calls += 1
        return self.plan

    def dev(self, request, plan, attempt, feedback):  # noqa: ANN001
        _ = (request, plan, attempt)
        self.dev_calls += 1
        self.dev_feedback.append(feedback)
        return self.dev_results.pop(0)

    def test(self, request, plan, dev_result, attempt):  # noqa: ANN001
        _ = (request, plan, dev_result, attempt)
        self.test_calls += 1
        return self.test_results.pop(0)

    def review(self, request, plan, dev_result, test_result, attempt):  # noqa: ANN001
        _ = (request, plan, dev_result, test_result, attempt)
        self.review_calls += 1
        return self.review_results.pop(0)


class OrchestratedRunRunnerTests(unittest.TestCase):
    def _request(self) -> WorkflowRequest:
        return WorkflowRequest(
            tenant_id="tenant-1",
            run_id="run-1",
            issue_key="MAB-100",
            issue_summary="Orchestrated run",
            issue_description="desc",
            max_dev_test_review_loops=2,
            suggested_test_commands=["pytest -q"],
            execution_repo_dir="/tmp/test-repo",
            execution_branch="run/MAB-100/run-1",
            base_branch="main",
            integration_branch="feature/MAB-100",
            pr_target_branch="main",
        )

    def _executor(
        self,
        stage_agents: _StubStageAgents,
        *,
        execute_tool=None,
    ) -> OrchestratedRunWorkflowExecutor:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeNoop(),
        )
        return OrchestratedRunWorkflowExecutor(
            runtime=runtime,
            stage_agents=stage_agents,
            execute_tool=execute_tool,
        )

    def _resume_payload(
        self,
        *,
        plan: PmPlan,
        dev_result: DevResult | None = None,
        test_result: TestResult | None = None,
        review_result: ReviewResult | None = None,
    ) -> dict:
        snapshot = ExecutionSnapshot.empty()
        snapshot.apply_stage_checkpoint(
            WorkflowStageCheckpoint(
                stage="pm",
                attempt=1,
                status="completed",
                summary="PM completed",
                plan=plan,
            )
        )
        if dev_result is not None:
            snapshot.apply_stage_checkpoint(
                WorkflowStageCheckpoint(
                    stage="dev",
                    attempt=1,
                    status="completed",
                    summary="Dev completed",
                    dev_result=dev_result,
                )
            )
        if test_result is not None:
            snapshot.apply_stage_checkpoint(
                WorkflowStageCheckpoint(
                    stage="test",
                    attempt=1,
                    status="completed",
                    summary="Test completed",
                    test_result=test_result,
                )
            )
        if review_result is not None:
            snapshot.apply_stage_checkpoint(
                WorkflowStageCheckpoint(
                    stage="review",
                    attempt=1,
                    status="completed",
                    summary="Review completed",
                    review_result=review_result,
                )
            )
        return snapshot.dump()

    def test_review_needs_changes_loops_back_to_dev_until_approved(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(change_summary=["implemented attempt 1"], pr_url="https://example/pull/1"),
                DevResult(change_summary=["implemented attempt 2"], pr_url="https://example/pull/1"),
            ],
            test_results=[
                TestResult(guidance=["pytest -q"]),
                TestResult(guidance=["pytest -q"]),
            ],
            review_results=[
                ReviewResult(
                    outcome="failed",
                    summary=["Fix Apple nonce handling"],
                    feedback="Fix Apple nonce handling",
                    pr_url="https://example/pull/1",
                ),
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/1",
                ),
            ],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(stage_agents.pm_calls, 1)
        self.assertEqual(stage_agents.dev_calls, 2)
        self.assertEqual(stage_agents.test_calls, 2)
        self.assertEqual(stage_agents.review_calls, 2)
        self.assertEqual(stage_agents.dev_feedback, [None, "Fix Apple nonce handling"])
        self.assertEqual(result.pr_url, "https://example/pull/1")

    def test_stage_checkpoint_hook_emits_durable_stage_artifacts(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[DevResult(change_summary=["implemented"], pr_url="https://example/pull/3")],
            test_results=[TestResult(guidance=["pytest -q"])],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/3",
                )
            ],
        )
        checkpoints: list[WorkflowStageCheckpoint] = []

        result = self._executor(stage_agents).execute(
            self._request(),
            stage_checkpoint_hook=checkpoints.append,
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(
            [(checkpoint.stage, checkpoint.status, checkpoint.attempt) for checkpoint in checkpoints],
            [
                ("pm", "completed", 1),
                ("dev", "completed", 1),
                ("test", "completed", 1),
                ("review", "completed", 1),
            ],
        )
        self.assertEqual(checkpoints[0].plan.plan_steps, ["plan"])
        self.assertEqual(checkpoints[1].dev_result.change_summary, ["implemented"])
        self.assertEqual(checkpoints[2].test_result.outcome, "continue")
        self.assertEqual(checkpoints[3].review_result.outcome, "continue")

    def test_review_approved_without_pr_loops_back_when_pr_creation_required(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(change_summary=["implemented attempt 1"], pr_url=None),
                DevResult(change_summary=["implemented attempt 2"], pr_url="https://example/pull/2"),
            ],
            test_results=[
                TestResult(guidance=["pytest -q"]),
                TestResult(guidance=["pytest -q"]),
            ],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url=None,
                ),
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/2",
                ),
            ],
        )

        result = self._executor(stage_agents).execute(replace(self._request(), allow_pr_creation=True))

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(
            stage_agents.dev_feedback,
            [None, "Review approved the changes, but no PR was created even though allow_pr_creation is enabled."],
        )
        self.assertEqual(result.pr_url, "https://example/pull/2")

    def test_review_approved_without_pr_fails_when_pr_creation_required_and_loops_exhausted(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[DevResult(change_summary=["implemented"], pr_url=None)],
            test_results=[TestResult(guidance=["pytest -q"])],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url=None,
                ),
            ],
        )

        result = self._executor(stage_agents).execute(
            replace(self._request(), allow_pr_creation=True, max_dev_test_review_loops=1)
        )

        self.assertEqual(result.outcome, "failed")
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "review")
        self.assertIn("no PR was created", result.diagnostics.message)

    def test_test_failure_retries_and_emits_feedback_hook(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(change_summary=["implemented attempt 1"], pr_url=None),
                DevResult(change_summary=["implemented attempt 2"], pr_url="https://example/pull/2"),
            ],
            test_results=[
                TestResult(outcome="failed", guidance=["pytest -q"], feedback="A unit test failed"),
                TestResult(guidance=["pytest -q"]),
            ],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/2",
                ),
            ],
        )
        captured_feedback: list[tuple[int, str]] = []

        result = self._executor(stage_agents).execute(
            self._request(),
            test_feedback_hook=lambda attempt, feedback: captured_feedback.append((attempt, feedback)),
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(captured_feedback, [(1, "A unit test failed")])
        self.assertEqual(stage_agents.dev_feedback, [None, "A unit test failed"])

    def test_terminal_blocked_review_stops_without_consuming_remaining_loops(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[DevResult(change_summary=["implemented"], pr_url=None)],
            test_results=[TestResult(guidance=["pytest -q"])],
            review_results=[
                ReviewResult(
                    outcome="blocked",
                    summary=["Governed runtime unavailable"],
                    feedback="Governed runtime unavailable",
                    pr_url=None,
                    blocker_message="Governed runtime unavailable",
                )
            ],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertEqual(result.outcome, "blocked")
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "review")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(stage_agents.dev_calls, 1)
        self.assertEqual(stage_agents.review_calls, 1)

    def test_non_terminal_blocked_review_loops_back_to_dev(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(change_summary=["implemented attempt 1"], pr_url="https://example/pull/1"),
                DevResult(change_summary=["implemented attempt 2"], pr_url="https://example/pull/1"),
            ],
            test_results=[
                TestResult(guidance=["pytest -q"]),
                TestResult(guidance=["pytest -q"]),
            ],
            review_results=[
                ReviewResult(
                    outcome="failed",
                    summary=["App still crashes"],
                    feedback="App still crashes on login",
                    pr_url="https://example/pull/1",
                ),
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/1",
                ),
            ],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(stage_agents.dev_feedback, [None, "App still crashes on login"])

    def test_terminal_dev_blocker_stops_immediately(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(
                    change_summary=["xcodebuild unavailable"],
                    pr_url=None,
                    outcome="blocked",
                    blocker_message="xcodebuild missing",
                )
            ],
            test_results=[],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(replace(self._request(), max_dev_test_review_loops=1))

        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(result.diagnostics.stage, "dev")

    def test_resume_from_dev_uses_persisted_pm_plan_without_rerunning_pm(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["should not be used"], acceptance_criteria=["unused"], risks=[]),
            dev_results=[DevResult(change_summary=["implemented"], pr_url="https://example/pull/9")],
            test_results=[TestResult(guidance=["pytest -q"])],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/9",
                )
            ],
        )

        request = replace(
            self._request(),
            entry_mode="resume",
            entry_stage="dev",
            checkpoint_kind="execution",
            checkpoint_session_id="dev-session-123",
            checkpoint_payload=self._resume_payload(
                plan=PmPlan(
                    plan_steps=["resume from persisted plan"],
                    acceptance_criteria=["ac1"],
                    risks=["risk1"],
                )
            ),
        )

        result = self._executor(stage_agents).execute(request)

        self.assertEqual(result.outcome, "success")
        self.assertEqual(stage_agents.pm_calls, 0)
        self.assertEqual(result.plan.plan_steps, ["resume from persisted plan"])

    def test_resume_from_pm_without_persisted_plan_reruns_pm_stage(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["resume via pm stage"],
                acceptance_criteria=["ac1"],
                risks=["risk1"],
            ),
            dev_results=[DevResult(change_summary=["implemented"], pr_url="https://example/pull/22")],
            test_results=[TestResult(guidance=["pytest -q"])],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/22",
                )
            ],
        )

        request = replace(
            self._request(),
            entry_mode="resume",
            entry_stage="pm",
            checkpoint_kind="pm",
            checkpoint_session_id="pm-session-123",
            checkpoint_payload=ExecutionSnapshot.empty().dump(),
        )

        result = self._executor(stage_agents).execute(request)

        self.assertEqual(result.outcome, "success")
        self.assertEqual(stage_agents.pm_calls, 1)
        self.assertEqual(result.plan.plan_steps, ["resume via pm stage"])

    def test_non_terminal_dev_blocker_message_does_not_stop(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[DevResult(change_summary=["App still crashes"], pr_url=None, blocker_message="App still crashes")],
            test_results=[TestResult(outcome="failed", guidance=["Fix crash"], feedback="App still crashes")],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(replace(self._request(), max_dev_test_review_loops=1))

        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.diagnostics.stage, "test")
        self.assertEqual(stage_agents.test_calls, 1)

    def test_pm_capability_mismatch_returns_requeueable_failure(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
                execution_worker_capability="macos",
            ),
            dev_results=[],
            test_results=[],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(
            replace(
                self._request(),
                current_worker_capability=WorkerCapability.LINUX,
                available_worker_capabilities=(WorkerCapability.LINUX,),
            )
        )

        self.assertEqual(result.outcome, "requeue")
        self.assertEqual(result.requeue_target, WorkerCapability.MACOS)
        self.assertIn("Execution capability mismatch:", result.requeue_reason)
        self.assertEqual(stage_agents.dev_calls, 0)

    def test_pm_requeue_outcome_preserves_target_and_reason(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
                outcome="requeue",
                requeue_target="macos",
                requeue_reason="StoreKit validation requires macOS worker",
                blocker_message="PM produced 1 execution steps and 1 acceptance criteria.",
            ),
            dev_results=[],
            test_results=[],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertEqual(result.outcome, "requeue")
        self.assertEqual(result.requeue_target, WorkerCapability.MACOS)
        self.assertEqual(result.requeue_reason, "StoreKit validation requires macOS worker")
        self.assertIsNone(result.diagnostics)
        self.assertEqual(result.orchestration_stage_trace[0]["status"], "requeue")

    def test_pm_missing_evidence_is_advisory_and_does_not_stop_before_dev(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
            ),
            dev_results=[DevResult(change_summary=["implemented"], pr_url="https://example/pull/1")],
            test_results=[TestResult(guidance=["pytest -q"])],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/1",
                )
            ],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertEqual(result.outcome, "success")
        self.assertEqual(stage_agents.dev_calls, 1)
        self.assertEqual(result.orchestration_stage_trace[0]["summary"], "PM produced 1 execution steps and 1 acceptance criteria.")

    def test_pm_runtime_values_missing_evidence_is_advisory_and_does_not_stop_before_dev(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
            ),
            dev_results=[DevResult(change_summary=["implemented"], pr_url="https://example/pull/2")],
            test_results=[TestResult(guidance=["pytest -q"])],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good"],
                    feedback=None,
                    pr_url="https://example/pull/2",
                )
            ],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertEqual(result.outcome, "success")
        self.assertEqual(stage_agents.dev_calls, 1)
        self.assertEqual(result.orchestration_stage_trace[0]["summary"], "PM produced 1 execution steps and 1 acceptance criteria.")

    def test_pm_confirmed_external_blocker_stops_before_dev(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
                outcome="blocked",
                blocker_message="Apple developer access is not approved for staging",
            ),
            dev_results=[],
            test_results=[],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(self._request())

        self.assertEqual(result.outcome, "blocked")
        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "pm")
        self.assertIn("Apple developer access is not approved for staging", result.diagnostics.message)
        self.assertEqual(stage_agents.dev_calls, 0)

    def test_review_resume_skips_pm_dev_and_test(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[],
            test_results=[],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good after clarification"],
                    feedback=None,
                    pr_url="https://example/pull/1",
                )
            ],
        )

        result = self._executor(stage_agents).execute(
            replace(
                self._request(),
                entry_mode="resume",
                entry_stage="review",
                checkpoint_kind="execution",
                checkpoint_session_id="dev-session-123",
                checkpoint_payload=self._resume_payload(
                    plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
                    dev_result=DevResult(
                        outcome="continue",
                        change_summary=["Implemented onboarding flow"],
                        pr_url="https://example/pull/1",
                    ),
                    test_result=TestResult(
                        outcome="continue",
                        guidance=["pytest -q"],
                        feedback=None,
                    ),
                    review_result=ReviewResult(
                        outcome="failed",
                        summary=["Needs nonce verification"],
                        feedback="Verify nonce handling with the QA account",
                        pr_url="https://example/pull/1",
                    ),
                ),
            ),
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(stage_agents.pm_calls, 0)
        self.assertEqual(stage_agents.dev_calls, 0)
        self.assertEqual(stage_agents.test_calls, 0)
        self.assertEqual(stage_agents.review_calls, 1)

    def test_review_resume_invalid_artifacts_do_not_mark_dev_and_test_completed(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["unused"], acceptance_criteria=["unused"], risks=[]),
            dev_results=[],
            test_results=[],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(
            replace(
                self._request(),
                entry_mode="resume",
                entry_stage="review",
                checkpoint_kind="execution",
                checkpoint_session_id="dev-session-123",
                checkpoint_payload=self._resume_payload(
                    plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[])
                ),
            ),
        )

        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(result.diagnostics.stage, "review")
        self.assertIn("no valid persisted dev/test artifacts", result.diagnostics.message)
        self.assertEqual(
            [entry["stage"] for entry in result.orchestration_stage_trace],
            ["pm"],
        )

    def test_resume_from_dev_rejects_invalid_persisted_pm_plan_contract(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["unused"], acceptance_criteria=["unused"], risks=[]),
            dev_results=[],
            test_results=[],
            review_results=[],
        )

        invalid_checkpoint_payload = self._resume_payload(
            plan=PmPlan(
                plan_steps=["resume from persisted plan"],
                acceptance_criteria=["ac1"],
                risks=["risk1"],
            )
        )
        invalid_checkpoint_payload["stages"]["pm"]["artifact"]["next_stage"] = "ship-it"

        result = self._executor(stage_agents).execute(
            replace(
                self._request(),
                entry_mode="resume",
                entry_stage="dev",
                checkpoint_kind="execution",
                checkpoint_session_id="dev-session-123",
                checkpoint_payload=invalid_checkpoint_payload,
            )
        )

        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(result.diagnostics.stage, "dev")
        self.assertIn("no valid persisted PM plan", result.diagnostics.message)
        self.assertEqual(stage_agents.pm_calls, 0)


if __name__ == "__main__":
    unittest.main()
