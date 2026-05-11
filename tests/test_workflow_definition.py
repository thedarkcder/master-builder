from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.core.workflow.definition import (
    WorkflowDefinition,
    WorkflowDefinitionRegistry,
    WorkflowStepKind,
    WorkflowWorkUnitIdempotencyPolicy,
    WorkflowWorkUnitKind,
    infer_workflow_steps,
    infer_workflow_work_units,
    workflow_step,
    workflow_work_unit,
)
from orchestrator.core.workflow.handler_composition import installed_operation_retry_capabilities
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.temporal.workflow_registry import resolve_temporal_binding_for_handler


def test_workflow_step_decorator_infers_ordered_graph() -> None:
    class ExampleWorkflow:
        @workflow_step(key="notify", label="Notify", kind=WorkflowStepKind.NOTIFICATION, after="build", required=False)
        def notify(self) -> None:
            raise NotImplementedError

        @workflow_step(key="build", label="Build", kind=WorkflowStepKind.BUSINESS)
        def build(self) -> None:
            raise NotImplementedError

    steps = infer_workflow_steps(ExampleWorkflow)

    assert [(step.key, step.kind, step.after, step.supports, step.graph_index) for step in steps] == [
        ("build", WorkflowStepKind.BUSINESS, (), (), 0),
        ("notify", WorkflowStepKind.NOTIFICATION, ("build",), (), 1),
    ]
    assert not any(step.retryable for step in steps)


def test_registered_parent_planning_retryable_steps_have_executable_capabilities() -> None:
    workflow_type = get_workflow_type(workflow_type_key="parent_planning")

    executable_retry_types = {
        capability.operation_type
        for capability in installed_operation_retry_capabilities(workflow_type=workflow_type)
    }
    retryable_steps = {step.key for step in workflow_type.steps if step.retryable}

    assert retryable_steps == {
        "jira_parent_update",
        "backlog_planning",
        "jira_child_fanout",
    }
    assert retryable_steps == executable_retry_types


def test_registered_jira_project_reconciliation_workflow_has_expected_steps_and_work_units() -> None:
    workflow_type = get_workflow_type(workflow_type_key="jira_project_reconciliation")

    assert [step.key for step in workflow_type.steps] == [
        "jira_project_scan",
        "jira_issue_classification",
        "jira_label_reconciliation",
        "parent_workflow_reconciliation",
        "reconciliation_summary",
    ]
    assert {unit.key for unit in workflow_type.work_units} == {
        "jira_project_scan.page_fetch",
        "jira_project_scan.issue_detail_fetch",
        "jira_issue_classification.compute",
        "jira_label_reconciliation.label_replace",
        "parent_workflow_reconciliation.parent_upsert",
        "reconciliation_summary.compute",
    }
    assert {step.key for step in workflow_type.steps if step.retryable} == {
        "jira_project_scan",
        "jira_issue_classification",
        "jira_label_reconciliation",
        "parent_workflow_reconciliation",
        "reconciliation_summary",
    }


def test_registered_project_deployment_setup_uses_dedicated_temporal_workflow() -> None:
    workflow_type = get_workflow_type(workflow_type_key="project_deployment_setup")
    binding = resolve_temporal_binding_for_handler(handler_key=workflow_type.handler_key)

    assert workflow_type.orchestration_backend == "temporal"
    assert workflow_type.handler_key == "project_deployment_setup"
    assert binding.workflow_name == "ProjectDeploymentSetupWorkflow"
    assert binding.execution_mode == "setup"
    assert [step.key for step in workflow_type.steps] == [
        "repo_deployment_analysis",
        "deployment_configuration",
        "initial_release",
    ]
    assert {unit.key for unit in workflow_type.work_units} == {
        "repo_deployment_analysis.checkout",
        "repo_deployment_analysis.analyze",
        "deployment_configuration.prepare_apps",
        "initial_release.create",
    }


def test_parent_planning_supporting_steps_declare_visual_owners() -> None:
    workflow_type = get_workflow_type(workflow_type_key="parent_planning")
    steps = {step.key: step for step in workflow_type.steps}

    assert steps["jira_comment_projection"].supports == (
        "backlog_planning",
        "pm_decision_resolution",
        "jira_child_fanout",
    )
    assert steps["jira_parent_update"].supports == ("backlog_planning", "jira_child_fanout")
    assert steps["discord_followup_projection"].supports == ("backlog_planning", "jira_child_fanout")
    assert steps["jira_child_promotion"].supports == ("jira_child_fanout",)


def test_registered_workflow_retry_metadata_never_exceeds_installed_capabilities() -> None:
    for workflow_type_key in ("issue_execution", "parent_planning", "pr_remediation"):
        workflow_type = get_workflow_type(workflow_type_key=workflow_type_key)
        executable_retry_types = {
            capability.operation_type
            for capability in installed_operation_retry_capabilities(workflow_type=workflow_type)
        }
        retryable_steps = {step.key for step in workflow_type.steps if step.retryable}

        assert retryable_steps <= executable_retry_types


def test_run_workflows_do_not_expose_retry_without_registered_executor() -> None:
    for workflow_type_key in ("issue_execution", "pr_remediation"):
        workflow_type = get_workflow_type(workflow_type_key=workflow_type_key)

        assert installed_operation_retry_capabilities(workflow_type=workflow_type) == ()
        assert {step.key for step in workflow_type.steps if step.retryable} == set()


def test_workflow_step_graph_rejects_missing_dependencies() -> None:
    class BrokenWorkflow:
        @workflow_step(key="child", label="Child", kind=WorkflowStepKind.BUSINESS, after="missing")
        def child(self) -> None:
            raise NotImplementedError

    with pytest.raises(ValueError, match="depends on unknown step missing"):
        infer_workflow_steps(BrokenWorkflow)


def test_workflow_step_graph_rejects_cycles() -> None:
    class BrokenWorkflow:
        @workflow_step(key="first", label="First", kind=WorkflowStepKind.BUSINESS, after="second")
        def first(self) -> None:
            raise NotImplementedError

        @workflow_step(key="second", label="Second", kind=WorkflowStepKind.BUSINESS, after="first")
        def second(self) -> None:
            raise NotImplementedError

    with pytest.raises(ValueError, match="cycle"):
        infer_workflow_steps(BrokenWorkflow)


def test_workflow_step_graph_rejects_missing_support_owner() -> None:
    class BrokenWorkflow:
        @workflow_step(key="notify", label="Notify", kind=WorkflowStepKind.NOTIFICATION, supports="missing")
        def notify(self) -> None:
            raise NotImplementedError

    with pytest.raises(ValueError, match="supports unknown step missing"):
        infer_workflow_steps(BrokenWorkflow)


def test_workflow_step_graph_rejects_duplicate_step_keys() -> None:
    class BrokenWorkflow:
        @workflow_step(key="build", label="Build", kind=WorkflowStepKind.BUSINESS)
        def build(self) -> None:
            raise NotImplementedError

        @workflow_step(key="build", label="Build again", kind=WorkflowStepKind.BUSINESS)
        def build_again(self) -> None:
            raise NotImplementedError

    with pytest.raises(ValueError, match="Duplicate workflow step key"):
        infer_workflow_steps(BrokenWorkflow)


def test_workflow_step_requires_enum_kind() -> None:
    with pytest.raises(ValueError, match="Workflow step kind must be"):
        workflow_step(key="build", label="Build", kind="business")  # type: ignore[arg-type]


def test_workflow_work_unit_decorator_infers_units() -> None:
    class ExampleWorkflow:
        @workflow_work_unit(
            key="build.model",
            step_key="build",
            label="Build model",
            kind=WorkflowWorkUnitKind.MODEL_CALL,
        )
        @workflow_step(key="build", label="Build", kind=WorkflowStepKind.BUSINESS)
        def build(self) -> None:
            raise NotImplementedError

    work_units = infer_workflow_work_units(ExampleWorkflow)

    assert [(unit.key, unit.step_key, unit.kind, unit.graph_index) for unit in work_units] == [
        ("build.model", "build", WorkflowWorkUnitKind.MODEL_CALL, 0)
    ]


def test_workflow_definition_rejects_unit_attached_to_unknown_step() -> None:
    class ExampleWorkflow:
        @workflow_step(key="build", label="Build", kind=WorkflowStepKind.BUSINESS)
        def build(self) -> None:
            raise NotImplementedError

        @workflow_work_unit(
            key="missing.model",
            step_key="missing",
            label="Missing model",
            kind=WorkflowWorkUnitKind.MODEL_CALL,
        )
        def missing(self) -> None:
            raise NotImplementedError

    registry = WorkflowDefinitionRegistry()
    with pytest.raises(ValueError, match="belongs to unknown step missing"):
        registry.register(
            WorkflowDefinition(
                workflow_type_key="example",
                system_key="example",
                handler_key="example_handler",
                label="Example",
                description="Example workflow",
                orchestration_backend="temporal",
                steps=infer_workflow_steps(ExampleWorkflow),
                work_units=infer_workflow_work_units(ExampleWorkflow),
            )
        )


def test_workflow_definition_rejects_side_effect_unit_without_idempotency() -> None:
    class ExampleWorkflow:
        @workflow_work_unit(
            key="notify.emit",
            step_key="notify",
            label="Notify",
            kind=WorkflowWorkUnitKind.SIDE_EFFECT,
            idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=False),
        )
        @workflow_step(key="notify", label="Notify", kind=WorkflowStepKind.NOTIFICATION)
        def notify(self) -> None:
            raise NotImplementedError

    registry = WorkflowDefinitionRegistry()
    with pytest.raises(ValueError, match="requires idempotency policy"):
        registry.register(
            WorkflowDefinition(
                workflow_type_key="example",
                system_key="example",
                handler_key="example_handler",
                label="Example",
                description="Example workflow",
                orchestration_backend="temporal",
                steps=infer_workflow_steps(ExampleWorkflow),
                work_units=infer_workflow_work_units(ExampleWorkflow),
            )
        )


def test_workflow_definition_registry_fails_unknown_operation_type() -> None:
    workflow_type = get_workflow_type(workflow_type_key="parent_planning")

    with pytest.raises(LookupError, match="has no step"):
        workflow_type.step("legacy_catalog_only_step")


def test_workflow_definition_registry_rejects_duplicate_workflow_keys() -> None:
    class ExampleWorkflow:
        @workflow_step(key="build", label="Build", kind=WorkflowStepKind.BUSINESS)
        def build(self) -> None:
            raise NotImplementedError

    registry = WorkflowDefinitionRegistry()
    definition = WorkflowDefinition(
        workflow_type_key="example",
        system_key="example",
        handler_key="example_handler",
        label="Example",
        description="Example workflow",
        orchestration_backend="temporal",
        steps=infer_workflow_steps(ExampleWorkflow),
    )
    registry.register(definition)

    with pytest.raises(ValueError, match="Duplicate workflow type key"):
        registry.register(
            WorkflowDefinition(
                workflow_type_key="example",
                system_key="other",
                handler_key="other_handler",
                label="Other",
                description="Other workflow",
                orchestration_backend="temporal",
                steps=infer_workflow_steps(ExampleWorkflow),
            )
        )


def test_no_runtime_code_references_db_authored_workflow_catalog() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    forbidden = (
        "list_workflow_type_operations",
        "update_workflow_type_configuration",
        "workflow_type_operations",
        "retry_policy_config_json",
        "lifecycle_json",
    )
    offenders: list[str] = []
    for path in (repo_root / "orchestrator").rglob("*.py"):
        if "/storage/migrations/versions/" in str(path):
            continue
        text = path.read_text()
        for token in forbidden:
            if token in text:
                offenders.append(f"{path.relative_to(repo_root)}:{token}")
    assert offenders == []


def test_temporal_run_activities_use_code_defined_step_runner() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / "orchestrator/temporal/activities/run_execution.py").read_text()

    assert "start_workflow_step_attempt(" in source
    assert "start_workflow_operation_attempt" not in source
