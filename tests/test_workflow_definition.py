from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.core.workflow_definition import (
    WorkflowDefinition,
    WorkflowDefinitionRegistry,
    WorkflowStepKind,
    infer_workflow_steps,
    workflow_step,
)
from orchestrator.core.workflow_type_catalog import get_workflow_type


def test_workflow_step_decorator_infers_ordered_graph() -> None:
    class ExampleWorkflow:
        @workflow_step(key="notify", label="Notify", kind=WorkflowStepKind.NOTIFICATION, after="build", required=False)
        def notify(self) -> None:
            raise NotImplementedError

        @workflow_step(key="build", label="Build", kind=WorkflowStepKind.BUSINESS)
        def build(self) -> None:
            raise NotImplementedError

    steps = infer_workflow_steps(ExampleWorkflow)

    assert [(step.key, step.kind, step.after, step.graph_index) for step in steps] == [
        ("build", WorkflowStepKind.BUSINESS, (), 0),
        ("notify", WorkflowStepKind.NOTIFICATION, ("build",), 1),
    ]


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
