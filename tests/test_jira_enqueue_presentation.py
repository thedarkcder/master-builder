from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.communications.jira_enqueue_presentation import (
    BacklogPreRunCheckPresentation,
    build_backlog_pre_run_check_presentation,
    format_backlog_pre_run_check_message,
    format_jira_enqueue_skipped_message,
    normalize_backlog_pre_run_check_text,
)
from orchestrator.core.decision.state_machine import build_execution_admission_block, ExecutionAdmissionReason
from orchestrator.core.decision.types import PrecheckOutcome


def test_format_jira_enqueue_skipped_message_includes_reason_and_guidance() -> None:
    message = format_jira_enqueue_skipped_message(
        issue_key="GP-1",
        issue_status="To Do",
        admission=build_execution_admission_block(
            reason=ExecutionAdmissionReason.DECISION_GATE_REQUIRED,
        ),
        extra_detail="cycle_id=abc",
    )
    assert "GP-1" in message
    assert "decision_gate_required" in message
    assert "cycle_id=abc" in message


def test_format_jira_enqueue_skipped_message_links_jira_issue_when_url_is_available() -> None:
    message = format_jira_enqueue_skipped_message(
        issue_key="AP-322",
        issue_url="https://bsktpay.atlassian.net/browse/AP-322",
        issue_status="To Do",
        admission=build_execution_admission_block(
            reason=ExecutionAdmissionReason.GTD_REQUIRED,
        ),
    )

    assert "for [AP-322](https://bsktpay.atlassian.net/browse/AP-322)." in message
    assert "for `AP-322`." not in message


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


def test_build_backlog_pre_run_check_presentation_uses_typed_decision_result() -> None:
    decision_result = SimpleNamespace(
        decision=SimpleNamespace(
            pre_check=SimpleNamespace(
                outcome="gtd_required",
                ready_label="agent:ready",
                decision_gate_reason="Decision Gate not required",
                gtd_missing_criteria=("dependencies_and_risks",),
                required_worker_label="worker:linux",
            )
        )
    )

    presentation = build_backlog_pre_run_check_presentation(
        decision_result=decision_result,
        ready_label="agent:ready",
    )

    assert presentation.outcome is PrecheckOutcome.GTD_REQUIRED
    assert presentation.ready_label == "agent:ready"
    assert presentation.gtd_missing_criteria == ("dependencies_and_risks",)
    assert presentation.required_worker_label == "worker:linux"
