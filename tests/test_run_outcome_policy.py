from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import (
    DemoRequirement,
    DevResult,
    PmPlan,
    QaFailureEvidence,
    QaRecording,
    QaResult,
    ReviewResult,
    TestResult,
    WorkflowResult,
    WorkflowStageCheckpoint,
)
from orchestrator.core.worker.run_outcome_policy import RunOutcomePolicy


@pytest.fixture(autouse=True)
def _stub_artifact_url_checks(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        "orchestrator.core.worker.run_outcome_policy.ensure_artifact_url_reachable",
        lambda *_args, **_kwargs: None,
    )


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
        variants=variants
        or ["Invalid input is rejected", "Repeat action remains safe"],
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


def _qa_failure_evidence(
    *,
    name: str = "App load",
    index: int = 1,
    capture_target: str = "browser",
    capture_reference: str = "https://preview.example",
) -> QaFailureEvidence:
    return QaFailureEvidence(
        name=name,
        artifact_url=f"https://cdn.example/qa-failure-{index}.webm",
        object_key=f"tenant-1/project-1/run-1/qa-failure-{index}.webm",
        capture_target=capture_target,  # type: ignore[arg-type]
        capture_reference=capture_reference,
        error_message="QA Demo Ready was not visible\nBrowser diagnostics:\npageerror: process is not defined",
        content_sha256=f"{100 + index:064x}",
        release_commit_sha="b" * 40,
        release_context_sha256=f"{999:064x}",
    )


def _assert_iso_created_at(value: object) -> None:
    assert isinstance(value, str)
    assert datetime.fromisoformat(value).tzinfo is not None


def test_pr_evidence_attach_failed_metadata_requires_explicit_checked_artifact_urls() -> (
    None
):
    from orchestrator.core.worker.run_outcome_policy import (
        _qa_demo_proof_event_metadata,
    )

    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[_qa_recording(name="Happy path", index=1)],
    )
    proof_context = SimpleNamespace(
        pr_url="https://github.com/acme/repo/pull/8",
        qa_result=qa_result,
        pr_evidence_failure_message="QA demo evidence PR update failed: RuntimeError: URL probe failed",
    )

    metadata = _qa_demo_proof_event_metadata(
        proof_context=proof_context, event="PREvidenceAttachFailed"
    )

    assert metadata is not None
    assert metadata["artifact_urls"] == ["https://cdn.example/qa-demo-1.webm"]
    assert (
        metadata["error_message"]
        == "QA demo evidence PR update failed: RuntimeError: URL probe failed"
    )
    assert "artifact_url_check_status" not in metadata
    assert "checked_artifact_urls" not in metadata


def _build_snapshot() -> dict[str, object]:
    snapshot = ExecutionSnapshot.empty()
    plan = PmPlan(
        plan_steps=["Implement feature"],
        acceptance_criteria=["Feature works"],
        risks=["Low risk"],
        demo_requirements=[_demo_requirement()],
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan
        )
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


def _prepared(
    run_plan: dict[str, object], *, effective_policy: dict[str, object]
) -> SimpleNamespace:
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
        statuses=SimpleNamespace(
            running="running", cancelled="cancelled", failed="failed"
        ),
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
            check_run_snapshot_freshness_fn=MagicMock(
                return_value=SimpleNamespace(stale=False, message=None)
            ),
            start_demo_proof_workflow_fn=MagicMock(
                return_value=SimpleNamespace(
                    workflow_id="demo_proof:run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    status="waiting_for_input",
                )
            ),
            advance_demo_proof_workflow_event_fn=MagicMock(
                return_value=SimpleNamespace(
                    workflow_id="demo_proof:run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                    status="completed",
                )
            ),
            mark_pull_request_ready_after_demo_proof_fn=MagicMock(),
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
            {
                "stage": "pm",
                "status": "completed",
                "attempt": 1,
                "summary": "PM ready.",
            },
            {
                "stage": "dev",
                "status": "completed",
                "attempt": 1,
                "summary": "Dev ready.",
            },
            {
                "stage": "test",
                "status": "completed",
                "attempt": 1,
                "summary": "Tests ready.",
            },
            {
                "stage": "review",
                "status": "completed",
                "attempt": 1,
                "summary": "Review ready.",
            },
        ],
    )


def test_complete_runs_qa_demo_stage_before_finalization() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
        ],
    )
    preview_release = SimpleNamespace(
        release_id="release-preview-1",
        release_kind="run_preview",
        status="live",
        service_urls=[
            SimpleNamespace(
                service_kind="website",
                service_name="web",
                service_key="web",
                status="active",
                url="https://preview.example",
                internal_url="http://127.0.0.1:8088",
                host="preview.example",
            )
        ],
        commit_sha="b" * 40,
        provider_context={
            "application_uuid": "app-preview-1",
            "deployment_uuid": "deployment-preview-1",
        },
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "demo-proof-lease:run:run-1:" + "b" * 40 + ":" + "b" * 40,
                "proof_scope_id": "run:run-1:" + "b" * 40,
                "commit_sha": "b" * 40,
                "state": "destroyed",
                "acquired_at": "2026-06-18T11:00:00+00:00",
                "expires_at": "2026-06-19T11:00:00+00:00",
                "destroy_reason": "qa_demo_complete",
                "destroyed_at": "2026-06-18T12:00:00+00:00",
            }
        },
    )
    finalizer_calls: dict[str, object] = {}

    def _destroy_preview(**_kwargs):  # noqa: ANN001
        preview_release.status = "destroyed"
        return preview_release

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
            return_value=SimpleNamespace(
                created=False, reason="existing", release=preview_release
            ),
        ) as create_preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            side_effect=_destroy_preview,
        ) as destroy_preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    assert create_preview_mock.call_args.kwargs["demo_proof_lease_required"] is True
    assert callable(
        create_preview_mock.call_args.kwargs["demo_proof_lease_acquired_fn"]
    )
    deps.execution.start_demo_proof_workflow_fn.assert_called_once()
    assert deps.execution.start_demo_proof_workflow_fn.call_args.kwargs[
        "proof_scope_id"
    ] == ("run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    assert (
        deps.execution.start_demo_proof_workflow_fn.call_args.kwargs["commit_sha"]
        == "b" * 40
    )
    assert deps.execution.start_demo_proof_workflow_fn.call_args.kwargs[
        "required_recording_counts"
    ] == {"browser": 3}
    event_calls = deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    assert [call.kwargs["event"] for call in event_calls] == [
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationPassed",
        "RecordingStarted",
        "RecordingCompleted",
        "EvidenceUploadStarted",
        "EvidenceUploaded",
        "PREvidenceAttachStarted",
        "PREvidenceAttached",
        "PreviewCleanupRequested",
        "PreviewCleanupCompleted",
    ]
    assert all(
        call.kwargs["required_recording_counts"] == {"browser": 3}
        for call in event_calls
    )
    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata") for call in event_calls
    }
    assert metadata_by_event["ReleaseLive"]["release_id"] == "release-preview-1"
    assert metadata_by_event["ReleaseLive"]["demo_proof_lease"] == {
        "lease_id": "demo-proof-lease:run:run-1:" + "b" * 40 + ":" + "b" * 40,
        "proof_scope_id": "run:run-1:" + "b" * 40,
        "commit_sha": "b" * 40,
        "state": "destroyed",
        "acquired_at": "2026-06-18T11:00:00+00:00",
        "expires_at": "2026-06-19T11:00:00+00:00",
        "destroy_reason": "qa_demo_complete",
        "destroyed_at": "2026-06-18T12:00:00+00:00",
    }
    assert metadata_by_event["RecordingCompleted"]["recording_count"] == 3
    assert metadata_by_event["ServiceVerificationPassed"]["required_service_kinds"] == [
        "website"
    ]
    assert metadata_by_event["ServiceVerificationPassed"]["service_urls"] == [
        {
            "service_kind": "website",
            "url": "https://preview.example",
            "recording_url": "http://127.0.0.1:8088",
            "recording_host_header": "preview.example",
            "status": "active",
            "service_name": "web",
            "service_key": "web",
        }
    ]
    assert metadata_by_event["EvidenceUploaded"]["artifact_urls"] == [
        "https://cdn.example/qa-demo-1.webm",
        "https://cdn.example/qa-demo-2.webm",
        "https://cdn.example/qa-demo-3.webm",
    ]
    uploaded_recordings = metadata_by_event["EvidenceUploaded"]["recordings"]
    assert len(uploaded_recordings) == 3
    for uploaded_recording in uploaded_recordings:
        _assert_iso_created_at(uploaded_recording["created_at"])
    assert uploaded_recordings == [
        {
            "recording_name": "Happy path",
            "capture_target": "browser",
            "artifact_url": "https://cdn.example/qa-demo-1.webm",
            "object_key": "tenant-1/project-1/run-1/qa-demo-1.webm",
            "capture_reference": "https://preview.example",
            "created_at": uploaded_recordings[0]["created_at"],
            "content_sha256": f"{1:064x}",
            "release_commit_sha": "b" * 40,
            "release_context_sha256": f"{999:064x}",
        },
        {
            "recording_name": "Invalid input is rejected",
            "capture_target": "browser",
            "artifact_url": "https://cdn.example/qa-demo-2.webm",
            "object_key": "tenant-1/project-1/run-1/qa-demo-2.webm",
            "capture_reference": "https://preview.example",
            "created_at": uploaded_recordings[1]["created_at"],
            "content_sha256": f"{2:064x}",
            "release_commit_sha": "b" * 40,
            "release_context_sha256": f"{999:064x}",
        },
        {
            "recording_name": "Repeat action remains safe",
            "capture_target": "browser",
            "artifact_url": "https://cdn.example/qa-demo-3.webm",
            "object_key": "tenant-1/project-1/run-1/qa-demo-3.webm",
            "capture_reference": "https://preview.example",
            "created_at": uploaded_recordings[2]["created_at"],
            "content_sha256": f"{3:064x}",
            "release_commit_sha": "b" * 40,
            "release_context_sha256": f"{999:064x}",
        },
    ]
    assert metadata_by_event["PREvidenceAttached"]["pr_url"] == workflow_result.pr_url
    assert metadata_by_event["PREvidenceAttached"]["artifact_urls"] == [
        "https://cdn.example/qa-demo-1.webm",
        "https://cdn.example/qa-demo-2.webm",
        "https://cdn.example/qa-demo-3.webm",
    ]
    assert (
        metadata_by_event["PREvidenceAttached"]["artifact_url_check_status"] == "passed"
    )
    assert metadata_by_event["PREvidenceAttached"]["checked_artifact_urls"] == [
        "https://cdn.example/qa-demo-1.webm",
        "https://cdn.example/qa-demo-2.webm",
        "https://cdn.example/qa-demo-3.webm",
    ]
    assert (
        metadata_by_event["PREvidenceAttached"]["pr_body_sha256"]
        == hashlib.sha256("updated-body".encode("utf-8")).hexdigest()
    )
    assert (
        metadata_by_event["PreviewCleanupCompleted"]["release_id"]
        == "release-preview-1"
    )
    assert metadata_by_event["PreviewCleanupCompleted"]["cleanup_status"] == "completed"
    assert (
        metadata_by_event["PreviewCleanupCompleted"]["cleanup_mode"] == "destroy_or_ttl"
    )
    assert metadata_by_event["PreviewCleanupCompleted"]["cleanup_evidence"] == {
        "release_id": "release-preview-1",
        "lease_id": "demo-proof-lease:run:run-1:" + "b" * 40 + ":" + "b" * 40,
        "proof_scope_id": "run:run-1:" + "b" * 40,
        "commit_sha": "b" * 40,
        "cleanup_status": "completed",
        "cleanup_mode": "qa_demo_complete",
        "lease_state": "destroyed",
        "acquired_at": "2026-06-18T11:00:00+00:00",
        "expires_at": "2026-06-19T11:00:00+00:00",
        "destroy_reason": "qa_demo_complete",
        "destroyed_at": "2026-06-18T12:00:00+00:00",
        "resource_refs": [
            {
                "resource_type": "release",
                "resource_id": "release-preview-1",
                "cleanup_action": "destroyed",
            },
            {
                "resource_type": "coolify_application",
                "resource_id": "app-preview-1",
                "cleanup_action": "destroyed",
            },
            {
                "resource_type": "coolify_deployment",
                "resource_id": "deployment-preview-1",
                "cleanup_action": "destroyed",
            },
            {
                "resource_type": "service_url",
                "resource_id": "https://preview.example",
                "cleanup_action": "destroyed",
                "service_kind": "website",
                "service_key": "web",
            },
        ],
    }
    deps.execution.mark_pull_request_ready_after_demo_proof_fn.assert_called_once()
    ready_kwargs = (
        deps.execution.mark_pull_request_ready_after_demo_proof_fn.call_args.kwargs
    )
    assert ready_kwargs["session"] is session
    assert ready_kwargs["tenant"] is prepared.tenant
    assert ready_kwargs["project"] is prepared.project
    assert ready_kwargs["workflow_result"] is workflow_result
    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "completed"
    assert checkpoint.qa_result == qa_result
    assert update_pr_mock.called
    assert update_pr_mock.call_args.kwargs["required_recording_counts"] == {
        "browser": 3
    }
    destroy_preview_mock.assert_called_once_with(
        session=session,
        tenant_id="tenant-1",
        project_id="project-1",
        release_id="release-preview-1",
        reason="qa_demo_complete",
    )
    assert (
        finalizer_calls["workflow_result"].orchestration_stage_trace[-1]["stage"]
        == "qa"
    )


def test_complete_blocks_ready_for_review_until_demo_proof_reports_completed() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
        ],
    )
    preview_release = SimpleNamespace(
        release_id="release-preview-1",
        release_kind="run_preview",
        status="live",
        service_urls=[
            SimpleNamespace(
                service_kind="website",
                service_name="web",
                service_key="web",
                status="active",
                url="https://preview.example",
                internal_url="http://127.0.0.1:8088",
                host="preview.example",
            )
        ],
        commit_sha="b" * 40,
        provider_context={
            "application_uuid": "app-preview-1",
            "deployment_uuid": "deployment-preview-1",
        },
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "demo-proof-lease:run:run-1:" + "b" * 40 + ":" + "b" * 40,
                "proof_scope_id": "run:run-1:" + "b" * 40,
                "commit_sha": "b" * 40,
                "state": "destroyed",
                "acquired_at": "2026-06-18T11:00:00+00:00",
                "expires_at": "2026-06-19T11:00:00+00:00",
                "destroy_reason": "qa_demo_complete",
                "destroyed_at": "2026-06-18T12:00:00+00:00",
            }
        },
    )
    finalizer_calls: dict[str, object] = {}

    def _destroy_preview(**_kwargs):  # noqa: ANN001
        preview_release.status = "destroyed"
        return preview_release

    def _advance_demo_proof_event(**kwargs):  # noqa: ANN202
        event = str(kwargs["event"])
        return SimpleNamespace(
            workflow_id="demo_proof:run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            status="waiting_for_input"
            if event == "PreviewCleanupCompleted"
            else "completed",
            event=event,
        )

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

    deps.execution.advance_demo_proof_workflow_event_fn.side_effect = (
        _advance_demo_proof_event
    )
    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(
                created=False, reason="existing", release=preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            _destroy_preview,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    deps.execution.mark_pull_request_ready_after_demo_proof_fn.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "PreviewCleanupCompleted" in str(
        finalizer_calls["workflow_result"].blocker_message
    )
    assert "waiting_for_input" in str(
        finalizer_calls["workflow_result"].blocker_message
    )


def test_complete_uses_destroyed_release_returned_by_cleanup_for_demo_proof_cleanup_evidence() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
        ],
    )
    live_preview_release = SimpleNamespace(
        release_id="release-preview-1",
        release_kind="run_preview",
        status="live",
        service_urls=[],
        commit_sha="b" * 40,
        provider_context={
            "application_uuid": "app-preview-live",
            "deployment_uuid": "deployment-preview-live",
        },
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "demo-proof-lease:run:run-1:" + "b" * 40 + ":" + "b" * 40,
                "proof_scope_id": "run:run-1:" + "b" * 40,
                "commit_sha": "b" * 40,
                "state": "active",
                "acquired_at": "2026-06-18T11:00:00+00:00",
                "expires_at": "2026-06-19T11:00:00+00:00",
            }
        },
    )
    destroyed_preview_release = SimpleNamespace(
        release_id="release-preview-1",
        release_kind="run_preview",
        status="destroyed",
        service_urls=[],
        commit_sha="b" * 40,
        provider_context={
            "application_uuid": "app-preview-destroyed",
            "deployment_uuid": "deployment-preview-destroyed",
        },
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "demo-proof-lease:run:run-1:" + "b" * 40 + ":" + "b" * 40,
                "proof_scope_id": "run:run-1:" + "b" * 40,
                "commit_sha": "b" * 40,
                "state": "destroyed",
                "acquired_at": "2026-06-18T11:00:00+00:00",
                "expires_at": "2026-06-19T11:00:00+00:00",
                "destroy_reason": "qa_demo_complete",
                "destroyed_at": "2026-06-18T12:00:00+00:00",
            }
        },
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
            return_value=SimpleNamespace(
                created=False, reason="existing", release=live_preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            return_value=destroyed_preview_release,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata")
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    }
    cleanup_evidence = metadata_by_event["PreviewCleanupCompleted"]["cleanup_evidence"]
    assert cleanup_evidence["release_id"] == "release-preview-1"
    assert cleanup_evidence["cleanup_status"] == "completed"
    assert cleanup_evidence["cleanup_mode"] == "qa_demo_complete"
    assert cleanup_evidence["lease_state"] == "destroyed"
    assert cleanup_evidence["resource_refs"] == [
        {
            "resource_type": "release",
            "resource_id": "release-preview-1",
            "cleanup_action": "destroyed",
        },
        {
            "resource_type": "coolify_application",
            "resource_id": "app-preview-destroyed",
            "cleanup_action": "destroyed",
        },
        {
            "resource_type": "coolify_deployment",
            "resource_id": "deployment-preview-destroyed",
            "cleanup_action": "destroyed",
        },
    ]


def test_complete_does_not_force_replace_preview_release_when_resuming_qa_demo_stage() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    prepared.workflow_request.entry_mode = "resume"
    prepared.workflow_request.entry_stage = "qa"
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
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
            return_value=SimpleNamespace(
                created=True, reason="created", release=SimpleNamespace(service_urls=[])
            ),
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    assert preview_mock.call_args.kwargs["force"] is False


def test_complete_blocks_success_when_qa_demo_preview_cleanup_fails() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
        ],
    )
    preview_release = SimpleNamespace(
        release_id="release-preview-1",
        release_kind="run_preview",
        status="live",
        service_urls=[],
        commit_sha="b" * 40,
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "lease-1",
                "proof_scope_id": "run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "commit_sha": "b" * 40,
                "state": "live",
                "acquired_at": "2026-06-18T10:00:00+00:00",
                "expires_at": "2026-06-18T11:00:00+00:00",
            }
        },
        provider_context={
            "application_uuid": "app-1",
            "deployment_uuid": "deployment-1",
        },
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
            return_value=SimpleNamespace(
                created=False, reason="existing", release=preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            side_effect=RuntimeError("provider deletion failed"),
        ) as destroy_preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    destroy_preview_mock.assert_called_once()
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert (
        "QA demo preview cleanup failed"
        in finalizer_calls["workflow_result"].blocker_message
    )
    assert (
        "provider deletion failed" in finalizer_calls["workflow_result"].blocker_message
    )
    assert [
        call.kwargs["event"]
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    ][-2:] == ["PreviewCleanupRequested", "PreviewCleanupFailed"]
    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata")
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    }
    cleanup_failed_metadata = metadata_by_event["PreviewCleanupFailed"]
    assert cleanup_failed_metadata["cleanup_status"] == "failed"
    assert cleanup_failed_metadata["cleanup_mode"] == "destroy_or_ttl"
    assert "provider deletion failed" in cleanup_failed_metadata["error_message"]
    assert cleanup_failed_metadata["cleanup_evidence"] == {
        "release_id": "release-preview-1",
        "lease_id": "lease-1",
        "proof_scope_id": "run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "commit_sha": "b" * 40,
        "cleanup_status": "failed",
        "cleanup_mode": "destroy_or_ttl",
        "lease_state": "cleanup_failed",
        "error_message": (
            "QA demo preview cleanup failed; run preview release was not proven destroyed: "
            "release-preview-1: RuntimeError: provider deletion failed"
        ),
        "acquired_at": "2026-06-18T10:00:00+00:00",
        "expires_at": "2026-06-18T11:00:00+00:00",
        "resource_refs": [
            {
                "resource_type": "release",
                "resource_id": "release-preview-1",
                "cleanup_action": "cleanup_failed",
            },
            {
                "resource_type": "coolify_application",
                "resource_id": "app-1",
                "cleanup_action": "cleanup_failed",
            },
            {
                "resource_type": "coolify_deployment",
                "resource_id": "deployment-1",
                "cleanup_action": "cleanup_failed",
            },
        ],
    }
    final_checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert final_checkpoint.stage == "qa"
    assert final_checkpoint.status == "blocked"


def test_complete_reuses_waiting_preview_release_when_resuming_qa_demo_after_release_wait() -> (
    None
):
    waiting_release = SimpleNamespace(
        release_id="release-preview-1",
        tenant_id="tenant-1",
        project_id="project-1",
        source_run_id="run-1",
        release_kind="run_preview",
        status="live",
        service_urls=[],
    )
    session = SimpleNamespace(
        refresh=lambda _run: None, get=lambda _model, _id: waiting_release
    )
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    prepared.workflow_request.entry_mode = "resume"
    prepared.workflow_request.entry_stage = "qa"
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
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
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
            execution_context={
                "execution_branch": "run/MAB-400/run-1",
                "qa_demo_waiting_release_id": "release-preview-1",
            },
        )

    preview_mock.assert_not_called()
    assert qa_mock.call_args.kwargs["preview_release"] is waiting_release


def test_complete_reuses_waiting_preview_release_from_persisted_snapshot_context() -> (
    None
):
    waiting_release = SimpleNamespace(
        release_id="release-preview-1",
        tenant_id="tenant-1",
        project_id="project-1",
        source_run_id="run-1",
        release_kind="run_preview",
        status="live",
        service_urls=[],
    )
    session = SimpleNamespace(
        refresh=lambda _run: None, get=lambda _model, _id: waiting_release
    )
    deps = _deps()
    snapshot = ExecutionSnapshot.require(_build_snapshot())
    snapshot.context.execution_context["qa_demo_waiting_release_id"] = (
        "release-preview-1"
    )
    prepared = _prepared(
        snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True}
    )
    prepared.workflow_request.entry_mode = "resume"
    prepared.workflow_request.entry_stage = "qa"
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
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
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    assert qa_mock.call_args.kwargs["preview_release"] is waiting_release


def test_complete_regenerates_preview_when_waiting_release_failed() -> None:
    failed_waiting_release = SimpleNamespace(
        release_id="release-preview-1",
        tenant_id="tenant-1",
        project_id="project-1",
        source_run_id="run-1",
        release_kind="run_preview",
        status="failed",
        service_urls=[],
        last_error="previous preview failed",
    )
    regenerated_release = SimpleNamespace(
        release_id="release-preview-2",
        tenant_id="tenant-1",
        project_id="project-1",
        source_run_id="run-1",
        release_kind="run_preview",
        status="live",
        service_urls=[],
    )
    session = SimpleNamespace(
        refresh=lambda _run: None, get=lambda _model, _id: failed_waiting_release
    )
    deps = _deps()
    snapshot = ExecutionSnapshot.require(_build_snapshot())
    snapshot.context.execution_context["qa_demo_waiting_release_id"] = (
        "release-preview-1"
    )
    prepared = _prepared(
        snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True}
    )
    prepared.workflow_request.entry_mode = "resume"
    prepared.workflow_request.entry_stage = "qa"
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
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
            return_value=SimpleNamespace(
                created=True, reason="created", release=regenerated_release
            ),
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    assert qa_mock.call_args.kwargs["preview_release"] is regenerated_release


def test_complete_fails_on_conflicting_waiting_preview_release_contexts() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    snapshot = ExecutionSnapshot.require(_build_snapshot())
    snapshot.context.execution_context["qa_demo_waiting_release_id"] = (
        "release-preview-1"
    )
    prepared = _prepared(
        snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True}
    )
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
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
            execution_context={
                "execution_branch": "run/MAB-400/run-1",
                "qa_demo_waiting_release_id": "release-preview-2",
            },
        )

    preview_mock.assert_not_called()
    finalized_result = finalizer_calls["workflow_result"]
    assert finalized_result.outcome == "blocked"
    assert "QA demo waiting release id conflict" in finalized_result.blocker_message


def test_complete_blocks_success_when_qa_demo_stage_cannot_finish() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    preview_release = SimpleNamespace(
        release_id="release-preview-1",
        release_kind="run_preview",
        status="live",
        service_urls=[],
        commit_sha="b" * 40,
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "lease-1",
                "proof_scope_id": "run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "commit_sha": "b" * 40,
                "state": "live",
                "acquired_at": "2026-06-18T10:00:00+00:00",
                "expires_at": "2026-06-18T11:00:00+00:00",
            }
        },
        provider_context={
            "application_uuid": "app-1",
            "deployment_uuid": "deployment-1",
        },
    )
    finalizer_calls: dict[str, object] = {}

    def _destroy_preview(**_kwargs):  # noqa: ANN001
        preview_release.status = "destroyed"
        lease = preview_release.delivery_metadata["demo_proof_lease"]
        lease["state"] = "destroyed"
        lease["destroy_reason"] = "qa_demo_failed"
        lease["destroyed_at"] = "2026-06-18T10:30:00+00:00"
        return preview_release

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
            return_value=SimpleNamespace(
                created=False, reason="existing", release=preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            side_effect=RuntimeError("upload failed after retries"),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            side_effect=_destroy_preview,
        ) as destroy_preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "blocked"
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert (
        "upload failed after retries"
        in finalizer_calls["workflow_result"].blocker_message
    )
    assert deps.execution.start_demo_proof_workflow_fn.call_args.kwargs[
        "proof_scope_id"
    ] == ("run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb")
    assert [
        call.kwargs["event"]
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    ] == [
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationPassed",
        "RecordingStarted",
        "RecordingFailed",
        "RecordingFailedPreviewCleanupCompleted",
    ]
    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata")
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    }
    assert metadata_by_event["RecordingFailed"]["error_message"] == (
        "QA demo recording failed: RuntimeError: upload failed after retries"
    )
    assert metadata_by_event["RecordingFailed"][
        "failure_evidence_unavailable_reason"
    ] == ("qa_demo_stage_raised_before_failure_evidence_upload")
    assert (
        metadata_by_event["RecordingFailedPreviewCleanupCompleted"]["cleanup_status"]
        == "completed"
    )
    assert (
        metadata_by_event["RecordingFailedPreviewCleanupCompleted"]["cleanup_mode"]
        == "destroy_or_ttl"
    )
    destroy_preview_mock.assert_called_once_with(
        session=session,
        tenant_id="tenant-1",
        project_id="project-1",
        release_id="release-preview-1",
        reason="qa_demo_failed",
    )


def test_complete_blocks_demo_required_success_when_preview_release_is_not_created() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
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
            return_value=SimpleNamespace(
                created=False, reason="preview_prs_disabled", release=None
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage", qa_mock
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    assert (
        "preview deployment was not created: preview_prs_disabled"
        in finalizer_calls["workflow_result"].blocker_message
    )
    deps.execution.persist_stage_checkpoint_fn.assert_not_called()


def test_complete_requeues_demo_required_success_when_preview_release_is_still_deploying() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    deps.execution.requeue_workflow_result_for_stale_snapshot_fn.return_value = (
        SimpleNamespace(status="queued")
    )
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    preview_release = SimpleNamespace(
        release_id="release-preview-1",
        status="deploying",
        service_urls=[
            SimpleNamespace(
                service_kind="website", status="pending", url="https://preview.example"
            )
        ],
    )

    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(
                created=True, reason="created", release=preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage"
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer"
        ) as finalizer_cls,
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
    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "requeue"
    assert (
        "waiting for the run preview deployment/release to become live"
        in checkpoint.summary
    )
    workflow_result_arg = (
        deps.execution.requeue_workflow_result_for_stale_snapshot_fn.call_args.kwargs[
            "workflow_result"
        ]
    )
    assert (
        workflow_result_arg.orchestration_stage_trace[-1]["wait_reason"]
        == "qa_demo_preview_release"
    )
    assert (
        workflow_result_arg.orchestration_stage_trace[-1]["wait_for_release_id"]
        == "release-preview-1"
    )
    qa_mock.assert_not_called()
    update_pr_mock.assert_not_called()
    finalizer_cls.assert_not_called()
    deps.execution.requeue_workflow_result_for_capability_fn.assert_not_called()
    deps.execution.requeue_workflow_result_for_stale_snapshot_fn.assert_called_once()
    assert (
        deps.execution.requeue_workflow_result_for_stale_snapshot_fn.call_args.kwargs[
            "mark_stale_snapshot"
        ]
        is False
    )


def test_complete_blocks_demo_required_success_when_preview_release_failed() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    preview_release = SimpleNamespace(
        release_id="release-preview-1",
        release_kind="run_preview",
        status="failed",
        commit_sha="b" * 40,
        service_urls=[],
        last_error='[{"output":"Deployment failed: certificate has expired","hidden":false}]',
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "lease-1",
                "proof_scope_id": "run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "commit_sha": "b" * 40,
                "state": "failed",
                "acquired_at": "2026-06-18T10:00:00+00:00",
                "expires_at": "2026-06-18T11:00:00+00:00",
            }
        },
        provider_context={
            "application_uuid": "app-1",
            "deployment_uuid": "deployment-1",
        },
    )
    finalizer_calls: dict[str, object] = {}

    def _destroy_preview(**_kwargs):  # noqa: ANN001
        preview_release.status = "destroyed"
        lease = preview_release.delivery_metadata["demo_proof_lease"]
        lease["state"] = "destroyed"
        lease["destroy_reason"] = "qa_demo_failed"
        lease["destroyed_at"] = "2026-06-18T10:30:00+00:00"
        return preview_release

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
                release=preview_release,
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage"
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            side_effect=_destroy_preview,
        ) as destroy_preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    destroy_preview_mock.assert_called_once_with(
        session=session,
        tenant_id="tenant-1",
        project_id="project-1",
        release_id="release-preview-1",
        reason="qa_demo_failed",
    )
    assert [
        call.kwargs["event"]
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    ] == [
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseFailed",
        "ReleaseFailedPreviewCleanupCompleted",
    ]
    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata")
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    }
    assert metadata_by_event["ReleaseFailed"]["error_message"] == (
        "QA demo recording requires a live run preview deployment/release before recording; "
        "preview release is failed. Release failure: Deployment failed: certificate has expired"
    )
    assert (
        metadata_by_event["ReleaseFailedPreviewCleanupCompleted"]["cleanup_status"]
        == "completed"
    )
    assert (
        metadata_by_event["ReleaseFailedPreviewCleanupCompleted"]["cleanup_mode"]
        == "destroy_or_ttl"
    )
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert (
        "preview release is failed"
        in finalizer_calls["workflow_result"].blocker_message
    )
    assert (
        "certificate has expired" in finalizer_calls["workflow_result"].blocker_message
    )


def test_complete_blocks_demo_required_success_without_pr_url_before_preview_release() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
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
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment"
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage"
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    assert (
        "requires a PR URL before creating the run preview release"
        in finalizer_calls["workflow_result"].blocker_message
    )


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
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment"
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage"
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    assert (
        "requires persisted PM demo requirements before creating the run preview release"
        in str(finalizer_calls["workflow_result"].blocker_message)
    )


def test_complete_blocks_demo_required_success_without_project_demo_targets() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
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
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment"
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage"
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    assert (
        "requires project demo capture targets derived from project app metadata"
        in str(finalizer_calls["workflow_result"].blocker_message)
    )


def test_complete_blocks_demo_required_success_when_pm_plan_omits_project_demo_target_before_preview() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    prepared.workflow_request.project_demo_capture_targets = ("browser", "ios")
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
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment"
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage"
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    assert "PM requirements are missing required project demo capture target(s)" in str(
        finalizer_calls["workflow_result"].blocker_message
    )
    assert "ios" in str(finalizer_calls["workflow_result"].blocker_message)


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
        WorkflowStageCheckpoint(
            stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan
        )
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
                summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"
            ),
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
        WorkflowStageCheckpoint(
            stage="qa",
            attempt=1,
            status="requeue",
            summary="Still requires iOS",
            qa_result=previous_qa,
        )
    )
    prepared = _prepared(
        snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = replace(_workflow_result(), plan=plan)
    qa_result = replace(
        previous_qa, feedback="QA demo recording still requires capture target(s): ios"
    )

    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(
                created=False,
                reason="existing",
                release=SimpleNamespace(service_urls=[]),
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
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

    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "requeue"
    assert qa_mock.call_args.kwargs["previous_qa_result"] == previous_qa
    update_pr_mock.assert_not_called()
    deps.execution.requeue_workflow_result_for_capability_fn.assert_called_once()
    assert (
        deps.execution.requeue_workflow_result_for_capability_fn.call_args.kwargs[
            "required_worker_capability"
        ]
        == "macos"
    )
    deps.execution.start_demo_proof_workflow_fn.assert_not_called()
    deps.execution.advance_demo_proof_workflow_event_fn.assert_called_once()
    assert (
        deps.execution.advance_demo_proof_workflow_event_fn.call_args.kwargs["event"]
        == "RecordingDeferred"
    )
    assert deps.execution.advance_demo_proof_workflow_event_fn.call_args.kwargs[
        "event_metadata"
    ] == {
        "recorded_capture_targets": ["browser"],
        "remaining_capture_targets": ["browser", "ios"],
    }


def test_complete_attaches_pr_evidence_after_accumulated_qa_demo_recordings_finish() -> (
    None
):
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
        WorkflowStageCheckpoint(
            stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan
        )
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
                summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"
            ),
        )
    )
    previous_qa = QaResult(
        summary=["Recorded Linux demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Browser walkthrough", index=1),
            _qa_recording(name="Browser invalid input", index=2),
            _qa_recording(name="Browser repeat action", index=3),
            _qa_recording(
                name="Android walkthrough",
                index=4,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
            _qa_recording(
                name="Android invalid input",
                index=5,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
            _qa_recording(
                name="Android repeat action",
                index=6,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
        ],
        outcome="requeue",
        feedback="Still requires iOS",
    )
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="qa",
            attempt=1,
            status="requeue",
            summary="Still requires iOS",
            qa_result=previous_qa,
        )
    )
    final_qa = replace(
        previous_qa,
        summary=["Recorded all demos"],
        recordings=[
            *previous_qa.recordings,
            _qa_recording(
                name="iOS walkthrough",
                index=7,
                capture_target="ios",
                capture_reference="ios-simulator://configured",
                suffix="mp4",
            ),
            _qa_recording(
                name="iOS invalid input",
                index=8,
                capture_target="ios",
                capture_reference="ios-simulator://configured",
                suffix="mp4",
            ),
            _qa_recording(
                name="iOS repeat action",
                index=9,
                capture_target="ios",
                capture_reference="ios-simulator://configured",
                suffix="mp4",
            ),
        ],
        outcome="continue",
        feedback=None,
    )
    prepared = _prepared(
        snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True}
    )
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
            return_value=SimpleNamespace(
                created=False,
                reason="existing",
                release=SimpleNamespace(service_urls=[]),
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=final_qa,
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "completed"
    assert qa_mock.call_args.kwargs["previous_qa_result"] == previous_qa
    assert [
        recording.capture_target
        for recording in update_pr_mock.call_args.kwargs["qa_result"].recordings
    ] == [
        "browser",
        "browser",
        "browser",
        "android",
        "android",
        "android",
        "ios",
        "ios",
        "ios",
    ]
    assert update_pr_mock.call_args.kwargs["required_capture_targets"] == (
        "browser",
        "ios",
        "android",
    )
    assert update_pr_mock.call_args.kwargs["required_recording_counts"] == {
        "browser": 3,
        "ios": 3,
        "android": 3,
    }
    deps.execution.requeue_workflow_result_for_capability_fn.assert_not_called()
    assert (
        finalizer_calls["workflow_result"].orchestration_stage_trace[-1]["status"]
        == "completed"
    )


def test_complete_persists_blocked_qa_checkpoint_when_pr_evidence_update_fails() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
        ],
    )
    preview_release = SimpleNamespace(
        release_id="release-preview-1",
        release_kind="run_preview",
        status="live",
        commit_sha="b" * 40,
        service_urls=[],
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "lease-1",
                "proof_scope_id": "run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "commit_sha": "b" * 40,
                "state": "live",
                "acquired_at": "2026-06-18T10:00:00+00:00",
                "expires_at": "2026-06-18T11:00:00+00:00",
            }
        },
        provider_context={
            "application_uuid": "app-1",
            "deployment_uuid": "deployment-1",
        },
    )
    finalizer_calls: dict[str, object] = {}

    def _destroy_preview(**_kwargs):  # noqa: ANN001
        preview_release.status = "destroyed"
        lease = preview_release.delivery_metadata["demo_proof_lease"]
        lease["state"] = "destroyed"
        lease["destroy_reason"] = "qa_demo_failed"
        lease["destroyed_at"] = "2026-06-18T10:30:00+00:00"
        return preview_release

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
            return_value=SimpleNamespace(
                created=False, reason="existing", release=preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            side_effect=RuntimeError("GitHub rejected PR update"),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            side_effect=_destroy_preview,
        ) as destroy_preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    first_checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args_list[
        0
    ].kwargs["checkpoint"]
    final_checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert first_checkpoint.status == "completed"
    assert final_checkpoint.stage == "qa"
    assert final_checkpoint.status == "blocked"
    assert (
        "QA demo evidence PR update failed: RuntimeError: GitHub rejected PR update"
        in str(final_checkpoint.qa_result.blocker_message)
    )
    assert [
        call.kwargs["event"]
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    ] == [
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationPassed",
        "RecordingStarted",
        "RecordingCompleted",
        "EvidenceUploadStarted",
        "EvidenceUploaded",
        "PREvidenceAttachStarted",
        "PREvidenceAttachFailed",
        "PREvidenceAttachFailedPreviewCleanupCompleted",
    ]
    destroy_preview_mock.assert_called_once_with(
        session=session,
        tenant_id="tenant-1",
        project_id="project-1",
        release_id="release-preview-1",
        reason="qa_demo_failed",
    )
    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata")
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    }
    assert metadata_by_event["PREvidenceAttachFailed"]["error_message"] == (
        "QA demo evidence PR update failed: RuntimeError: GitHub rejected PR update"
    )
    assert (
        metadata_by_event["PREvidenceAttachFailedPreviewCleanupCompleted"][
            "cleanup_status"
        ]
        == "completed"
    )
    assert (
        metadata_by_event["PREvidenceAttachFailedPreviewCleanupCompleted"][
            "cleanup_mode"
        ]
        == "destroy_or_ttl"
    )
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "GitHub rejected PR update" in str(
        finalizer_calls["workflow_result"].blocker_message
    )


def test_complete_blocks_pr_evidence_update_when_artifact_url_check_fails(
    monkeypatch,
) -> None:  # noqa: ANN001
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Happy path", index=1),
            _qa_recording(name="Invalid input is rejected", index=2),
            _qa_recording(name="Repeat action remains safe", index=3),
        ],
    )
    preview_release = SimpleNamespace(
        release_id="release-preview-1",
        release_kind="run_preview",
        status="live",
        commit_sha="b" * 40,
        service_urls=[],
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "lease-1",
                "proof_scope_id": "run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "commit_sha": "b" * 40,
                "state": "live",
                "acquired_at": "2026-06-18T10:00:00+00:00",
                "expires_at": "2026-06-18T11:00:00+00:00",
            }
        },
        provider_context={
            "application_uuid": "app-1",
            "deployment_uuid": "deployment-1",
        },
    )
    finalizer_calls: dict[str, object] = {}

    def _reject_url(*_args, **_kwargs):  # noqa: ANN202
        raise RuntimeError(
            "QA demo artifact URL is not reachable: https://cdn.example/qa-demo-1.webm: HTTP 404"
        )

    def _destroy_preview(**_kwargs):  # noqa: ANN001
        preview_release.status = "destroyed"
        lease = preview_release.delivery_metadata["demo_proof_lease"]
        lease["state"] = "destroyed"
        lease["destroy_reason"] = "qa_demo_failed"
        lease["destroyed_at"] = "2026-06-18T10:30:00+00:00"
        return preview_release

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

    monkeypatch.setattr(
        "orchestrator.core.worker.run_outcome_policy.ensure_artifact_url_reachable",
        _reject_url,
    )
    with (
        patch(
            "orchestrator.core.worker.run_outcome_policy.create_run_preview_deployment",
            return_value=SimpleNamespace(
                created=False, reason="existing", release=preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            side_effect=_destroy_preview,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    update_pr_mock.assert_not_called()
    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata")
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    }
    assert metadata_by_event["PREvidenceAttachFailed"]["artifact_urls"] == [
        "https://cdn.example/qa-demo-1.webm",
        "https://cdn.example/qa-demo-2.webm",
        "https://cdn.example/qa-demo-3.webm",
    ]
    assert (
        "artifact_url_check_status" not in metadata_by_event["PREvidenceAttachFailed"]
    )
    assert "checked_artifact_urls" not in metadata_by_event["PREvidenceAttachFailed"]
    assert "artifact URL is not reachable" in str(
        finalizer_calls["workflow_result"].blocker_message
    )


def test_complete_records_qa_failure_evidence_pr_attach_failure_before_blocking_ready_review() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    failure_evidence = _qa_failure_evidence()
    qa_result = QaResult(
        summary=["QA demo recording failed after 1 attempts"],
        scenarios=[],
        recordings=[],
        failure_evidence=[failure_evidence],
        outcome="blocked",
        blocker_message="QA demo recording failed after 1 attempts: app did not load",
    )
    preview_release = SimpleNamespace(
        release_id="release-1",
        release_kind="run_preview",
        status="live",
        service_urls=[],
        commit_sha="b" * 40,
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "lease-1",
                "proof_scope_id": "run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "commit_sha": "b" * 40,
                "state": "live",
                "acquired_at": "2026-06-18T10:00:00+00:00",
                "expires_at": "2026-06-18T11:00:00+00:00",
            }
        },
        provider_context={
            "application_uuid": "app-1",
            "deployment_uuid": "deployment-1",
        },
    )
    finalizer_calls: dict[str, object] = {}

    def _destroy_preview(**_kwargs):  # noqa: ANN001
        preview_release.status = "destroyed"
        lease = preview_release.delivery_metadata["demo_proof_lease"]
        lease["state"] = "destroyed"
        lease["destroy_reason"] = "qa_demo_failed"
        lease["destroyed_at"] = "2026-06-18T10:30:00+00:00"
        return preview_release

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
            return_value=SimpleNamespace(
                created=False, reason="existing", release=preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_failure_evidence",
            side_effect=RuntimeError("GitHub rejected failure evidence update"),
        ) as update_failure_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            side_effect=_destroy_preview,
        ) as destroy_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    update_failure_pr_mock.assert_called_once()
    assert update_failure_pr_mock.call_args.kwargs["required_capture_targets"] == (
        "browser",
    )
    update_pr_mock.assert_not_called()
    destroy_mock.assert_called_once()
    assert destroy_mock.call_args.kwargs["reason"] == "qa_demo_failed"
    assert [
        call.kwargs["event"]
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    ] == [
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationPassed",
        "RecordingStarted",
        "RecordingFailureEvidenceCaptured",
        "FailureEvidenceUploadStarted",
        "FailureEvidenceUploaded",
        "PRFailureEvidenceAttachStarted",
        "PRFailureEvidenceAttachFailed",
        "PRFailureEvidenceAttachFailedPreviewCleanupCompleted",
    ]
    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata")
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    }
    assert metadata_by_event["PRFailureEvidenceAttachFailed"][
        "error_message"
    ].startswith(
        "QA demo failure evidence PR update failed: RuntimeError: GitHub rejected failure evidence update"
    )
    assert (
        "pageerror: process is not defined"
        in metadata_by_event["PRFailureEvidenceAttachFailed"]["error_message"]
    )
    assert metadata_by_event["PRFailureEvidenceAttachFailed"]["artifact_urls"] == [
        "https://cdn.example/qa-failure-1.webm"
    ]
    assert (
        metadata_by_event["PRFailureEvidenceAttachFailedPreviewCleanupCompleted"][
            "cleanup_status"
        ]
        == "completed"
    )
    assert (
        metadata_by_event["PRFailureEvidenceAttachFailedPreviewCleanupCompleted"][
            "cleanup_mode"
        ]
        == "destroy_or_ttl"
    )
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "app did not load" in str(finalizer_calls["workflow_result"].blocker_message)
    assert "GitHub rejected failure evidence update" in str(
        finalizer_calls["workflow_result"].blocker_message
    )
    assert failure_evidence.error_message in str(
        finalizer_calls["workflow_result"].blocker_message
    )


def test_complete_attaches_qa_failure_evidence_to_pr_before_blocking_ready_review() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    workflow_result = _workflow_result()
    qa_result = QaResult(
        summary=["QA demo recording failed after 1 attempts"],
        scenarios=[],
        recordings=[],
        failure_evidence=[_qa_failure_evidence()],
        outcome="blocked",
        blocker_message="QA demo recording failed after 1 attempts: app did not load",
        failure_kind="release_readiness",
    )
    preview_release = SimpleNamespace(
        release_id="release-1",
        release_kind="run_preview",
        status="live",
        service_urls=[],
        commit_sha="b" * 40,
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
            return_value=SimpleNamespace(
                created=False, reason="existing", release=preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_failure_evidence",
            return_value="updated-body",
        ) as update_failure_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            side_effect=RuntimeError("provider deletion failed"),
        ) as destroy_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert checkpoint.status == "blocked"
    assert checkpoint.qa_result.failure_evidence == qa_result.failure_evidence
    update_failure_pr_mock.assert_called_once()
    assert update_failure_pr_mock.call_args.kwargs["qa_result"] == qa_result
    assert update_failure_pr_mock.call_args.kwargs["required_capture_targets"] == (
        "browser",
    )
    update_pr_mock.assert_not_called()
    destroy_mock.assert_called_once()
    assert destroy_mock.call_args.kwargs["reason"] == "qa_demo_failed"
    deps.execution.start_demo_proof_workflow_fn.assert_called_once()
    assert deps.execution.start_demo_proof_workflow_fn.call_args.kwargs[
        "trigger_event"
    ] == ("run_failed_with_demo_failure_evidence")
    event_calls = deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    assert [call.kwargs["event"] for call in event_calls] == [
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationFailed",
        "FailureEvidenceUploadStarted",
        "FailureEvidenceUploaded",
        "PRFailureEvidenceAttachStarted",
        "PRFailureEvidenceAttached",
        "FailurePreviewCleanupRequested",
        "FailurePreviewCleanupFailed",
    ]
    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata") for call in event_calls
    }
    service_failure_evidence = metadata_by_event["ServiceVerificationFailed"][
        "failure_evidence"
    ]
    assert len(service_failure_evidence) == 1
    _assert_iso_created_at(service_failure_evidence[0]["created_at"])
    assert service_failure_evidence == [
        {
            "recording_name": "App load",
            "capture_target": "browser",
            "artifact_url": "https://cdn.example/qa-failure-1.webm",
            "object_key": "tenant-1/project-1/run-1/qa-failure-1.webm",
            "capture_reference": "https://preview.example",
            "created_at": service_failure_evidence[0]["created_at"],
            "content_sha256": f"{101:064x}",
            "release_commit_sha": "b" * 40,
            "release_context_sha256": f"{999:064x}",
            "error_message": "QA Demo Ready was not visible\nBrowser diagnostics:\npageerror: process is not defined",
        }
    ]
    assert (
        "did not load"
        in metadata_by_event["ServiceVerificationFailed"]["error_message"]
    )
    uploaded_failure_evidence = metadata_by_event["FailureEvidenceUploaded"][
        "failure_evidence"
    ]
    assert len(uploaded_failure_evidence) == 1
    _assert_iso_created_at(uploaded_failure_evidence[0]["created_at"])
    assert uploaded_failure_evidence == [
        {
            "recording_name": "App load",
            "capture_target": "browser",
            "artifact_url": "https://cdn.example/qa-failure-1.webm",
            "object_key": "tenant-1/project-1/run-1/qa-failure-1.webm",
            "capture_reference": "https://preview.example",
            "created_at": uploaded_failure_evidence[0]["created_at"],
            "content_sha256": f"{101:064x}",
            "release_commit_sha": "b" * 40,
            "release_context_sha256": f"{999:064x}",
            "error_message": "QA Demo Ready was not visible\nBrowser diagnostics:\npageerror: process is not defined",
        }
    ]
    assert (
        metadata_by_event["PRFailureEvidenceAttached"]["artifact_url_check_status"]
        == "passed"
    )
    assert metadata_by_event["PRFailureEvidenceAttached"]["checked_artifact_urls"] == [
        "https://cdn.example/qa-failure-1.webm"
    ]
    deps.execution.mark_pull_request_ready_after_demo_proof_fn.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "app did not load" in str(finalizer_calls["workflow_result"].blocker_message)
    assert "provider deletion failed" in str(
        finalizer_calls["workflow_result"].blocker_message
    )


def test_complete_blocks_incomplete_qa_failure_evidence_before_pr_update() -> None:
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
        WorkflowStageCheckpoint(
            stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan
        )
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
                summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"
            ),
        )
    )
    prepared = _prepared(
        snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True}
    )
    prepared.workflow_request.project_demo_capture_targets = ("browser", "ios")
    workflow_result = replace(_workflow_result(), plan=plan)
    qa_result = QaResult(
        summary=["QA demo browser recording failed"],
        scenarios=[],
        recordings=[],
        failure_evidence=[_qa_failure_evidence()],
        outcome="blocked",
        blocker_message="QA demo recording failed: browser app did not load",
    )
    preview_release = SimpleNamespace(
        release_id="release-1",
        release_kind="run_preview",
        status="live",
        commit_sha="b" * 40,
        service_urls=[],
        delivery_metadata={
            "demo_proof_lease": {
                "lease_id": "lease-1",
                "proof_scope_id": "run:run-1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "commit_sha": "b" * 40,
                "state": "live",
                "acquired_at": "2026-06-18T10:00:00+00:00",
                "expires_at": "2026-06-18T11:00:00+00:00",
            }
        },
        provider_context={
            "application_uuid": "app-1",
            "deployment_uuid": "deployment-1",
        },
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
            return_value=SimpleNamespace(
                created=False, reason="existing", release=preview_release
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_failure_evidence",
            return_value="updated-body",
        ) as update_failure_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.destroy_project_deployment_preview_release",
            side_effect=RuntimeError("provider deletion failed"),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "blocked"
    assert "missing proof for required capture target(s): ios" in str(
        checkpoint.qa_result.blocker_message
    )
    update_failure_pr_mock.assert_not_called()
    update_pr_mock.assert_not_called()
    deps.execution.start_demo_proof_workflow_fn.assert_called_once()
    assert [
        call.kwargs["event"]
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    ] == [
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationPassed",
        "RecordingStarted",
        "RecordingFailed",
        "RecordingFailedPreviewCleanupFailed",
    ]
    metadata_by_event = {
        call.kwargs["event"]: call.kwargs.get("event_metadata")
        for call in deps.execution.advance_demo_proof_workflow_event_fn.call_args_list
    }
    cleanup_failed_metadata = metadata_by_event["RecordingFailedPreviewCleanupFailed"]
    assert cleanup_failed_metadata["cleanup_status"] == "failed"
    assert "provider deletion failed" in cleanup_failed_metadata["error_message"]
    assert (
        cleanup_failed_metadata["cleanup_evidence"]["lease_state"] == "cleanup_failed"
    )
    assert cleanup_failed_metadata["cleanup_evidence"]["resource_refs"] == [
        {
            "resource_type": "release",
            "resource_id": "release-1",
            "cleanup_action": "cleanup_failed",
        },
        {
            "resource_type": "coolify_application",
            "resource_id": "app-1",
            "cleanup_action": "cleanup_failed",
        },
        {
            "resource_type": "coolify_deployment",
            "resource_id": "deployment-1",
            "cleanup_action": "cleanup_failed",
        },
    ]
    assert finalizer_calls["workflow_result"].outcome == "blocked"
    assert "missing proof for required capture target(s): ios" in str(
        finalizer_calls["workflow_result"].blocker_message
    )


def test_complete_blocks_when_qa_continue_result_is_missing_required_capture_target_proof() -> (
    None
):
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
        WorkflowStageCheckpoint(
            stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan
        )
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
                summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"
            ),
        )
    )
    qa_result = QaResult(
        summary=["Recorded demos"],
        scenarios=[],
        recordings=[
            _qa_recording(name="Browser walkthrough", index=1),
            _qa_recording(name="Browser invalid input", index=2),
            _qa_recording(name="Browser repeat action", index=3),
            _qa_recording(
                name="Android walkthrough",
                index=4,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
            _qa_recording(
                name="Android invalid input",
                index=5,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
            _qa_recording(
                name="Android repeat action",
                index=6,
                capture_target="android",
                capture_reference="android-emulator://configured",
                suffix="mp4",
            ),
        ],
        outcome="continue",
    )
    prepared = _prepared(
        snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True}
    )
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
            return_value=SimpleNamespace(
                created=False,
                reason="existing",
                release=SimpleNamespace(service_urls=[]),
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "blocked"
    assert "without proof for required capture target(s): ios" in str(
        checkpoint.qa_result.blocker_message
    )
    update_pr_mock.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"


def test_complete_blocks_when_qa_continue_result_has_too_few_variant_recordings() -> (
    None
):
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
        WorkflowStageCheckpoint(
            stage="pm", attempt=1, status="completed", summary="PM ready.", plan=plan
        )
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
                summary=["Looks good"], pr_url="https://github.com/acme/repo/pull/8"
            ),
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
    prepared = _prepared(
        snapshot.dump(), effective_policy={"qa_demo_recording_enabled": True}
    )
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
            return_value=SimpleNamespace(
                created=False,
                reason="existing",
                release=SimpleNamespace(service_urls=[]),
            ),
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence"
        ) as update_pr_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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

    checkpoint = deps.execution.persist_stage_checkpoint_fn.call_args.kwargs[
        "checkpoint"
    ]
    assert checkpoint.stage == "qa"
    assert checkpoint.status == "blocked"
    assert "without proof for required capture target(s): browser" in str(
        checkpoint.qa_result.blocker_message
    )
    update_pr_mock.assert_not_called()
    assert finalizer_calls["workflow_result"].outcome == "blocked"


def test_complete_creates_preview_release_for_ios_only_demo_requirements() -> None:
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": True}
    )
    prepared.workflow_request.project_demo_capture_targets = ("ios",)
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
            return_value=SimpleNamespace(
                created=False,
                reason="existing",
                release=SimpleNamespace(service_urls=[]),
            ),
        ) as preview_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.execute_qa_demo_stage",
            return_value=qa_result,
        ) as qa_mock,
        patch(
            "orchestrator.core.worker.run_outcome_policy.update_pull_request_with_demo_evidence",
            return_value="updated-body",
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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
    assert (
        qa_mock.call_args.kwargs["preview_release"] is preview_mock.return_value.release
    )


def test_complete_does_not_requeue_when_start_ref_moves_to_run_artifact_commit() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": False}
    )
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
        patch(
            "orchestrator.core.worker.run_outcome_policy.WorkflowFinalizer", _Finalizer
        ),
        patch(
            "orchestrator.core.worker.run_outcome_policy.CompletionTailExecutor"
        ) as tail_executor_cls,
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


def test_complete_requeues_when_start_ref_moves_to_different_commit_than_run_artifact() -> (
    None
):
    session = SimpleNamespace(refresh=lambda _run: None)
    deps = _deps()
    prepared = _prepared(
        _build_snapshot(), effective_policy={"qa_demo_recording_enabled": False}
    )
    prepared.workflow_request.start_point_ref = "origin/feature/MAB-400"
    prepared.workflow_request.start_point_sha = "oldsha1234567890"
    deps.execution.check_run_snapshot_freshness_fn.return_value = SimpleNamespace(
        stale=True,
        current_start_point_sha="newsha9876543210",
        message="Branch snapshot stale: origin/feature/MAB-400 moved from oldsha123456 to newsha987654. Requeueing from the latest snapshot.",
    )
    cleanup_mock = MagicMock()
    deps.execution.requeue_workflow_result_for_stale_snapshot_fn.return_value = (
        "requeued"
    )

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
