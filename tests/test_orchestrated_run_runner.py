import unittest
from dataclasses import replace
from pathlib import Path
import subprocess
import tempfile

from orchestrator.core.runtime.runtime import CodexRuntime
from orchestrator.core.worker.capability_normalization import WorkerCapability
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.orchestrated_run_runner import (
    OrchestratedRunWorkflowExecutor,
)
from orchestrator.core.workflow.runner import (
    DemoRequirement,
    DevResult,
    PmPlan,
    ReviewResult,
    TestResult,
    WorkflowRequest,
    WorkflowStageCheckpoint,
)


class _RuntimeNoop:
    def __call__(
        self,
        _system: str,
        _user: str,
        _working_dir: str | None = None,
        _on_log_line=None,
    ) -> str:
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
        self.test_dev_results: list[DevResult] = []
        self.pm_calls = 0
        self.dev_calls = 0
        self.test_calls = 0
        self.review_calls = 0

    def pm(  # noqa: ANN001
        self,
        request,
        attempt,
        feedback,
        history,
        last_dev_result,
        last_test_result,
        last_review_result,
        capture_target_constraints_json="[]",
    ):
        _ = (
            request,
            attempt,
            feedback,
            history,
            last_dev_result,
            last_test_result,
            last_review_result,
            capture_target_constraints_json,
        )
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
        self.test_dev_results.append(dev_result)
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
        pm_capture_target_constraints_json: str = "[]",
    ) -> OrchestratedRunWorkflowExecutor:
        runtime = CodexRuntime(
            model="gpt-5-codex",
            max_output_tokens=1200,
            command="override",
            _request=_RuntimeNoop(),
        )
        return OrchestratedRunWorkflowExecutor(
            runtime=runtime,
            pm_capture_target_constraints_json=pm_capture_target_constraints_json,
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

    def _init_git_repo(self, repo_dir: str) -> None:
        path = Path(repo_dir)
        subprocess.run(
            ["git", "init"], cwd=path, check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "config", "user.name", "Test User"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
        (path / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", "tracked.txt"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "initial"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "branch", "-M", "main"],
            cwd=path,
            check=True,
            capture_output=True,
            text=True,
        )

    def test_review_needs_changes_loops_back_to_dev_until_approved(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(
                    change_summary=["implemented attempt 1"],
                    pr_url="https://example/pull/1",
                ),
                DevResult(
                    change_summary=["implemented attempt 2"],
                    pr_url="https://example/pull/1",
                ),
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

    def test_dev_continue_fails_when_tracked_changes_remain_unpublished(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            self._init_git_repo(repo_dir)
            Path(repo_dir, "tracked.txt").write_text("changed\n", encoding="utf-8")
            stage_agents = _StubStageAgents(
                plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
                dev_results=[
                    DevResult(
                        change_summary=["implemented"], pr_url="https://example/pull/1"
                    )
                ],
                test_results=[],
                review_results=[],
            )

            result = self._executor(stage_agents).execute(
                replace(
                    self._request(),
                    execution_repo_dir=repo_dir,
                    max_dev_test_review_loops=1,
                )
            )

        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.diagnostics.stage, "dev")
        self.assertIn("tracked changes still unpublished", result.diagnostics.message)
        self.assertEqual(stage_agents.test_calls, 0)
        self.assertEqual(stage_agents.review_calls, 0)

    def test_dev_continue_ignores_untracked_artifacts_when_repo_is_clean(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            self._init_git_repo(repo_dir)
            Path(repo_dir, "artifacts").mkdir()
            Path(repo_dir, "artifacts", "proof.log").write_text(
                "ok\n", encoding="utf-8"
            )
            Path(repo_dir, ".deriveddata").mkdir()
            Path(repo_dir, ".deriveddata", "temp.txt").write_text(
                "cache\n", encoding="utf-8"
            )
            stage_agents = _StubStageAgents(
                plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
                dev_results=[
                    DevResult(
                        change_summary=["implemented"], pr_url="https://example/pull/1"
                    )
                ],
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

            result = self._executor(stage_agents).execute(
                replace(
                    self._request(),
                    execution_repo_dir=repo_dir,
                    max_dev_test_review_loops=1,
                )
            )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(stage_agents.test_calls, 1)
        self.assertEqual(stage_agents.review_calls, 1)

    def test_dev_continue_fails_when_product_diff_includes_tasks_lessons(self) -> None:
        with tempfile.TemporaryDirectory() as repo_dir:
            self._init_git_repo(repo_dir)
            subprocess.run(
                ["git", "checkout", "-b", "feature/MAB-100"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            Path(repo_dir, "Feature.swift").write_text(
                "struct Feature {}\n", encoding="utf-8"
            )
            Path(repo_dir, "tasks").mkdir()
            Path(repo_dir, "tasks", "lessons.md").write_text(
                "lesson\n", encoding="utf-8"
            )
            subprocess.run(
                ["git", "add", "Feature.swift", "tasks/lessons.md"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "mixed scope"],
                cwd=repo_dir,
                check=True,
                capture_output=True,
                text=True,
            )
            stage_agents = _StubStageAgents(
                plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
                dev_results=[
                    DevResult(
                        change_summary=["implemented"], pr_url="https://example/pull/1"
                    )
                ],
                test_results=[],
                review_results=[],
            )

            result = self._executor(stage_agents).execute(
                replace(
                    self._request(),
                    execution_repo_dir=repo_dir,
                    max_dev_test_review_loops=1,
                )
            )

        self.assertEqual(result.outcome, "failed")
        self.assertEqual(result.diagnostics.stage, "dev")
        self.assertIn("mixed repo housekeeping files", result.diagnostics.message)
        self.assertEqual(stage_agents.test_calls, 0)
        self.assertEqual(stage_agents.review_calls, 0)

    def test_stage_checkpoint_hook_emits_durable_stage_artifacts(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(
                    change_summary=["implemented"], pr_url="https://example/pull/3"
                )
            ],
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
            [
                (checkpoint.stage, checkpoint.status, checkpoint.attempt)
                for checkpoint in checkpoints
            ],
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

    def test_review_approved_without_pr_publishes_with_governed_github_tools_when_pr_creation_required(
        self,
    ) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(change_summary=["implemented attempt 1"], pr_url=None)
            ],
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
        tool_calls: list[tuple[str, str, dict[str, object]]] = []

        def _execute_tool(
            _tenant_id,  # noqa: ANN001
            _project_id,  # noqa: ANN001
            _run_id,  # noqa: ANN001
            _issue_key,  # noqa: ANN001
            stage,  # noqa: ANN001
            tool_name,  # noqa: ANN001
            tool_args,  # noqa: ANN001
            _worker_platform,  # noqa: ANN001
        ):
            tool_calls.append((stage, tool_name, dict(tool_args)))
            if tool_name == "github.push_branch":
                return {"branch_name": "feature/MAB-100", "commit_sha": "abc123"}
            if tool_name == "github.open_pr":
                return {"pr_number": 2, "pr_url": "https://example/pull/2"}
            raise AssertionError(f"unexpected tool {tool_name}")

        result = self._executor(stage_agents, execute_tool=_execute_tool).execute(
            replace(self._request(), allow_pr_creation=True)
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(
            stage_agents.dev_feedback,
            [None],
        )
        self.assertEqual(result.pr_url, "https://example/pull/2")
        self.assertEqual(
            [(stage, tool_name) for stage, tool_name, _tool_args in tool_calls],
            [("review", "github.push_branch"), ("review", "github.open_pr")],
        )
        self.assertEqual(tool_calls[0][2]["branch_name"], "feature/MAB-100")
        self.assertEqual(tool_calls[1][2]["head_branch"], "feature/MAB-100")
        self.assertEqual(tool_calls[1][2]["base_branch"], "main")

    def test_review_approved_without_pr_blocks_when_required_pr_base_branch_is_unresolved(
        self,
    ) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(change_summary=["implemented attempt 1"], pr_url=None)
            ],
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
        tool_calls: list[tuple[str, str, dict[str, object]]] = []

        def _execute_tool(
            _tenant_id,  # noqa: ANN001
            _project_id,  # noqa: ANN001
            _run_id,  # noqa: ANN001
            _issue_key,  # noqa: ANN001
            stage,  # noqa: ANN001
            tool_name,  # noqa: ANN001
            tool_args,  # noqa: ANN001
            _worker_platform,  # noqa: ANN001
        ):
            tool_calls.append((stage, tool_name, dict(tool_args)))
            raise AssertionError(
                "PR publication tools should not run without a resolved base branch"
            )

        result = self._executor(stage_agents, execute_tool=_execute_tool).execute(
            replace(
                self._request(),
                allow_pr_creation=True,
                base_branch=None,
                pr_target_branch=None,
            )
        )

        self.assertEqual(result.outcome, "blocked")
        self.assertIn(
            "missing a resolved PR target/base branch", result.blocker_message or ""
        )
        self.assertEqual(tool_calls, [])

    def test_review_approved_without_pr_blocks_when_pr_creation_required_and_tool_executor_missing(
        self,
    ) -> None:
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
            replace(
                self._request(), allow_pr_creation=True, max_dev_test_review_loops=1
            )
        )

        self.assertIsNotNone(result.diagnostics)
        self.assertEqual(result.diagnostics.stage, "review")
        self.assertEqual(result.outcome, "blocked")
        self.assertIn("no governed GitHub tool executor", result.diagnostics.message)

    def test_test_failure_retries_and_emits_feedback_hook(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]),
            dev_results=[
                DevResult(change_summary=["implemented attempt 1"], pr_url=None),
                DevResult(
                    change_summary=["implemented attempt 2"],
                    pr_url="https://example/pull/2",
                ),
            ],
            test_results=[
                TestResult(
                    outcome="failed",
                    guidance=["pytest -q"],
                    feedback="A unit test failed",
                ),
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
            test_feedback_hook=lambda attempt, feedback: captured_feedback.append(
                (attempt, feedback)
            ),
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.attempts, 2)
        self.assertEqual(captured_feedback, [(1, "A unit test failed")])
        self.assertEqual(stage_agents.dev_feedback, [None, "A unit test failed"])

    def test_terminal_blocked_review_stops_without_consuming_remaining_loops(
        self,
    ) -> None:
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
                DevResult(
                    change_summary=["implemented attempt 1"],
                    pr_url="https://example/pull/1",
                ),
                DevResult(
                    change_summary=["implemented attempt 2"],
                    pr_url="https://example/pull/1",
                ),
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
        self.assertEqual(
            stage_agents.dev_feedback, [None, "App still crashes on login"]
        )

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

        result = self._executor(stage_agents).execute(
            replace(self._request(), max_dev_test_review_loops=1)
        )

        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(result.diagnostics.stage, "dev")

    def test_resume_from_dev_uses_persisted_pm_plan_without_rerunning_pm(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["should not be used"],
                acceptance_criteria=["unused"],
                risks=[],
            ),
            dev_results=[
                DevResult(
                    change_summary=["implemented"], pr_url="https://example/pull/9"
                )
            ],
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
            dev_results=[
                DevResult(
                    change_summary=["implemented"], pr_url="https://example/pull/22"
                )
            ],
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
            dev_results=[
                DevResult(
                    change_summary=["App still crashes"],
                    pr_url=None,
                    blocker_message="App still crashes",
                )
            ],
            test_results=[
                TestResult(
                    outcome="failed",
                    guidance=["Fix crash"],
                    feedback="App still crashes",
                )
            ],
            review_results=[],
        )

        result = self._executor(stage_agents).execute(
            replace(self._request(), max_dev_test_review_loops=1)
        )

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
        self.assertEqual(
            result.requeue_reason, "StoreKit validation requires macOS worker"
        )
        self.assertIsNone(result.diagnostics)
        self.assertEqual(result.orchestration_stage_trace[0]["status"], "requeue")

    def test_pm_requeue_for_mixed_demo_targets_continues_until_qa_routing(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["Validate the delivered feature"],
                acceptance_criteria=["Feature is demoed on browser, iOS, and Android"],
                risks=[],
                demo_requirements=[
                    DemoRequirement(
                        title="Browser walkthrough",
                        acceptance_criterion="Feature is demoed on browser, iOS, and Android",
                        capture_target="browser",
                        variants=["Reload still works", "Navigation remains usable"],
                    ),
                    DemoRequirement(
                        title="iOS walkthrough",
                        acceptance_criterion="Feature is demoed on browser, iOS, and Android",
                        capture_target="ios",
                        variants=["Relaunch still works", "Navigation remains usable"],
                    ),
                    DemoRequirement(
                        title="Android walkthrough",
                        acceptance_criterion="Feature is demoed on browser, iOS, and Android",
                        capture_target="android",
                        variants=["Relaunch still works", "Navigation remains usable"],
                    ),
                ],
                outcome="requeue",
                next_stage="test",
                execution_worker_capability="macos",
                requeue_target="macos",
                requeue_reason="iOS simulator validation is part of the ticket acceptance criteria.",
            ),
            dev_results=[],
            test_results=[TestResult(guidance=["npm test"])],
            review_results=[
                ReviewResult(
                    summary=["Approved"],
                    pr_url="https://github.com/acme/repo/pull/8",
                )
            ],
        )
        constraints_json = (
            '[{"capture_target":"browser","provider_available":true,"required_worker_platform":null},'
            '{"capture_target":"ios","provider_available":true,"required_worker_platform":"macos"},'
            '{"capture_target":"android","provider_available":true,"required_worker_platform":"linux"}]'
        )

        result = self._executor(
            stage_agents,
            pm_capture_target_constraints_json=constraints_json,
        ).execute(
            replace(self._request(), current_worker_capability=WorkerCapability.LINUX)
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(stage_agents.dev_calls, 0)
        self.assertEqual(stage_agents.test_calls, 1)
        self.assertEqual(stage_agents.review_calls, 1)
        self.assertEqual(result.plan.execution_worker_capability, "linux")
        self.assertEqual(result.orchestration_stage_trace[0]["status"], "completed")

    def test_pm_missing_evidence_is_advisory_and_does_not_stop_before_dev(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
            ),
            dev_results=[
                DevResult(
                    change_summary=["implemented"], pr_url="https://example/pull/1"
                )
            ],
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
        self.assertEqual(
            result.orchestration_stage_trace[0]["summary"],
            "PM produced 1 execution steps and 1 acceptance criteria.",
        )

    def test_pm_runtime_values_missing_evidence_is_advisory_and_does_not_stop_before_dev(
        self,
    ) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["plan"],
                acceptance_criteria=["ac1"],
                risks=[],
            ),
            dev_results=[
                DevResult(
                    change_summary=["implemented"], pr_url="https://example/pull/2"
                )
            ],
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
        self.assertEqual(
            result.orchestration_stage_trace[0]["summary"],
            "PM produced 1 execution steps and 1 acceptance criteria.",
        )

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
        self.assertIn(
            "Apple developer access is not approved for staging",
            result.diagnostics.message,
        )
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
                    plan=PmPlan(
                        plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]
                    ),
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

    def test_qa_resume_skips_all_model_stages_and_returns_success_for_qa_policy(
        self,
    ) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["unused"], acceptance_criteria=["unused"], risks=[]
            ),
            dev_results=[],
            test_results=[],
            review_results=[],
        )
        checkpoints: list[WorkflowStageCheckpoint] = []

        result = self._executor(stage_agents).execute(
            replace(
                self._request(),
                entry_mode="resume",
                entry_stage="qa",
                checkpoint_kind="execution",
                checkpoint_payload=self._resume_payload(
                    plan=PmPlan(
                        plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]
                    ),
                    dev_result=DevResult(
                        outcome="continue",
                        change_summary=["Implemented QA demo ready indicator"],
                        pr_url="https://example/pull/1",
                    ),
                    test_result=TestResult(
                        outcome="continue",
                        guidance=["pytest -q"],
                        feedback=None,
                    ),
                    review_result=ReviewResult(
                        outcome="continue",
                        summary=["Approved for QA proof"],
                        feedback=None,
                        pr_url="https://example/pull/1",
                    ),
                ),
            ),
            stage_checkpoint_hook=checkpoints.append,
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.pr_url, "https://example/pull/1")
        self.assertEqual(stage_agents.pm_calls, 0)
        self.assertEqual(stage_agents.dev_calls, 0)
        self.assertEqual(stage_agents.test_calls, 0)
        self.assertEqual(stage_agents.review_calls, 0)
        self.assertEqual(
            [checkpoint.stage for checkpoint in checkpoints],
            ["pm", "dev", "test", "review"],
        )
        self.assertEqual(
            [entry["stage"] for entry in result.orchestration_stage_trace],
            ["pm", "dev", "test", "review"],
        )

    def test_review_resume_without_pr_publishes_with_governed_github_tools_when_pr_creation_required(
        self,
    ) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["unused"], acceptance_criteria=["unused"], risks=[]
            ),
            dev_results=[],
            test_results=[],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Looks good after clarification"],
                    feedback=None,
                    pr_url=None,
                )
            ],
        )
        tool_calls: list[tuple[str, str, dict[str, object]]] = []

        def _execute_tool(
            _tenant_id,  # noqa: ANN001
            _project_id,  # noqa: ANN001
            _run_id,  # noqa: ANN001
            _issue_key,  # noqa: ANN001
            stage,  # noqa: ANN001
            tool_name,  # noqa: ANN001
            tool_args,  # noqa: ANN001
            _worker_platform,  # noqa: ANN001
        ):
            tool_calls.append((stage, tool_name, dict(tool_args)))
            if tool_name == "github.push_branch":
                return {"branch_name": "feature/MAB-100", "commit_sha": "abc123"}
            if tool_name == "github.open_pr":
                return {"pr_number": 10, "pr_url": "https://example/pull/10"}
            raise AssertionError(f"unexpected tool {tool_name}")

        result = self._executor(stage_agents, execute_tool=_execute_tool).execute(
            replace(
                self._request(),
                allow_pr_creation=True,
                entry_mode="resume",
                entry_stage="review",
                checkpoint_kind="execution",
                checkpoint_session_id="dev-session-123",
                checkpoint_payload=self._resume_payload(
                    plan=PmPlan(
                        plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]
                    ),
                    dev_result=DevResult(
                        outcome="continue",
                        change_summary=["Implemented onboarding flow"],
                        pr_url=None,
                    ),
                    test_result=TestResult(
                        outcome="continue",
                        guidance=["pytest -q"],
                        feedback=None,
                    ),
                ),
            ),
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(result.pr_url, "https://example/pull/10")
        self.assertEqual(
            [(stage, tool_name) for stage, tool_name, _tool_args in tool_calls],
            [("review", "github.push_branch"), ("review", "github.open_pr")],
        )

    def test_resume_from_test_uses_persisted_dev_result_and_restarts_at_test(
        self,
    ) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["unused"], acceptance_criteria=["unused"], risks=[]
            ),
            dev_results=[],
            test_results=[
                TestResult(outcome="continue", guidance=["pytest -q"], feedback=None)
            ],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Approved after fresh test run"],
                    feedback=None,
                    pr_url="https://example/pull/10",
                )
            ],
        )

        result = self._executor(stage_agents).execute(
            replace(
                self._request(),
                entry_mode="resume",
                entry_stage="test",
                checkpoint_kind="execution",
                checkpoint_session_id="test-session-123",
                checkpoint_payload=self._resume_payload(
                    plan=PmPlan(
                        plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]
                    ),
                    dev_result=DevResult(
                        outcome="continue",
                        change_summary=["Restored entitlement sync"],
                        pr_url="https://example/pull/10",
                    ),
                ),
            ),
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(stage_agents.pm_calls, 0)
        self.assertEqual(stage_agents.dev_calls, 0)
        self.assertEqual(stage_agents.test_calls, 1)
        self.assertEqual(stage_agents.review_calls, 1)

    def test_pm_can_start_fresh_run_at_test_without_running_dev(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["validate published head"],
                acceptance_criteria=["ac1"],
                risks=[],
                next_stage="test",
            ),
            dev_results=[],
            test_results=[
                TestResult(outcome="continue", guidance=["pytest -q"], feedback=None)
            ],
            review_results=[
                ReviewResult(
                    outcome="continue",
                    summary=["Approved after direct validation"],
                    feedback=None,
                    pr_url="https://example/pull/10",
                )
            ],
        )

        result = self._executor(stage_agents).execute(
            replace(
                self._request(),
                trigger_context={"pr_url": "https://example/pull/10"},
            )
        )

        self.assertEqual(result.outcome, "success")
        self.assertEqual(stage_agents.pm_calls, 1)
        self.assertEqual(stage_agents.dev_calls, 0)
        self.assertEqual(stage_agents.test_calls, 1)
        self.assertEqual(stage_agents.review_calls, 1)
        self.assertEqual(
            stage_agents.test_dev_results[0],
            DevResult(
                outcome="continue",
                change_summary=[
                    "PM directed the workflow to start at test without a fresh dev attempt."
                ],
                pr_url="https://example/pull/10",
            ),
        )
        self.assertEqual(
            [entry["stage"] for entry in result.orchestration_stage_trace],
            ["pm", "dev", "test", "review"],
        )

    def test_review_resume_invalid_artifacts_do_not_mark_dev_and_test_completed(
        self,
    ) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["unused"], acceptance_criteria=["unused"], risks=[]
            ),
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
                    plan=PmPlan(
                        plan_steps=["plan"], acceptance_criteria=["ac1"], risks=[]
                    )
                ),
            ),
        )

        self.assertEqual(result.outcome, "blocked")
        self.assertEqual(result.diagnostics.stage, "review")
        self.assertIn(
            "no valid persisted dev/test artifacts", result.diagnostics.message
        )
        self.assertEqual(
            [entry["stage"] for entry in result.orchestration_stage_trace],
            ["pm"],
        )

    def test_resume_from_dev_rejects_invalid_persisted_pm_plan_contract(self) -> None:
        stage_agents = _StubStageAgents(
            plan=PmPlan(
                plan_steps=["unused"], acceptance_criteria=["unused"], risks=[]
            ),
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
