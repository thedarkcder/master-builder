from types import SimpleNamespace

import pytest

from orchestrator.core.decision.engine import (
    _evaluate_terminal_execution_readiness_ingress,
    evaluate_ingress_precheck,
)
from orchestrator.core.decision.precheck_mapping import evaluate_with_labels
from orchestrator.core.precheck.pre_run_check import evaluate_pre_run_check


@pytest.mark.parametrize("capability", ["linux", "macos"])
@pytest.mark.parametrize(
    "callback",
    [evaluate_ingress_precheck, _evaluate_terminal_execution_readiness_ingress],
)
def test_project_policy_reaches_actual_readiness_precheck(callback, capability):
    decision = callback(
        source="jira_webhook",
        tenant_id="example-tenant",
        project_id="example-project",
        project_policy_overrides={"default_worker_capability": capability},
        issue_key="EXAMPLE-1",
        issue_summary="Validate project worker policy",
        issue_description="A generic task with no platform hints.",
        issue_labels=["worker:invalid"],
        ready_label="agent:ready",
        evaluate_pre_run_check_fn=evaluate_pre_run_check,
    )
    assert decision.policy_error is None
    assert decision.pre_check.required_worker_capability == capability
    assert decision.pre_check.required_worker_label == f"worker:{capability}"
    assert decision.pre_check.gtd_valid is False


@pytest.mark.parametrize(
    "callback",
    [evaluate_ingress_precheck, _evaluate_terminal_execution_readiness_ingress],
)
def test_project_mapping_preserves_policy_for_actual_engine_callback(callback):
    evaluation = evaluate_with_labels(
        session=SimpleNamespace(),
        tenant=SimpleNamespace(
            tenant_id="example-tenant",
            policy_config={"allow_label_mutations": False},
            jira_config={"ready_label": "agent:ready"},
        ),
        project=SimpleNamespace(
            project_id="example-project",
            policy_overrides={"default_worker_capability": "macos"},
        ),
        source="jira_webhook",
        issue_key="EXAMPLE-1",
        issue_summary="Validate selected project policy",
        issue_description="A generic task with no platform hints.",
        recorded_answers=None,
        issue_labels=["worker:invalid"],
        settings=SimpleNamespace(),
        tenant_atlassian_oauth_context_fn=lambda **_: pytest.fail(
            "Disabled label mutation must not request external OAuth"
        ),
        evaluate_ingress_precheck_fn=callback,
        evaluate_pre_run_check_fn=evaluate_pre_run_check,
    )
    assert evaluation.decision.policy_error is None
    assert evaluation.decision.pre_check.required_worker_capability == "macos"
    assert evaluation.decision.pre_check.gtd_valid is False
    assert evaluation.issue_labels == ["worker:invalid"]
