from __future__ import annotations

from datetime import datetime, timedelta, timezone
import tempfile

from orchestrator.api.webhooks.jira_trigger_policy import (
    resolve_decision_gate_cooldown_block,
    resolve_jira_trigger_decision,
    resolve_retry_source,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from tests.workflow_test_support import add_run_with_workflow, make_run


def test_resolve_jira_trigger_decision_disables_plain_status_recheck_in_transition_only_mode() -> (
    None
):
    decision = resolve_jira_trigger_decision(
        comment_command=None,
        webhook_event="jira:issue_updated",
        from_status=None,
        to_status=None,
        ready_trigger_mode="transition_only",
    )

    assert decision.trigger_reason == "status_recheck"
    assert decision.trigger_mode_skip_reason == "ready_status_recheck_disabled"


def test_resolve_jira_trigger_decision_prefers_retry_command() -> None:
    decision = resolve_jira_trigger_decision(
        comment_command="retry",
        webhook_event="comment_created",
        from_status="In Progress",
        to_status="To Do",
        ready_trigger_mode="status_recheck",
    )

    assert decision.trigger_reason == "comment_command_retry"
    assert decision.trigger_mode_skip_reason is None


def test_resolve_jira_trigger_decision_marks_status_transition_to_todo() -> None:
    decision = resolve_jira_trigger_decision(
        comment_command=None,
        webhook_event="jira:issue_updated",
        from_status="In Progress",
        to_status="To Do",
        ready_trigger_mode="status_recheck",
    )

    assert decision.trigger_reason == "status_transition_to_todo"


def test_resolve_decision_gate_cooldown_block_returns_active_cooldown_window() -> None:
    tmp = tempfile.TemporaryDirectory()
    database_url = f"sqlite:///{tmp.name}/jira_trigger_policy.db"
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    session_factory = create_session_factory(database_url=database_url)
    now = datetime.now(timezone.utc)

    with session_factory() as session:
        run = make_run(
            run_id="run-1",
            tenant_id="tenant-1",
            project_id=None,
            issue_key="TP-1",
            issue_summary="Needs clarification",
            issue_description="Missing decision",
            repo_url="https://github.com/example/repo",
            status="blocked",
            last_error="Decision Gate required: Missing GTD sections",
            created_at=now - timedelta(minutes=2),
            finished_at=now - timedelta(minutes=2),
        )
        add_run_with_workflow(session, run, workflow_status="blocked")
        session.commit()

        block = resolve_decision_gate_cooldown_block(
            session=session,
            tenant_id="tenant-1",
            issue_key="TP-1",
            cooldown_window=timedelta(minutes=10),
            now=now,
        )

    assert block is not None
    assert block.run_id == "run-1"
    assert block.remaining_seconds > 0
    tmp.cleanup()
    reset_db_engine_cache()


def test_resolve_retry_source_returns_latest_retryable_run_description() -> None:
    tmp = tempfile.TemporaryDirectory()
    database_url = f"sqlite:///{tmp.name}/jira_trigger_policy_retry.db"
    reset_db_engine_cache()
    run_migrations(database_url=database_url)
    session_factory = create_session_factory(database_url=database_url)
    now = datetime.now(timezone.utc)

    with session_factory() as session:
        run = make_run(
            run_id="run-2",
            tenant_id="tenant-1",
            project_id=None,
            issue_key="TP-2",
            issue_summary="Retry source",
            issue_description="Retry this description",
            repo_url="https://github.com/example/repo",
            status="failed",
            last_error="boom",
            created_at=now,
            finished_at=now,
        )
        add_run_with_workflow(session, run, workflow_status="failed")
        session.commit()

        resolution = resolve_retry_source(
            session=session,
            tenant_id="tenant-1",
            issue_key="TP-2",
            comment_command="retry",
            fallback_issue_description="fallback",
        )

    assert resolution.missing_retryable_run is False
    assert resolution.source_run is not None
    assert resolution.issue_description == "Retry this description"
    tmp.cleanup()
    reset_db_engine_cache()
