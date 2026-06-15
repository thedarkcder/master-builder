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


def _demo_requirement(
    *,
    title: str = "Feature walkthrough",
    acceptance_criterion: str = "Feature works",
    capture_target: str = "browser",
    variants: list[str] | None = None,
) -> DemoRequirement:
    return DemoRequirement(
        title=title,
        acceptance_criterion=acceptance_criterion,
        capture_target=capture_target,  # type: ignore[arg-type]
        variants=variants or ["Repeat action remains safe"],
    )


def _qa_recording(
    *,
    name: str,
    index: int,
    capture_target: str = "browser",
    capture_reference: str = "https://preview.example",
    suffix: str = "webm",
) -> QaRecording:
    return QaRecording(
        name=name,
        artifact_url=f"https://cdn.example/qa-demo-{index}.{suffix}",
        object_key=f"tenant-1/project-1/run-1/qa-demo-{index}.{suffix}",
        capture_target=capture_target,  # type: ignore[arg-type]
        capture_reference=capture_reference,
        content_sha256=f"{index:064x}",
        release_commit_sha="b" * 40,
        release_context_sha256=f"{999:064x}",
    )


def _build_snapshot() -> dict[str, object]:
    snapshot = ExecutionSnapshot.empty()
    plan = PmPlan(
        plan_steps=["Implement feature"],
        acceptance_criteria=["Feature works"],
        risks=["Low risk"],
        demo_requirements=[_demo_requirement()],
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
        workflow_request=SimpleNamespace(
            attempt_number=1,
            start_point_ref=None,
            start_point_sha=None,
            project_demo_capture_targets=("browser",),
        ),
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
            run_requeued_capability_update_fn=None,
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
            demo_requirements=[_demo_requirement()],
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
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Repeat action remains safe", index=2),
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
    assert update_pr_mock.call_args.kwargs["required_recording_counts"] == {"browser": 2}
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


def test_complete_blocks_demo_required_success_when_preview_release_is_not_created() -> None:
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

    qa_mock = MagicMock()
    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(created=False, reason="preview_prs_disabled", release=None),
        ),
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage", qa_mock),
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

    qa_mock.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "preview deployment was not created: preview_prs_disabled" in finalizer_calls["workflow_result"].blocker_message
    deps.execution.persist_stage_checkpoint_fn.assert_not_called()


def test_complete_requeues_demo_required_success_when_preview_release_is_still_deploying() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    deps.execution.requeue_workflow_result_for_stale_snapshot_fn.return_value = SimpleNamespace(status="queued")
    prepared = _prepared(_build_snapshot(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = _workflow_result()
    preview_release = SimpleNamespace(
        release_id="release-preview-1",
        status="deploying",
        service_urls=[SimpleNamespace(service_kind="website", status="pending", url="https://preview.example")],
    )

    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(created=True, reason="created", release=preview_release),
        ),
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage") as qa_mock,
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence") as update_pr_mock,
        patch("orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer") as finalizer_cls,
    ):
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

    assert result.status == "queued"
    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs["checkpoint"]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "requeue"
    assert "waiting for the run preview deployment/release to become live" in checkpoint.summary
    qa_mock.assert_not_called()
    update_pr_mock.assert_not_called()
    finalizer_cls.assert_not_called()
    deps.execution.requeue_workflow_result_for_capability_fn.assert_not_called()
    deps.execution.requeue_workflow_result_for_stale_snapshot_fn.assert_called_once()
    assert deps.execution.requeue_workflow_result_for_stale_snapshot_fn.call_args.kwargs["mark_stale_snapshot"] is False


def test_complete_blocks_demo_required_success_when_preview_release_failed() -> None:
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
                persisted_status="failed",
                last_error=kwargs["workflow_result"].blocker_message,
                persisted_plan=prepared.run.plan,
                event_types=(),
                tail_steps=(),
            )

    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(
                created=False,
                reason="existing",
                release=SimpleNamespace(status="failed", service_urls=[]),
            ),
        ),
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage") as qa_mock,
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence") as update_pr_mock,
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

    qa_mock.assert_not_called()
    update_pr_mock.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "preview release is failed" in finalizer_calls["workflow_result"].blocker_message


def test_complete_blocks_demo_required_success_without_pr_url_before_preview_release() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(_build_snapshot(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = replace(_workflow_result(), pr_url=None)
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
        patch("orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment") as preview_mock,
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage") as qa_mock,
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence") as update_pr_mock,
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

    preview_mock.assert_not_called()
    qa_mock.assert_not_called()
    update_pr_mock.assert_not_called()
    deps.execution.persist_stage_checkpoint_fn.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "requires a PR URL before creating the run preview release" in finalizer_calls["workflow_result"].blocker_message


def test_complete_blocks_demo_required_success_without_pm_demo_plan() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared({}, effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = replace(_workflow_result(), plan=None)
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
        patch("orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment") as preview_mock,
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage") as qa_mock,
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence") as update_pr_mock,
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

    preview_mock.assert_not_called()
    qa_mock.assert_not_called()
    update_pr_mock.assert_not_called()
    deps.execution.persist_stage_checkpoint_fn.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "requires persisted PM demo requirements before creating the run preview release" in str(
        finalizer_calls["workflow_result"].blocker_message
    )


def test_complete_blocks_demo_required_success_without_project_demo_targets() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(_build_snapshot(), effective_policy={"qa_demo_recording_enabled": True})
    prepared.workflow_request.project_demo_capture_targets = ()
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
        patch("orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment") as preview_mock,
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage") as qa_mock,
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence") as update_pr_mock,
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

    preview_mock.assert_not_called()
    qa_mock.assert_not_called()
    update_pr_mock.assert_not_called()
    deps.execution.persist_stage_checkpoint_fn.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "requires project demo capture targets derived from project app metadata" in str(
        finalizer_calls["workflow_result"].blocker_message
    )


def test_complete_requeues_when_qa_demo_stage_needs_remaining_worker_platform() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    plan = PmPlan(
        plan_steps=["Implement feature"],
        acceptance_criteria=["Feature works everywhere"],
        risks=["Low risk"],
        demo_requirements=[
            _demo_requirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="browser",
            ),
            _demo_requirement(
                title="iOS walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="ios",
            ),
        ],
    )
    snapshot = ExecutionSnapshot.empty()
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan)
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="dev",
            attempt=1,
            status="completed",
            summary="Dev ready.",
            dev_result=DevResult(change_summary=["Implemented feature"], pr_url="https://github.com/acme/repo/pull/8"),
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
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
        )
    )
    previous_qa = QaResult(
        summary=["Recorded browser proof"],
        scenarios=[],
        recordings=[
            QaRecording(
                name="Browser walkthrough",
                artifact_url="https://cdn.example/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=f"{1:064x}",
                release_commit_sha="b" * 40,
                release_context_sha256=f"{999:064x}",
            )
        ],
        outcome="requeue",
        feedback="Still requires iOS",
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(stage="qa", attempt=1, status="requeue", summary="Still requires iOS", qa_result=previous_qa)
    )
    prepared = _prepared(snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = replace(_workflow_result(), plan=plan)
    qa_result = replace(previous_qa, feedback="QA demo recording still requires capture target(s): ios")

    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(created=False, reason="existing", release=SimpleNamespace(service_urls=[])),
        ),
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage", return_value=qa_result) as qa_mock,
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence") as update_pr_mock,
    ):
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
    assert checkpoint.status == "requeue"
    assert qa_mock.call_args.kwargs["previous_qa_result"] == previous_qa
    update_pr_mock.assert_not_called()
    deps.execution.requeue_workflow_result_for_capability_fn.assert_called_once()
    assert deps.execution.requeue_workflow_result_for_capability_fn.call_args.kwargs["required_worker_capability"] == "macos"


def test_complete_attaches_pr_evidence_after_accumulated_qa_demo_recordings_finish() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    plan = PmPlan(
        plan_steps=["Implement feature"],
        acceptance_criteria=["Feature works everywhere"],
        risks=["Low risk"],
        demo_requirements=[
            _demo_requirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="browser",
            ),
            _demo_requirement(
                title="iOS walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="ios",
            ),
            _demo_requirement(
                title="Android walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="android",
            ),
        ],
    )
    snapshot = ExecutionSnapshot.empty()
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan)
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="dev",
            attempt=1,
            status="completed",
            summary="Dev ready.",
            dev_result=DevResult(change_summary=["Implemented feature"], pr_url="https://github.com/acme/repo/pull/8"),
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
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
        )
    )
    previous_qa = QaResult(
        summary=["Recorded Linux demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Browser walkthrough", index=1),
            _qa_recording(name="Browser repeat action", index=2),
            _qa_recording(
                name="Android walkthrough",
                index=3,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
            _qa_recording(
                name="Android repeat action",
                index=4,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
        ],
        outcome="requeue",
        feedback="Still requires iOS",
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(stage="qa", attempt=1, status="requeue", summary="Still requires iOS", qa_result=previous_qa)
    )
    final_qa = replace(
        previous_qa,
        summary=["Recorded all demos"],
        recordings=[
            *previous_qa.recordings,
            _qa_recording(
                name="iOS walkthrough",
                index=5,
                capture_target="ios",
                capture_reference="ios-simulator://configured",
                suffix="mp4",
            ),
            _qa_recording(
                name="iOS repeat action",
                index=6,
                capture_target="ios",
                capture_reference="ios-simulator://configured",
                suffix="mp4",
            ),
        ],
        outcome="continue",
        feedback=None,
    )
    prepared = _prepared(snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = replace(_workflow_result(), plan=plan)
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
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage", return_value=final_qa) as qa_mock,
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence", return_value="updated-body") as update_pr_mock,
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
    assert checkpoint.status == "completed"
    assert qa_mock.call_args.kwargs["previous_qa_result"] == previous_qa
    assert [recording.capture_target for recording in update_pr_mock.call_args.kwargs["qa_result"].recordings] == [
        "browser",
        "browser",
        "android",
        "android",
        "ios",
        "ios",
    ]
    assert update_pr_mock.call_args.kwargs["required_capture_targets"] == ("browser", "ios", "android")
    assert update_pr_mock.call_args.kwargs["required_recording_counts"] == {"browser": 2, "ios": 2, "android": 2}
    deps.execution.requeue_workflow_result_for_capability_fn.assert_not_called()
    assert finalizer_calls["workflow_result"].orchestration_stage_trace[-1]["status"] == "completed"


def test_complete_persists_blocked_qa_checkpoint_when_pr_evidence_update_fails() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(_build_snapshot(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Repeat action remains safe", index=2),
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
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage", return_value=qa_result),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            side_effect=RuntimeError("GitHub rejected PR update"),
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

    assert deps.execution.persist_stage_checkpoint_fn.call_count == 2
    first_checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args_list[0].kwargs["checkpoint"]
    final_checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs["checkpoint"]
    assert first_checkpoint.status == "completed"
    assert final_checkpoint.stage == "qa"
    assert final_checkpoint.status == "blocked"
    assert "QA demo evidence PR update failed: RuntimeError: GitHub rejected PR update" in str(
        final_checkpoint.qa_result.blocker_message
    )
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "GitHub rejected PR update" in str(finalizer_calls["workflow_result"].blocker_message)


def test_complete_blocks_when_qa_continue_result_is_missing_required_capture_target_proof() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    plan = PmPlan(
        plan_steps=["Implement feature"],
        acceptance_criteria=["Feature works everywhere"],
        risks=["Low risk"],
        demo_requirements=[
            _demo_requirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="browser",
            ),
            _demo_requirement(
                title="iOS walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="ios",
            ),
            _demo_requirement(
                title="Android walkthrough",
                acceptance_criterion="Feature works everywhere",
                capture_target="android",
            ),
        ],
    )
    snapshot = ExecutionSnapshot.empty()
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan)
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="dev",
            attempt=1,
            status="completed",
            summary="Dev ready.",
            dev_result=DevResult(change_summary=["Implemented feature"], pr_url="https://github.com/acme/repo/pull/8"),
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
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
        )
    )
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Browser walkthrough", index=1),
            _qa_recording(name="Browser repeat action", index=2),
            _qa_recording(
                name="Android walkthrough",
                index=3,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
            _qa_recording(
                name="Android repeat action",
                index=4,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
        ],
        outcome="continue",
    )
    prepared = _prepared(snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = replace(_workflow_result(), plan=plan)
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
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage", return_value=qa_result),
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence") as update_pr_mock,
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
    assert "without proof for required capture target(s): ios" in str(checkpoint.qa_result.blocker_message)
    update_pr_mock.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"


def test_complete_blocks_when_qa_continue_result_has_too_few_variant_recordings() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    plan = PmPlan(
        plan_steps=["Implement feature"],
        acceptance_criteria=["Feature handles happy path and misuse"],
        risks=["Low risk"],
        demo_requirements=[
            DemoRequirement(
                title="Browser walkthrough",
                acceptance_criterion="Feature handles happy path and misuse",
                capture_target="browser",
                variants=["Bad input shows validation", "Repeat action remains safe"],
            )
        ],
    )
    snapshot = ExecutionSnapshot.empty()
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan)
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="dev",
            attempt=1,
            status="completed",
            summary="Dev ready.",
            dev_result=DevResult(change_summary=["Implemented feature"], pr_url="https://github.com/acme/repo/pull/8"),
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
            review_result=ReviewResult(summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"),
        )
    )
    qa_result = QaResult(
        summary=["Recorded one browser demo"],
        scenarios=[],
        recordings=[
            QaRecording(
                name="Browser walkthrough",
                artifact_url="https://cdn.example/qa-demo-1.webm",
                object_key="tenant-1/project-1/run-1/qa-demo-1.webm",
                capture_target="browser",
                capture_reference="https://preview.example",
                content_sha256=f"{101:064x}",
                release_commit_sha="b" * 40,
                release_context_sha256=f"{999:064x}",
            )
        ],
        outcome="continue",
    )
    prepared = _prepared(snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True})
    workflow_result = replace(_workflow_result(), plan=plan)
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
        patch("orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage", return_value=qa_result),
        patch("orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence") as update_pr_mock,
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
    assert "without proof for required capture target(s): browser" in str(checkpoint.qa_result.blocker_message)
    update_pr_mock.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"


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
                _demo_requirement(
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
                content_sha256=f"{102:064x}",
                release_commit_sha="b" * 40,
                release_context_sha256=f"{999:064x}",
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
