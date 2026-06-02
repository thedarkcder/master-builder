from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import (
    DemoRequirement,
    DevResult,
    PmPlan,
    QaRecording,
    QaResult,
    ReviewResult,
    TestResult,
    WorkflowResult,
    WorkflowStageCheckpoint,
)
from orchestrator.core.worker.run_outcome_policy import RunOutcomePolicy


def _build_snapshot() -> dict[str, object]:
    snapshot = ExecutionSnapshot.empty()
    plan = PmPlan(
        plan_steps=["Implement feature"],
        acceptance_criteria=["Feature works"],
        risks=["Low risk"],
        demo_requirements=[DemoRequirement(title="Feature walkthrough", acceptance_criterion="Feature works")],
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan)
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="dev",
            attempt=1,
            status="completed",
            summary="Dev ready.",
            dev_result=DevResult(
                change_summary=["Implemented feature"],
                pr_url="https://github.com/acme/repo/pull/8",
            ),
        )
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="test",
            attempt=1,
            status="completed",
            summary="Tests ready.",
            test_result=TestResult(guidance=["pytest -q"]),
        )
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="review",
            attempt=1,
            status="completed",
            summary="Review ready.",
            review_result=ReviewResult(
                summary=["Looks good"],
                pr_url="https://github.com/acme/repo/pull/8",
            ),
        )
    )
    return snapshot.dump()


def _prepared(run_plan: dict[str, object], *, effective_policy: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(
        run=SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="MAB-400",
            status="running",
            plan=run_plan,
            pr_url="https://github.com/acme/repo/pull/8",
            worker_service_instance_id="worker-1",
            claim_id="claim-1",
        ),
        project=SimpleNamespace(
            project_id="project-1",
            github_repository="https://github.com/acme/repo",
        ),
        tenant=SimpleNamespace(
            tenant_id="tenant-1",
            github_config={},
        ),
        effective_policy=effective_policy,
        worker_service_instance_id="worker-1",
        claim_id="claim-1",
        notifier=SimpleNamespace(stage_updates=[], append=lambda item: None),
        workflow_request=SimpleNamespace(attempt_number=1, start_point_ref=None, start_point_sha=None),
        jira_issue_url="https://jira.example/browse/MAB-400",
        run_dashboard_url="https://admin.example/runs/run-1",
        agent_id="worker-linux-local",
        worker_workspace_key="worker-a",
    )


def _deps() -> SimpleNamespace:
    return SimpleNamespace(
        statuses=SimpleNamespace(running="running", cancelled="cancelled", failed="failed"),
        identity=SimpleNamespace(
            logger=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
        ),
        stage_updates=SimpleNamespace(
            plan_posted_update_fn=None,
            pr_opened_update_fn=None,
            run_failed_update_fn=None,
            run_requeued_stale_snapshot_update_fn=None,
            send_jira_message_fn=MagicMock(),
        ),
        execution=SimpleNamespace(
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=MagicMock(),
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(return_value=SimpleNamespace(stale=False, message=None)),
        ),
    )


def _workflow_result() -> WorkflowResult:
    return WorkflowResult(
        outcome="success",
        plan=PmPlan(
            plan_steps=["Implement feature"],
            acceptance_criteria=["Feature works"],
            risks=["Low risk"],
            demo_requirements=[DemoRequirement(title="Feature walkthrough", acceptance_criterion="Feature works")],
        ),
        pr_url="https://github.com/acme/repo/pull/8",
        summary=["Implemented feature"],
        test_guidance=["pytest -q"],
        attempts=1,
        orchestration_stage_trace=[
            {"stage": "pm", "status": "completed", "attempt": 1, "summary": "PM ready."},
            {"stage": "dev", "status": "completed", "attempt": 1, "summary": "Dev ready."},
            {"stage": "test", "status": "completed", "attempt": 1, "summary": "Tests ready."},
            {"stage": "review", "status": "completed", "attempt": 1, "summary": "Review ready."},
        ],
    )


def test_complete_runs_qa_demo_stage_before_finalization() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(_build_snapshot(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            QaRecording(
                name="Happy path",
                artifact_url="https://cdn.example/qa/happy.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_reference="https://preview.example",
            )
        ],
    )
    finalizer_calls: dict[str, object] = {}

    class _Finalizer:
        def __init__(self, **_kwargs):
            pass

        def finalize(self, **kwargs):
            finalizer_calls.update(kwargs)
            return SimpleNamespace(
                run=prepared.run,
                workflow_result=kwargs["workflow_result"],
                persisted_status="succeeded",
                last_error=None,
                persisted_plan=prepared.run.plan,
                event_types=(),
                tail_steps=(),
            )

    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(created=False, reason="existing", release=SimpleNamespace(service_urls=[])),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ) as update_pr_mock,
        patch("orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer),
        patch("orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor") as tail_executor_cls,
    ):
        tail_executor_cls.return_value.execute.return_value = None
        result = RunOutcomePolicy(
            session=session,
            settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/workdirs"),
            deps=deps,
            cleanup_run_workspaces_safe_fn=MagicMock(),
        ).complete(
            prepared=prepared,
            workflow_result=workflow_result,
            execution_context={"execution_branch": "run/MAB-400/run-1"},
        )

    assert result is prepared.run
    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs["checkpoint"]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "completed"
    assert checkpoint.qa_result == qa_result
    assert update_pr_mock.called
    assert finalizer_calls["workflow_result"].orchestration_stage_trace[-1]["stage"] == "qa"


def test_complete_blocks_success_when_qa_demo_stage_cannot_finish() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(_build_snapshot(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = _workflow_result()
    finalizer_calls: dict[str, object] = {}

    class _Finalizer:
        def __init__(self, **_kwargs):
            pass

        def finalize(self, **kwargs):
            finalizer_calls.update(kwargs)
            return SimpleNamespace(
                run=prepared.run,
                workflow_result=kwargs["workflow_result"],
                persisted_status="blocked",
                last_error=kwargs["workflow_result"].blocker_message,
                persisted_plan=prepared.run.plan,
                event_types=(),
                tail_steps=(),
            )

    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(created=False, reason="existing", release=SimpleNamespace(service_urls=[])),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            side_effect=RuntimeError("upload failed after retries"),
        ),
        patch("orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer),
        patch("orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor") as tail_executor_cls,
    ):
        tail_executor_cls.return_value.execute.return_value = None
        RunOutcomePolicy(
            session=session,
            settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/workdirs"),
            deps=deps,
            cleanup_run_workspaces_safe_fn=MagicMock(),
        ).complete(
            prepared=prepared,
            workflow_result=workflow_result,
            execution_context={"execution_branch": "run/MAB-400/run-1"},
        )

    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs["checkpoint"]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "blocked"
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "upload failed after retries" in finalizer_calls["workflow_result"].blocker_message


def test_complete_creates_preview_release_for_ios_only_demo_requirements() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(_build_snapshot(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = replace(
        _workflow_result(),
        plan=PmPlan(
            plan_steps=["Implement feature"],
            acceptance_criteria=["Feature works on iOS"],
            risks=["Low risk"],
            demo_requirements=[
                DemoRequirement(
                    title="Native walkthrough",
                    acceptance_criterion="Feature works on iOS",
                    capture_target="ios",
                )
            ],
        ),
    )
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            QaRecording(
                name="Native happy path",
                artifact_url="https://cdn.example/qa/native.mp4",
                object_key="tenant-1/project-1/run-1/qa-demo-1.mp4",
                capture_target="ios",
                capture_reference="ios-simulator://girlpower",
            )
        ],
    )

    class _Finalizer:
        def __init__(self, **_kwargs):
            pass

        def finalize(self, **kwargs):
            return SimpleNamespace(
                run=prepared.run,
                workflow_result=kwargs["workflow_result"],
                persisted_status="succeeded",
                last_error=None,
                persisted_plan=prepared.run.plan,
                event_types=(),
                tail_steps=(),
            )

    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(created=False, reason="existing", release=SimpleNamespace(service_urls=[])),
        ) as preview_mock,
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage", return_value=qa_result) as qa_mock,
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence", return_value="updated-body"),
        patch("orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer),
        patch("orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor") as tail_executor_cls,
    ):
        tail_executor_cls.return_value.execute.return_value = None
        RunOutcomePolicy(
            session=session,
            settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/workdirs"),
            deps=deps,
            cleanup_run_workspaces_safe_fn=MagicMock(),
        ).complete(
            prepared=prepared,
            workflow_result=workflow_result,
            execution_context={"execution_branch": "run/MAB-400/run-1"},
        )

    preview_mock.assert_called_once()
    assert qa_mock.call_args.kwargs["preview_release"] is preview_mock.return_value.release


def test_complete_does_not_requeue_when_start_ref_moves_to_run_artifact_commit() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(_build_snapshot(), effective_policy={"qa_demo_recording_enabled": False})
    prepared.workflow_request.start_point_ref = "origin/feature/MAB-400"
    prepared.workflow_request.start_point_sha = "oldsha1234567890"
    deps.execution.check_run_snapshot_freshness_fn.return_value = SimpleNamespace(
        stale=True,
        current_start_point_sha="newsha9876543210",
        message="Branch snapshot stale: origin/feature/MAB-400 moved from oldsha123456 to newsha987654. Requeueing from the latest snapshot.",
    )
    finalizer_calls: dict[str, object] = {}

    class _Finalizer:
        def __init__(self, **_kwargs):
            pass

        def finalize(self, **kwargs):
            finalizer_calls.update(kwargs)
            return SimpleNamespace(
                run=prepared.run,
                workflow_result=kwargs["workflow_result"],
                persisted_status="succeeded",
                last_error=None,
                persisted_plan=prepared.run.plan,
                event_types=(),
                tail_steps=(),
            )

    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.latest_pushed_execution_artifact_for_run",
            return_value=SimpleNamespace(commit_sha="newsha9876543210"),
        ),
        patch("orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer),
        patch("orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor") as tail_executor_cls,
    ):
        tail_executor_cls.return_value.execute.return_value = None
        result = RunOutcomePolicy(
            session=session,
            settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/workdirs"),
            deps=deps,
            cleanup_run_workspaces_safe_fn=MagicMock(),
        ).complete(
            prepared=prepared,
            workflow_result=_workflow_result(),
            execution_context={"execution_branch": "run/MAB-400/run-1"},
        )

    assert result is prepared.run
    deps.execution.requeue_workflow_result_for_stale_snapshot_fn.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "success"


def test_complete_requeues_when_start_ref_moves_to_different_commit_than_run_artifact() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(_build_snapshot(), effective_policy={"qa_demo_recording_enabled": False})
    prepared.workflow_request.start_point_ref = "origin/feature/MAB-400"
    prepared.workflow_request.start_point_sha = "oldsha1234567890"
    deps.execution.check_run_snapshot_freshness_fn.return_value = SimpleNamespace(
        stale=True,
        current_start_point_sha="newsha9876543210",
        message="Branch snapshot stale: origin/feature/MAB-400 moved from oldsha123456 to newsha987654. Requeueing from the latest snapshot.",
    )
    cleanup_mock = MagicMock()
    deps.execution.requeue_workflow_result_for_stale_snapshot_fn.return_value = "requeued"

    with patch(
        "orchestrator.core.worker.run_outcome_policy.latest_pushed_execution_artifact_for_run",
        return_value=SimpleNamespace(commit_sha="artifactsha111111"),
    ):
        result = RunOutcomePolicy(
            session=session,
            settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/workdirs"),
            deps=deps,
            cleanup_run_workspaces_safe_fn=cleanup_mock,
        ).complete(
            prepared=prepared,
            workflow_result=_workflow_result(),
            execution_context={"execution_branch": "run/MAB-400/run-1"},
        )

    assert result == "requeued"
    cleanup_mock.assert_called_once()
    deps.execution.requeue_workflow_result_for_stale_snapshot_fn.assert_called_once()
