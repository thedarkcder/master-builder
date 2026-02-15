from orchestrator.core.pre_run_check import evaluate_pre_run_check


def test_pre_run_check_ready_for_agent_when_label_present_and_gate_clear() -> None:
    result = evaluate_pre_run_check(
        issue_summary="GP-80",
        issue_description=(
            "Objective: x\n"
            "Scope: y\n"
            "Acceptance Criteria: z\n"
            "How to test: q\n"
            "NFR intent: MVP"
        ),
        issue_labels=["agent:ready"],
        ready_label="agent:ready",
    )
    assert result.outcome == "ready_for_agent"
    assert result.ready_label_present is True
    assert result.decision_gate_triggered is False
    assert result.required_worker_capability == "linux"
    assert result.required_worker_label == "worker:linux"
    assert result.required_worker_label_present is False


def test_pre_run_check_missing_ready_label_when_gate_clear() -> None:
    result = evaluate_pre_run_check(
        issue_summary="GP-80",
        issue_description=(
            "Objective: x\n"
            "Scope: y\n"
            "Acceptance Criteria: z\n"
            "How to test: q\n"
            "NFR intent: MVP"
        ),
        issue_labels=[],
        ready_label="agent:ready",
    )
    assert result.outcome == "missing_ready_label"
    assert result.ready_label_present is False
    assert result.decision_gate_triggered is False
    assert result.required_worker_capability == "linux"


def test_pre_run_check_decision_gate_required_takes_priority() -> None:
    result = evaluate_pre_run_check(
        issue_summary="GP-80",
        issue_description="Missing sections",
        issue_labels=["agent:ready"],
        ready_label="agent:ready",
    )
    assert result.outcome == "decision_gate_required"
    assert result.decision_gate_triggered is True


def test_pre_run_check_infers_macos_worker_requirement() -> None:
    result = evaluate_pre_run_check(
        issue_summary="Build iOS onboarding flow",
        issue_description=(
            "Objective: add SwiftUI onboarding.\n"
            "Scope: iOS app only.\n"
            "Acceptance Criteria: onboarding works.\n"
            "How to test: run Xcode build.\n"
            "NFR intent: MVP"
        ),
        issue_labels=["agent:ready", "worker:macos"],
        ready_label="agent:ready",
    )
    assert result.required_worker_capability == "macos"
    assert result.required_worker_label == "worker:macos"
    assert result.required_worker_label_present is True
