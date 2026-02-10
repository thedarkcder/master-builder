from orchestrator.core.worker_stage_events import decision_gate_required_update, run_failed_update


def test_decision_gate_required_update_contains_stage_and_messages() -> None:
    update = decision_gate_required_update(
        tenant_id="tenant-1",
        issue_key="example-46",
        run_id="run-1",
        jira_url="https://example.test/browse/example-46",
        reason="Missing acceptance criteria",
        questions=["What is in scope?"],
    )
    assert update["stage"] == "decision_gate_required"
    assert "Missing acceptance criteria" in update["jira_message"]
    assert "example-46" in update["discord_message"]


def test_run_failed_update_contains_next_steps_guidance() -> None:
    update = run_failed_update(
        tenant_id="tenant-1",
        issue_key="example-46",
        run_id="run-1",
        jira_url="https://example.test/browse/example-46",
        error="Runner crashed",
    )
    assert update["stage"] == "run_failed"
    assert "Runner crashed" in update["jira_message"]
    assert "Review diagnostics" in update["discord_message"]
