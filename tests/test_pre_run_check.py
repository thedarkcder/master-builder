from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from unittest.mock import patch


def test_pre_run_check_ready_for_agent_when_label_present_and_gate_clear() -> None:
    with (
        patch("orchestrator.core.pre_run_check.infer_required_worker_capability", return_value="linux"),
        patch(
            "orchestrator.core.pre_run_check.evaluate_decision_gate",
            return_value=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
        ),
        patch(
            "orchestrator.core.pre_run_check.validate_good_to_do",
            return_value=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        ),
    ):
        result = evaluate_pre_run_check(
            issue_summary="GP-80",
            issue_description="desc",
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
    with (
        patch("orchestrator.core.pre_run_check.infer_required_worker_capability", return_value="linux"),
        patch(
            "orchestrator.core.pre_run_check.evaluate_decision_gate",
            return_value=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
        ),
        patch(
            "orchestrator.core.pre_run_check.validate_good_to_do",
            return_value=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        ),
    ):
        result = evaluate_pre_run_check(
            issue_summary="GP-80",
            issue_description="desc",
            issue_labels=[],
            ready_label="agent:ready",
        )
    assert result.outcome == "missing_ready_label"
    assert result.ready_label_present is False
    assert result.decision_gate_triggered is False
    assert result.required_worker_capability == "linux"


def test_pre_run_check_decision_gate_required_takes_priority() -> None:
    with (
        patch("orchestrator.core.pre_run_check.infer_required_worker_capability", return_value="linux"),
        patch(
            "orchestrator.core.pre_run_check.evaluate_decision_gate",
            return_value=DecisionGateResult(
                triggered=True,
                reason="Missing GTD sections: Objective",
                missing_sections=("Objective",),
                questions=("What is objective?",),
                recommendation="Decision required before build",
                tags=("[NEEDS-PM]",),
            ),
        ),
        patch(
            "orchestrator.core.pre_run_check.validate_good_to_do",
            return_value=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        ),
    ):
        result = evaluate_pre_run_check(
            issue_summary="GP-80",
            issue_description="Missing sections",
            issue_labels=["agent:ready"],
            ready_label="agent:ready",
        )
    assert result.outcome == "decision_gate_required"
    assert result.decision_gate_triggered is True


def test_pre_run_check_infers_macos_worker_requirement() -> None:
    with (
        patch("orchestrator.core.pre_run_check.infer_required_worker_capability", return_value="macos"),
        patch(
            "orchestrator.core.pre_run_check.evaluate_decision_gate",
            return_value=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
        ),
        patch(
            "orchestrator.core.pre_run_check.validate_good_to_do",
            return_value=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        ),
    ):
        result = evaluate_pre_run_check(
            issue_summary="Build iOS onboarding flow",
            issue_description="desc",
            issue_labels=["agent:ready", "worker:macos"],
            ready_label="agent:ready",
        )
    assert result.required_worker_capability == "macos"
    assert result.required_worker_label == "worker:macos"
    assert result.required_worker_label_present is True


def test_pre_run_check_requires_gtd_before_ready_for_agent() -> None:
    with (
        patch("orchestrator.core.pre_run_check.infer_required_worker_capability", return_value="linux"),
        patch(
            "orchestrator.core.pre_run_check.evaluate_decision_gate",
            return_value=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
        ),
        patch(
            "orchestrator.core.pre_run_check.validate_good_to_do",
            return_value=GoodToDoValidationResult(
                valid=False,
                missing_criteria=("Dependencies and risks identified",),
                clarification_questions=("Which dependencies or risks may impact delivery?",),
            ),
        ),
    ):
        result = evaluate_pre_run_check(
            issue_summary="GP-80",
            issue_description="desc",
            issue_labels=["agent:ready"],
            ready_label="agent:ready",
        )
    assert result.outcome == "gtd_required"
    assert result.gtd_valid is False
