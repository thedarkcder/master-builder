from orchestrator.core.pre_run_check import evaluate_pre_run_check
from orchestrator.core.decision_gate import DecisionGateResult
from orchestrator.core.gtd import GoodToDoValidationResult
from orchestrator.core.precheck_policy import PrecheckPolicyResult
from unittest.mock import patch


def _policy_result(*, decision_gate: DecisionGateResult, gtd: GoodToDoValidationResult) -> PrecheckPolicyResult:
    return PrecheckPolicyResult(
        decision_gate=decision_gate,
        gtd=gtd,
    )


def test_pre_run_check_ready_for_agent_when_label_present_and_gate_clear() -> None:
    with (
        patch("orchestrator.core.pre_run_check.infer_required_worker_capability", return_value="linux"),
        patch(
            "orchestrator.core.pre_run_check.evaluate_precheck_policy",
            return_value=_policy_result(
                decision_gate=DecisionGateResult(
                    triggered=False,
                    reason="Decision Gate not required",
                    missing_sections=(),
                    questions=(),
                    recommendation="Proceed",
                    tags=(),
                ),
                gtd=GoodToDoValidationResult(
                    valid=True,
                    missing_criteria=(),
                    clarification_questions=(),
                ),
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
            "orchestrator.core.pre_run_check.evaluate_precheck_policy",
            return_value=_policy_result(
                decision_gate=DecisionGateResult(
                    triggered=False,
                    reason="Decision Gate not required",
                    missing_sections=(),
                    questions=(),
                    recommendation="Proceed",
                    tags=(),
                ),
                gtd=GoodToDoValidationResult(
                    valid=True,
                    missing_criteria=(),
                    clarification_questions=(),
                ),
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
            "orchestrator.core.pre_run_check.evaluate_precheck_policy",
            return_value=_policy_result(
                decision_gate=DecisionGateResult(
                    triggered=True,
                    reason="Missing GTD sections: Objective",
                    missing_sections=("Objective",),
                    questions=("What is objective?",),
                    recommendation="Decision required before build",
                    tags=("[NEEDS-PM]",),
                ),
                gtd=GoodToDoValidationResult(
                    valid=True,
                    missing_criteria=(),
                    clarification_questions=(),
                ),
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
            "orchestrator.core.pre_run_check.evaluate_precheck_policy",
            return_value=_policy_result(
                decision_gate=DecisionGateResult(
                    triggered=False,
                    reason="Decision Gate not required",
                    missing_sections=(),
                    questions=(),
                    recommendation="Proceed",
                    tags=(),
                ),
                gtd=GoodToDoValidationResult(
                    valid=True,
                    missing_criteria=(),
                    clarification_questions=(),
                ),
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
            "orchestrator.core.pre_run_check.evaluate_precheck_policy",
            return_value=_policy_result(
                decision_gate=DecisionGateResult(
                    triggered=False,
                    reason="Decision Gate not required",
                    missing_sections=(),
                    questions=(),
                    recommendation="Proceed",
                    tags=(),
                ),
                gtd=GoodToDoValidationResult(
                    valid=False,
                    missing_criteria=("Dependencies and risks identified",),
                    clarification_questions=("Which dependencies or risks may impact delivery?",),
                ),
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


def test_pre_run_check_passes_recorded_answers_to_policy() -> None:
    with (
        patch("orchestrator.core.pre_run_check.infer_required_worker_capability", return_value="linux"),
        patch("orchestrator.core.pre_run_check.evaluate_precheck_policy") as policy_mock,
    ):
        policy_mock.return_value = _policy_result(
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
            gtd=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        )
        result = evaluate_pre_run_check(
            issue_summary="GP-80",
            issue_description="desc",
            recorded_answers=[
                {
                    "question_id": "dg_1",
                    "question_text": "What config is approved?",
                    "status": "answered",
                    "answer": "Production bundle ID is com.example.app.",
                }
            ],
            issue_labels=["agent:ready"],
            ready_label="agent:ready",
        )
    assert result.outcome == "ready_for_agent"
    assert policy_mock.call_args.kwargs["recorded_answers"] == [
        {
            "question_id": "dg_1",
            "question_text": "What config is approved?",
            "status": "answered",
            "answer": "Production bundle ID is com.example.app.",
        }
    ]


def test_pre_run_check_forwards_run_id_to_precheck_policy() -> None:
    captured: dict[str, object] = {}

    def _capture_policy(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return _policy_result(
            decision_gate=DecisionGateResult(
                triggered=False,
                reason="Decision Gate not required",
                missing_sections=(),
                questions=(),
                recommendation="Proceed",
                tags=(),
            ),
            gtd=GoodToDoValidationResult(
                valid=True,
                missing_criteria=(),
                clarification_questions=(),
            ),
        )

    with patch("orchestrator.core.pre_run_check.evaluate_precheck_policy", side_effect=_capture_policy):
        result = evaluate_pre_run_check(
            tenant_id="tenant-1",
            project_id="project-1",
            issue_key="TP-1",
            run_id="run-123",
            issue_summary="summary",
            issue_description="desc",
            issue_labels=["agent:ready"],
            ready_label="agent:ready",
        )

    assert result.outcome == "ready_for_agent"
    assert captured["run_id"] == "run-123"
