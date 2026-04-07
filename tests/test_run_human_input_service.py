from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.core.run_human_input_service import (
    answer_human_input_request,
    create_human_input_request,
    resume_workflow_from_human_input_answer,
)


def test_create_human_input_request_snapshots_checkpoint_and_moves_run_to_waiting() -> None:
    session = MagicMock()
    tenant = SimpleNamespace(tenant_id="example")
    project = SimpleNamespace(project_id="example-default")
    run = SimpleNamespace(
        run_id="run-1",
        workflow_id="workflow-1",
        issue_key="GP-122",
        status="running",
        last_heartbeat_at="heartbeat",
        worker_service_instance_id="worker-1",
        plan={"plan": {"plan_steps": ["Verify Apple Sign In"]}},
    )
    workflow = SimpleNamespace(
        workflow_id="workflow-1",
        status="running",
        active_run_id="run-1",
        latest_checkpoint_id=None,
        blocked_reason=None,
        updated_at=None,
    )
    session.execute.return_value.scalar_one_or_none.return_value = None
    session.get.side_effect = lambda model, key: workflow if key == "workflow-1" else None
    checkpoint = SimpleNamespace(checkpoint_id="checkpoint-1")
    send_result = SimpleNamespace(
        sent=True,
        channel_id="channel-1",
        thread_channel_id="thread-1",
        message_id="message-1",
        reason=None,
    )

    with (
        patch("orchestrator.core.run_human_input_service.snapshot_checkpoint_for_run", return_value=checkpoint),
        patch("orchestrator.core.run_human_input_service.send_tenant_discord_message", return_value=send_result),
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
    tenant = SimpleNamespace(tenant_id="example")
    project = SimpleNamespace(project_id="example-default")
    run = SimpleNamespace(
        run_id="run-1",
        workflow_id="workflow-1",
        issue_key="GP-125",
        status="running",
        last_heartbeat_at="heartbeat",
        worker_service_instance_id="worker-1",
        plan={"plan": {"plan_steps": ["Clarify fallback policy"]}},
    )
    workflow = SimpleNamespace(
        workflow_id="workflow-1",
        status="running",
        active_run_id="run-1",
        latest_checkpoint_id=None,
        blocked_reason=None,
        updated_at=None,
    )
    session.execute.return_value.scalar_one_or_none.return_value = None
    session.get.side_effect = lambda model, key: workflow if key == "workflow-1" else None
    checkpoint = SimpleNamespace(checkpoint_id="checkpoint-1")
    send_result = SimpleNamespace(
        sent=True,
        channel_id="channel-1",
        thread_channel_id="thread-1",
        message_id="message-1",
        reason=None,
    )

    with (
        patch("orchestrator.core.run_human_input_service.snapshot_checkpoint_for_run", return_value=checkpoint),
        patch("orchestrator.core.run_human_input_service.send_tenant_discord_message", return_value=send_result) as send_mock,
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
        answer_encrypted=None,
        answer_source_ref=None,
        consumed_by_run_id=None,
        tenant_id="example",
        source_stage="review",
        expires_at=None,
    )
    source_run = SimpleNamespace(
        run_id="run-1",
        tenant_id="example",
        project_id="example-default",
        issue_key="GP-122",
        issue_summary="Apple Sign In",
        issue_description="Verify Apple Sign In",
        repo_url="https://github.com/example/repo",
        branch="feature/GP-122",
        pr_url="https://github.com/example/repo/pull/123",
        plan={"review_feedback": "Confirm nonce handling"},
    )
    checkpoint = SimpleNamespace(
        checkpoint_id="checkpoint-1",
        payload_json={"review_feedback": "Confirm nonce handling"},
    )
    resumed_run = SimpleNamespace(run_id="run-2")
    session.get.side_effect = lambda model, key: (
        source_run if key == "run-1" else checkpoint if key == "checkpoint-1" else None
    )
    enqueue_result = SimpleNamespace(enqueued=True, reason=None, run=resumed_run)

    with (
        patch("orchestrator.core.run_human_input_service.encrypt_value", return_value="encrypted"),
        patch("orchestrator.core.run_human_input_service.enqueue_run", return_value=enqueue_result) as enqueue_run_mock,
    ):
        answered = answer_human_input_request(
            session=session,
            settings=SimpleNamespace(secrets_encryption_key="secret-key"),
            request=request,
            reply_text="use qa-apple@example.com",
            source_ref="discord:message-1",
        )
        result = resume_workflow_from_human_input_answer(
            session=session,
            settings=SimpleNamespace(secrets_encryption_key="secret-key"),
            request=answered,
        )

    assert result is resumed_run
    bootstrap = enqueue_run_mock.call_args.kwargs["bootstrap"]
    assert bootstrap.workflow_id == "workflow-1"
    assert bootstrap.parent_run_id == "run-1"
    assert bootstrap.entry_mode == "resume"
    assert bootstrap.entry_stage == "review"
    assert bootstrap.entry_checkpoint_id == "checkpoint-1"
    assert bootstrap.branch == "feature/GP-122"
    assert request.status == "consumed"
    assert request.consumed_by_run_id == "run-2"
