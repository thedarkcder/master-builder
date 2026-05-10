from orchestrator.core.worker.stage_events import decision_gate_required_update, run_failed_update
from orchestrator.core.worker.stage_event_types import WorkerStageEvent


def test_decision_gate_required_update_contains_stage_and_messages() -> None:
    update = decision_gate_required_update(
        tenant_id="tenant-1",
        issue_key="YANA-46",
        run_id="run-1",
        jira_url="https://example.test/browse/YANA-46",
        reason="Missing acceptance criteria",
        questions=["What is in scope?"],
    )
    assert update.stage == WorkerStageEvent.DECISION_GATE_REQUIRED
    assert "Missing acceptance criteria" in update.jira_message
    assert "YANA-46" in update.discord_message


def test_run_failed_update_contains_next_steps_guidance() -> None:
    update = run_failed_update(
        tenant_id="tenant-1",
        issue_key="YANA-46",
        run_id="run-1",
        jira_url="https://example.test/browse/YANA-46",
        error="Runner crashed",
    )
    assert update.stage == WorkerStageEvent.RUN_FAILED
    assert "Runner crashed" in update.jira_message
    assert "Review diagnostics" in update.discord_message


def test_run_failed_update_preserves_multiline_auth_guidance_in_discord_message() -> None:
    error = """PM stage failed: Codex CLI is not authenticated.

Welcome to Codex [v0.118.0]
OpenAI's command-line coding agent

1. Open this link in your browser and sign in to your account
   https://auth.openai.com/codex/device
"""
    update = run_failed_update(
        tenant_id="route25",
        issue_key="GP-186",
        run_id="run-123",
        jira_url="https://example.test/browse/GP-186",
        run_url="https://admin.example.test/runs/run-123",
        error=error,
    )

    assert "Error: PM stage failed: Codex CLI is not authenticated." in update.discord_message
    assert "\n\nWelcome to Codex [v0.118.0]\nOpenAI's command-line coding agent\n\n" in update.discord_message
    assert "https://auth.openai.com/codex/device" in update.discord_message
