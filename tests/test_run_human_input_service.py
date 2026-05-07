from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch, sentinel

from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.runs.human_input_service import (
    resume_run_from_human_input_answer,
    answer_human_input_request,
    create_human_input_request,
    resume_workflow_from_human_input_answer,
)
from orchestrator.core.runs.enqueue_types import EnqueueFailureReason


def test_create_human_input_request_snapshots_checkpoint_and_moves_run_to_waiting() -> None:
    session = MagicMock()
    tenant = SimpleNamespace(tenant_id="route25")
    project = SimpleNamespace(project_id="route25-default")
    run = SimpleNamespace(
        run_id="run-1",
        workflow_id="workflow-1",
        issue_key="GP-122",
        status="running",
        last_heartbeat_at="heartbeat",
        worker_service_instance_id="worker-1",
        plan=ExecutionSnapshot.empty(
            trigger_context={"source": "manual"}
        ).dump(),
    )
    workflow = SimpleNamespace(
        workflow_id="workflow-1",
        status="running",
        active_run_id="run-1",
        latest_checkpoint_id=None,
        updated_at=None,
        last_error=None,
    )
    workflow_query = MagicMock()
    workflow_query.scalars.return_value.one_or_none.return_value = workflow
    pending_query = MagicMock()
    pending_query.scalar_one_or_none.return_value = None
    session.execute.side_effect = [workflow_query, pending_query]
    checkpoint = SimpleNamespace(checkpoint_id="checkpoint-1")
    send_result = SimpleNamespace(
        sent=True,
        channel_id="channel-1",
        thread_channel_id="thread-1",
        message_id="message-1",
        reason=None,
    )

    with (
        patch("orchestrator.core.runs.human_input_service.snapshot_checkpoint_for_run", return_value=checkpoint),
        patch("orchestrator.core.runs.human_input_service.send_tenant_discord_message", return_value=send_result),
        patch("orchestrator.core.runs.human_input_service.upsert_followup_context"),
    ):
        request = create_human_input_request(
            session=session,
            settings=SimpleNamespace(),
            tenant=tenant,
            project=project,
            run=run,
            issue_key="GP-122",
            source_stage="dev",
            request_type="verification_code",
            prompt="Reply with the current code",
        )

    assert request.workflow_id == "workflow-1"
    assert request.checkpoint_id == "checkpoint-1"
    assert request.source_run_id == "run-1"
    assert request.source_stage == "dev"
    assert run.status == "waiting_for_input"
    assert run.last_heartbeat_at is None
    assert run.worker_service_instance_id is None
    assert workflow.status == "waiting_for_input"
    assert workflow.latest_checkpoint_id == "checkpoint-1"


def test_create_human_input_request_renders_structured_questions_in_discord_message() -> None:
    session = MagicMock()
    tenant = SimpleNamespace(tenant_id="route25")
    project = SimpleNamespace(project_id="route25-default")
    run = SimpleNamespace(
        run_id="run-1",
        workflow_id="workflow-1",
        issue_key="GP-125",
        status="running",
        last_heartbeat_at="heartbeat",
        worker_service_instance_id="worker-1",
        plan=ExecutionSnapshot.empty(trigger_context={"source": "manual"}).dump(),
    )
    workflow = SimpleNamespace(
        workflow_id="workflow-1",
        status="running",
        active_run_id="run-1",
        latest_checkpoint_id=None,
        updated_at=None,
        last_error=None,
    )
    workflow_query = MagicMock()
    workflow_query.scalars.return_value.one_or_none.return_value = workflow
    pending_query = MagicMock()
    pending_query.scalar_one_or_none.return_value = None
    session.execute.side_effect = [workflow_query, pending_query]
    checkpoint = SimpleNamespace(checkpoint_id="checkpoint-1")
    send_result = SimpleNamespace(
        sent=True,
        channel_id="channel-1",
        thread_channel_id="thread-1",
        message_id="message-1",
        reason=None,
    )

    with (
        patch("orchestrator.core.runs.human_input_service.snapshot_checkpoint_for_run", return_value=checkpoint),
        patch("orchestrator.core.runs.human_input_service.send_tenant_discord_message", return_value=send_result) as send_mock,
        patch("orchestrator.core.runs.human_input_service.upsert_followup_context"),
    ):
        create_human_input_request(
            session=session,
            settings=SimpleNamespace(),
            tenant=tenant,
            project=project,
            run=run,
            issue_key="GP-125",
            source_stage="pm",
            request_type="decision_gate_clarification",
            prompt="Decision Gate needs two clarifications before execution.",
            request_context={
                "questions": [
                    {
                        "id": "sync_failure_policy",
                        "question": "If StoreKit status is indeterminate, should gating fail closed or keep the last-known entitlement?",
                        "options": ["Fail closed", "Keep last-known entitlement"],
                    },
                    {
                        "id": "dependencies_risks",
                        "question": "What dependencies or risks should be recorded?",
                        "options": ["No external dependencies; risk is temporary entitlement uncertainty", "none"],
                    },
                ]
            },
        )

    message = send_mock.call_args.kwargs["message"]
    assert "Please answer these items:" in message
    assert "1. If StoreKit status is indeterminate" in message
    assert "1.1 Fail closed" in message
    assert "2. What dependencies or risks should be recorded?" in message
    assert "2.2 none" in message
    assert "reply in this thread and answer each numbered item in order" in message


def test_resume_workflow_from_human_input_answer_creates_resume_attempt_and_consumes_request() -> None:
    session = MagicMock()
    request = SimpleNamespace(
        request_id="request-1",
        workflow_id="workflow-1",
        checkpoint_id="checkpoint-1",
        source_run_id="run-1",
        status="pending",
        answered_at=None,
        updated_at=None,
        answer_text=None,
        answer_encrypted=None,
        answer_source_ref=None,
        consumed_by_run_id=None,
        tenant_id="route25",
        source_stage="review",
        expires_at=None,
    )
    source_run = SimpleNamespace(
        run_id="run-1",
        tenant_id="route25",
        project_id="route25-default",
        issue_key="GP-122",
        issue_summary="Apple Sign In",
        issue_description="Verify Apple Sign In",
        repo_url="https://github.com/example/repo",
        branch="feature/GP-122",
        pr_url="https://github.com/example/repo/pull/123",
        pre_check_outcome="ready_for_agent",
        required_worker_capability="macos",
        plan=ExecutionSnapshot.empty(trigger_context={"source": "manual"}).dump(),
    )
    checkpoint = SimpleNamespace(
        checkpoint_id="checkpoint-1",
        payload_json=ExecutionSnapshot.empty(trigger_context={"source": "manual"}).dump(),
    )
    resumed_run = SimpleNamespace(run_id="run-2")
    existing_resume_query = MagicMock()
    existing_resume_query.scalars.return_value.all.return_value = []
    lock_query = MagicMock()
    lock_query.scalars.return_value.one_or_none.return_value = request
    session.execute.side_effect = [lock_query, existing_resume_query]
    session.get.side_effect = lambda model, key: (
        source_run if key == "run-1" else checkpoint if key == "checkpoint-1" else None
    )
    enqueue_result = SimpleNamespace(enqueued=True, reason=None, run=resumed_run)
    runtime = MagicMock()
    runtime.create_attempt.return_value = enqueue_result

    with (
        patch("orchestrator.core.runs.human_input_service.build_workflow_runtime", return_value=runtime),
        patch("orchestrator.core.runs.human_input_service.close_followup_contexts"),
    ):
        answered = answer_human_input_request(
            session=session,
            settings=SimpleNamespace(secrets_encryption_key="secret-key"),
            request=request,
            reply_text="use qa-apple@example.com",
            source_ref="discord:message-1",
        )
        result = resume_run_from_human_input_answer(
            session=session,
            settings=SimpleNamespace(secrets_encryption_key="secret-key"),
            request=answered,
        )

    assert result is resumed_run
    bootstrap = runtime.create_attempt.call_args.kwargs["bootstrap"]
    assert bootstrap.workflow_id == "workflow-1"
    assert bootstrap.parent_run_id == "run-1"
    assert bootstrap.entry_mode == "resume"
    assert bootstrap.entry_stage == "review"
    assert bootstrap.entry_checkpoint_id == "checkpoint-1"
    assert bootstrap.branch == "feature/GP-122"
    assert bootstrap.precheck_outcome == "ready_for_agent"
    assert bootstrap.required_worker_capability == "macos"
    snapshot = ExecutionSnapshot.require(bootstrap.plan, allow_empty=True)
    assert snapshot.context.execution_context.get("human_input_request_id") == "request-1"
    assert request.answer_text == "use qa-apple@example.com"
    assert request.answer_encrypted is None
    assert request.status == "consumed"
    assert request.consumed_by_run_id == "run-2"


def test_resume_workflow_from_human_input_answer_carries_precheck_from_source_run_when_checkpoint_missing() -> None:
    session = MagicMock()
    request = SimpleNamespace(
        request_id="request-1",
        workflow_id="workflow-1",
        checkpoint_id="checkpoint-1",
        source_run_id="run-1",
        status="answered",
        consumed_by_run_id=None,
        tenant_id="route25",
        source_stage="review",
    )
    source_snapshot = ExecutionSnapshot.empty(trigger_context={"source": "manual"})
    source_snapshot.context.execution_context["pre_check_outcome"] = "ready_for_agent"
    source_run = SimpleNamespace(
        run_id="run-1",
        branch="feature/GP-122",
        pr_url="https://github.com/example/repo/pull/123",
        required_worker_capability="macos",
        plan=source_snapshot.dump(),
    )
    checkpoint = SimpleNamespace(
        checkpoint_id="checkpoint-1",
        payload_json=ExecutionSnapshot.empty(trigger_context={"source": "manual"}).dump(),
    )
    resumed_run = SimpleNamespace(run_id="run-2")
    lock_query = MagicMock()
    lock_query.scalars.return_value.one_or_none.return_value = request
    existing_resume_query = MagicMock()
    existing_resume_query.scalars.return_value.all.return_value = []
    session.execute.side_effect = [lock_query, existing_resume_query]
    session.get.side_effect = lambda model, key: (
        source_run if key == "run-1" else checkpoint if key == "checkpoint-1" else None
    )
    enqueue_result = SimpleNamespace(enqueued=True, reason=None, run=resumed_run)
    runtime = MagicMock()
    runtime.create_attempt.return_value = enqueue_result

    with (
        patch("orchestrator.core.runs.human_input_service.build_workflow_runtime", return_value=runtime),
        patch("orchestrator.core.runs.human_input_service.close_followup_contexts"),
    ):
        result = resume_run_from_human_input_answer(
            session=session,
            settings=SimpleNamespace(secrets_encryption_key="secret-key"),
            request=request,
        )

    assert result is resumed_run
    bootstrap = runtime.create_attempt.call_args.kwargs["bootstrap"]
    snapshot = ExecutionSnapshot.require(bootstrap.plan, allow_empty=True)
    assert snapshot.context.execution_context.get("human_input_request_id") == "request-1"
    assert snapshot.context.execution_context.get("pre_check_outcome") == "ready_for_agent"
    assert bootstrap.required_worker_capability == "macos"


def test_resume_workflow_from_human_input_answer_delegates_to_workflow_runtime() -> None:
    session = MagicMock()
    request = SimpleNamespace(
        request_id="request-1",
        workflow_id="workflow-1",
        status="answered",
    )
    workflow = SimpleNamespace(workflow_id="workflow-1")
    runtime = MagicMock()
    runtime.resume_input.return_value = sentinel.resumed_run
    session.get.side_effect = lambda model, key: request if key == "request-1" else workflow if key == "workflow-1" else None

    with patch("orchestrator.core.runs.human_input_service.build_workflow_runtime", return_value=runtime):
        result = resume_workflow_from_human_input_answer(
            session=session,
            settings=SimpleNamespace(secrets_encryption_key="secret-key"),
            request=request,
        )

    assert result is sentinel.resumed_run
    runtime.resume_input.assert_called_once_with(workflow=workflow, request=request)


def test_resume_workflow_from_human_input_answer_reuses_existing_resume_run() -> None:
    session = MagicMock()
    request = SimpleNamespace(
        request_id="request-1",
        workflow_id="workflow-1",
        checkpoint_id="checkpoint-1",
        source_run_id="run-1",
        status="answered",
        consumed_by_run_id=None,
        tenant_id="route25",
    )
    existing_resume_run = SimpleNamespace(run_id="run-2", attempt_number=2)
    existing_run_snapshot = ExecutionSnapshot.empty()
    existing_run_snapshot.context.execution_context["human_input_request_id"] = "request-1"
    existing_resume_run.plan = existing_run_snapshot.dump()
    lock_query = MagicMock()
    lock_query.scalars.return_value.one_or_none.return_value = request
    existing_resume_query = MagicMock()
    existing_resume_query.scalars.return_value.all.return_value = [existing_resume_run]
    session.execute.side_effect = [lock_query, existing_resume_query]

    with patch("orchestrator.core.runs.human_input_service.close_followup_contexts"):
        result = resume_run_from_human_input_answer(
            session=session,
            settings=SimpleNamespace(secrets_encryption_key="secret-key"),
            request=request,
        )

    assert result is existing_resume_run
    assert request.status == "consumed"
    assert request.consumed_by_run_id == "run-2"


def test_resume_workflow_from_human_input_answer_rejects_unrelated_active_resume_run() -> None:
    session = MagicMock()
    request = SimpleNamespace(
        request_id="request-1",
        workflow_id="workflow-1",
        checkpoint_id="checkpoint-1",
        source_run_id="run-1",
        status="answered",
        consumed_by_run_id=None,
        tenant_id="route25",
        source_stage="review",
    )
    source_run = SimpleNamespace(
        run_id="run-1",
        branch="feature/GP-122",
        pr_url="https://github.com/example/repo/pull/123",
    )
    checkpoint = SimpleNamespace(
        checkpoint_id="checkpoint-1",
        payload_json=ExecutionSnapshot.empty(trigger_context={"source": "manual"}).dump(),
    )
    unrelated_snapshot = ExecutionSnapshot.empty()
    unrelated_snapshot.context.execution_context["human_input_request_id"] = "other-request"
    unrelated_active_run = SimpleNamespace(
        run_id="run-unrelated",
        plan=unrelated_snapshot.dump(),
    )
    lock_query = MagicMock()
    lock_query.scalars.return_value.one_or_none.return_value = request
    existing_resume_query = MagicMock()
    existing_resume_query.scalars.return_value.all.return_value = []
    session.execute.side_effect = [lock_query, existing_resume_query]
    session.get.side_effect = lambda model, key: (
        source_run if key == "run-1" else checkpoint if key == "checkpoint-1" else None
    )
    enqueue_result = SimpleNamespace(
        enqueued=False,
        reason=EnqueueFailureReason.RUN_ALREADY_ACTIVE,
        run=unrelated_active_run,
    )
    runtime = MagicMock()
    runtime.create_attempt.return_value = enqueue_result

    with patch("orchestrator.core.runs.human_input_service.build_workflow_runtime", return_value=runtime):
        try:
            resume_run_from_human_input_answer(
                session=session,
                settings=SimpleNamespace(secrets_encryption_key="secret-key"),
                request=request,
            )
        except ValueError as exc:
            assert "unrelated to this human-input request" in str(exc)
        else:
            raise AssertionError("Expected ValueError for unrelated active resume run")


def test_resume_workflow_from_human_input_answer_rejects_non_canonical_checkpoint_payload() -> None:
    session = MagicMock()
    request = SimpleNamespace(
        request_id="request-1",
        workflow_id="workflow-1",
        checkpoint_id="checkpoint-1",
        source_run_id="run-1",
        status="answered",
        consumed_by_run_id=None,
        tenant_id="route25",
        source_stage="review",
    )
    source_run = SimpleNamespace(
        run_id="run-1",
        branch="feature/GP-122",
        pr_url="https://github.com/example/repo/pull/123",
        required_worker_capability="macos",
        plan=ExecutionSnapshot.empty(trigger_context={"source": "manual"}).dump(),
        pre_check_outcome="ready_for_agent",
    )
    checkpoint = SimpleNamespace(
        checkpoint_id="checkpoint-1",
        payload_json={},
    )
    lock_query = MagicMock()
    lock_query.scalars.return_value.one_or_none.return_value = request
    existing_resume_query = MagicMock()
    existing_resume_query.scalars.return_value.all.return_value = []
    session.execute.side_effect = [lock_query, existing_resume_query]
    session.get.side_effect = lambda model, key: (
        source_run if key == "run-1" else checkpoint if key == "checkpoint-1" else None
    )

    try:
        resume_run_from_human_input_answer(
            session=session,
            settings=SimpleNamespace(secrets_encryption_key="secret-key"),
            request=request,
        )
    except ValueError as exc:
        assert "Unsupported execution snapshot version/shape" in str(exc)
    else:
        raise AssertionError("Expected ValueError for non-canonical checkpoint payload")


def test_create_human_input_request_commits_before_dispatching_discord_message() -> None:
    session = MagicMock()
    tenant = SimpleNamespace(tenant_id="route25")
    project = SimpleNamespace(project_id="route25-default")
    run = SimpleNamespace(
        run_id="run-1",
        workflow_id="workflow-1",
        issue_key="GP-186",
        status="running",
        last_heartbeat_at="heartbeat",
        worker_service_instance_id="worker-1",
        attempt_number=2,
        plan=ExecutionSnapshot.empty(trigger_context={"source": "manual"}).dump(),
    )
    workflow = SimpleNamespace(
        workflow_id="workflow-1",
        status="running",
        active_run_id="run-1",
        latest_checkpoint_id=None,
        updated_at=None,
        last_error=None,
    )
    checkpoint = SimpleNamespace(checkpoint_id="checkpoint-1")
    send_result = SimpleNamespace(
        sent=True,
        channel_id="channel-1",
        thread_channel_id="thread-1",
        message_id="message-1",
        reason=None,
    )
    workflow_query = MagicMock()
    workflow_query.scalars.return_value.one_or_none.return_value = workflow
    pending_query = MagicMock()
    pending_query.scalar_one_or_none.return_value = None
    session.execute.side_effect = [workflow_query, pending_query]
    committed_before_send = {"value": False}

    def _commit() -> None:
        committed_before_send["value"] = True

    def _send_side_effect(**_kwargs):
        assert committed_before_send["value"] is True
        return send_result

    session.commit.side_effect = _commit
    with (
        patch("orchestrator.core.runs.human_input_service.snapshot_checkpoint_for_run", return_value=checkpoint),
        patch("orchestrator.core.runs.human_input_service.send_tenant_discord_message", side_effect=_send_side_effect),
        patch("orchestrator.core.runs.human_input_service.upsert_followup_context"),
    ):
        request = create_human_input_request(
            session=session,
            settings=SimpleNamespace(),
            tenant=tenant,
            project=project,
            run=run,
            issue_key="GP-186",
            source_stage="test",
            request_type="release_decision",
            prompt="Approve release?",
        )

    assert request.thread_channel_id == "thread-1"
    assert session.commit.call_count == 2


def test_create_human_input_request_redelivers_existing_pending_request_without_thread() -> None:
    session = MagicMock()
    tenant = SimpleNamespace(tenant_id="route25")
    project = SimpleNamespace(project_id="route25-default")
    run = SimpleNamespace(
        run_id="run-1",
        workflow_id="workflow-1",
        issue_key="GP-189",
        status="waiting_for_input",
        last_heartbeat_at=None,
        worker_service_instance_id=None,
        attempt_number=1,
        plan=ExecutionSnapshot.empty(trigger_context={"source": "manual"}).dump(),
    )
    existing_request = SimpleNamespace(
        request_id="request-1",
        workflow_id="workflow-1",
        checkpoint_id="checkpoint-1",
        source_run_id="run-1",
        issue_key="GP-189",
        source_stage="dev",
        request_type="credential_input",
        prompt="Provide the credential value",
        instructions=None,
        expected_reply_format=None,
        request_context_json={},
        thread_channel_id=None,
        thread_message_id=None,
    )
    send_result = SimpleNamespace(
        sent=True,
        channel_id="channel-1",
        thread_channel_id="thread-1",
        message_id="message-1",
        reason=None,
    )
    workflow = SimpleNamespace(workflow_id="workflow-1", status="waiting_for_input")
    workflow_query = MagicMock()
    workflow_query.scalars.return_value.one_or_none.return_value = workflow
    pending_query = MagicMock()
    pending_query.scalar_one_or_none.return_value = existing_request
    session.execute.side_effect = [workflow_query, pending_query]

    with (
        patch("orchestrator.core.runs.human_input_service.snapshot_checkpoint_for_run") as checkpoint_mock,
        patch("orchestrator.core.runs.human_input_service.send_tenant_discord_message", return_value=send_result),
        patch("orchestrator.core.runs.human_input_service.upsert_followup_context"),
    ):
        request = create_human_input_request(
            session=session,
            settings=SimpleNamespace(),
            tenant=tenant,
            project=project,
            run=run,
            issue_key="GP-189",
            source_stage="dev",
            request_type="credential_input",
            prompt="Provide the credential value",
        )

    checkpoint_mock.assert_not_called()
    assert request is existing_request
    assert request.thread_channel_id == "thread-1"
