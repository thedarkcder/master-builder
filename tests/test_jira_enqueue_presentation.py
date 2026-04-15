from __future__ import annotations

from orchestrator.core.communications.jira_enqueue_presentation import (
    BacklogPreRunCheckPresentation,
    format_backlog_pre_run_check_message,
    format_jira_enqueue_skipped_message,
    normalize_backlog_pre_run_check_text,
)
from orchestrator.core.decision_types import PrecheckOutcome


def test_format_jira_enqueue_skipped_message_includes_reason_and_guidance() -> None:
    message = format_jira_enqueue_skipped_message(
        issue_key="GP-1",
        issue_status="To Do",
        reason="decision_gate_required",
        extra_detail="cycle_id=abc",
    )
    assert "GP-1" in message
    assert "decision_gate_required" in message
    assert "cycle_id=abc" in message


def test_format_backlog_pre_run_check_message_lists_decision_gate_reason() -> None:
    message = format_backlog_pre_run_check_message(
        issue_key="GP-1",
        board_id=10,
        issue_status="Backlog",
        pre_run_check=BacklogPreRunCheckPresentation(
            outcome=PrecheckOutcome.DECISION_GATE_REQUIRED,
            decision_gate_reason="Need architecture sign-off.",
            required_worker_label="worker:linux",
        ),
    )
    assert "Decision Gate reason: Need architecture sign-off." in message
    assert "Required worker capability: `worker:linux`." in message


def test_normalize_backlog_pre_run_check_text_truncates_long_values() -> None:
    value = normalize_backlog_pre_run_check_text("x" * 260, max_chars=20)
    assert value is not None
    assert value.endswith("...")
    assert len(value) == 20
