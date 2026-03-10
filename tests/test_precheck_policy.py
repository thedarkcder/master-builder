from unittest.mock import patch

import pytest

from orchestrator.core.precheck_policy import evaluate_precheck_policy


def test_evaluate_precheck_policy_returns_decision_gate_and_gtd() -> None:
    with patch(
        "orchestrator.core.precheck_policy.invoke_codex_json",
        return_value={
            "triggered": False,
            "reason": "Decision Gate not required",
            "missing_sections": [],
            "questions": [],
            "recommendation": "Proceed",
            "tags": [],
            "gtd_valid": True,
            "gtd_missing_criteria": [],
            "gtd_clarification_questions": [],
        },
    ), patch(
        "orchestrator.core.precheck_policy.build_codex_runtime",
        return_value=object(),
    ):
        result = evaluate_precheck_policy(
            issue_summary="TP-1",
            issue_description="Objective and how to test are clear.",
            tenant_id="tenant-1",
            project_id="project-1",
            issue_key="TP-1",
        )

    assert result.decision_gate.triggered is False
    assert result.decision_gate.reason == "Decision Gate not required"
    assert result.gtd.valid is True


def test_evaluate_precheck_policy_passes_recorded_answers_to_prompt() -> None:
    with patch(
        "orchestrator.core.precheck_policy.invoke_codex_json",
        return_value={
            "triggered": False,
            "reason": "Decision Gate not required",
            "missing_sections": [],
            "questions": [],
            "recommendation": "Proceed",
            "tags": [],
            "gtd_valid": True,
            "gtd_missing_criteria": [],
            "gtd_clarification_questions": [],
        },
    ) as invoke_mock, patch(
        "orchestrator.core.precheck_policy.build_codex_runtime",
        return_value=object(),
    ):
        evaluate_precheck_policy(
            issue_summary="TP-1",
            issue_description="Description",
            recorded_answers=[
                {
                    "question_id": "dg_1",
                    "question_text": "What config is approved?",
                    "status": "answered",
                    "answer": "Production bundle ID is com.example.app.",
                }
            ],
            tenant_id="tenant-1",
            project_id="project-1",
            issue_key="TP-1",
        )

    assert "Production bundle ID is com.example.app." in invoke_mock.call_args.kwargs["user_prompt"]


def test_evaluate_precheck_policy_rejects_invalid_gtd_payload() -> None:
    with patch(
        "orchestrator.core.precheck_policy.invoke_codex_json",
        return_value={
            "triggered": False,
            "reason": "Decision Gate not required",
            "missing_sections": [],
            "questions": [],
            "recommendation": "Proceed",
            "tags": [],
            "gtd_valid": False,
            "gtd_missing_criteria": ["How to test missing"],
            "gtd_clarification_questions": [],
        },
    ), patch(
        "orchestrator.core.precheck_policy.build_codex_runtime",
        return_value=object(),
    ):
        with pytest.raises(RuntimeError, match="invalid GTD result"):
            evaluate_precheck_policy(
                issue_summary="TP-1",
                issue_description="Description",
                tenant_id="tenant-1",
                project_id="project-1",
                issue_key="TP-1",
            )


def test_evaluate_precheck_policy_passes_run_id_into_codex_context() -> None:
    captured: dict[str, object] = {}

    def _capture_invoke(*, runtime, context, system_prompt, user_prompt, **kwargs):  # noqa: ANN001
        _ = runtime, system_prompt, user_prompt, kwargs
        captured["context"] = context
        return {
            "triggered": False,
            "reason": "Decision Gate not required",
            "missing_sections": [],
            "questions": [],
            "recommendation": "Proceed",
            "tags": [],
            "gtd_valid": True,
            "gtd_missing_criteria": [],
            "gtd_clarification_questions": [],
        }

    with patch(
        "orchestrator.core.precheck_policy.invoke_codex_json",
        side_effect=_capture_invoke,
    ), patch(
        "orchestrator.core.precheck_policy.build_codex_runtime",
        return_value=object(),
    ):
        result = evaluate_precheck_policy(
            issue_summary="TP-1",
            issue_description="Objective and how to test are clear.",
            tenant_id="tenant-1",
            project_id="project-1",
            issue_key="TP-1",
            run_id="run-123",
        )

    assert result.gtd.valid is True
    assert captured["context"].run_id == "run-123"
