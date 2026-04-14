from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.core.run_human_input_service import (
    create_human_input_request,
    resume_run_from_human_input_reply,
)


def test_create_human_input_request_allows_dev_stage_without_persisted_pm_plan() -> None:
    session = MagicMock()
    tenant = SimpleNamespace(tenant_id="example")
    project = SimpleNamespace(project_id="example-default")
    run = SimpleNamespace(
        run_id="run-1",
        issue_key="GP-122",
        dev_session_id="dev-session-1",
        pm_session_id=None,
        orchestrated_session_id=None,
        plan={},
    )
    send_result = SimpleNamespace(
        sent=True,
        thread_channel_id="thread-1",
        message_id="message-1",
        reason=None,
    )

    with patch(
        "orchestrator.core.run_human_input_service.send_tenant_discord_message",
        return_value=send_result,
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

    assert request.resume_stage == "dev"
    assert request.resume_session_id == "dev-session-1"
    assert request.request_context_json == {}


def test_resume_run_from_human_input_reply_uses_request_context_resume_source_plan() -> None:
    session = MagicMock()
    request = SimpleNamespace(
        request_id="request-1",
        source_run_id="run-1",
        status="pending",
        request_context_json={"resume_source_plan": {"plan_steps": ["Verify Apple Sign In"]}},
        resume_stage="dev",
        resume_session_id="dev-session-1",
        answered_at=None,
        updated_at=None,
        answer_encrypted=None,
        answer_source_ref=None,
        resumed_run_id=None,
    )
    source_run = SimpleNamespace(
        run_id="run-1",
        tenant_id="example",
        project_id="example-default",
        issue_key="GP-122",
        issue_summary="Apple Sign In",
        issue_description="Verify Apple Sign In",
        repo_url="https://github.com/example/repo",
        plan={},
    )
    resumed_run = SimpleNamespace(
        run_id="run-2",
        plan={},
        dev_session_id=None,
        pm_session_id=None,
        orchestrated_session_id=None,
    )
    session.get.return_value = source_run
    enqueue_result = SimpleNamespace(enqueued=True, reason=None, run=resumed_run)

    with (
        patch("orchestrator.core.run_human_input_service.encrypt_value", return_value="encrypted"),
        patch("orchestrator.core.run_human_input_service.enqueue_run", return_value=enqueue_result),
    ):
        result = resume_run_from_human_input_reply(
            session=session,
            settings=SimpleNamespace(secrets_encryption_key="secret-key"),
            request=request,
            reply_text="123456",
            source_ref="discord:message-1",
        )

    assert result is resumed_run
    assert resumed_run.dev_session_id == "dev-session-1"
    assert resumed_run.plan["trigger_context"]["resume_source_plan"] == {"plan_steps": ["Verify Apple Sign In"]}
    assert resumed_run.plan["trigger_context"]["human_input_request_ids"] == ["request-1"]
