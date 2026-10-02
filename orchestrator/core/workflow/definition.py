from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Iterable


class WorkflowStepKind(StrEnum):
    BUSINESS = "business"
    HUMAN_GATE = "human_gate"
    SIDE_EFFECT = "side_effect"
    INTEGRATION = "integration"
    NOTIFICATION = "notification"


class WorkflowWorkUnitKind(StrEnum):
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    EXTERNAL_API = "external_api"
    SIDE_EFFECT = "side_effect"
    PURE_COMPUTE = "pure_compute"
    ASSEMBLY = "assembly"


@dataclass(frozen=True)
class WorkflowWorkUnitRetryPolicy:
    max_attempts: int = 1
    initial_interval_seconds: int = 0
    max_interval_seconds: int = 0
    backoff_coefficient: float = 1.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(
                "Workflow work unit retry policy max_attempts must be >= 1"
            )
        if self.initial_interval_seconds < 0:
            raise ValueError(
                "Workflow work unit retry policy initial interval must be >= 0"
            )
        if self.max_interval_seconds < 0:
            raise ValueError(
                "Workflow work unit retry policy max interval must be >= 0"
            )
        if self.backoff_coefficient < 1.0:
            raise ValueError(
                "Workflow work unit retry policy backoff coefficient must be >= 1.0"
            )


@dataclass(frozen=True)
class WorkflowWorkUnitIdempotencyPolicy:
    required: bool = True


@dataclass(frozen=True)
class WorkflowWorkUnitDefinition:
    key: str
    step_key: str
    label: str
    kind: WorkflowWorkUnitKind
    retry_policy: WorkflowWorkUnitRetryPolicy = field(
        default_factory=WorkflowWorkUnitRetryPolicy
    )
    idempotency_policy: WorkflowWorkUnitIdempotencyPolicy = field(
        default_factory=WorkflowWorkUnitIdempotencyPolicy
    )
    required: bool = True
    description: str | None = None
    graph_index: int = 0


@dataclass(frozen=True)
class WorkflowStepDefinition:
    key: str
    label: str
    kind: WorkflowStepKind
    after: tuple[str, ...] = ()
    supports: tuple[str, ...] = ()
    required: bool = True
    retryable: bool = False
    description: str | None = None
    graph_index: int = 0


@dataclass(frozen=True)
class WorkflowRetryPolicyDefinition:
    manual_retry_enabled: bool = True
    max_attempts: int = 1
    initial_interval_seconds: int = 0
    max_interval_seconds: int = 0
    backoff_coefficient: float = 1.0

    def to_payload(self) -> dict[str, object]:
        return {
            "manual_retry_enabled": self.manual_retry_enabled,
            "max_attempts": self.max_attempts,
            "initial_interval_seconds": self.initial_interval_seconds,
            "max_interval_seconds": self.max_interval_seconds,
            "backoff_coefficient": self.backoff_coefficient,
        }


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_type_key: str
    system_key: str
    handler_key: str
    label: str
    description: str
    orchestration_backend: str
    retry_policy: WorkflowRetryPolicyDefinition = field(
        default_factory=WorkflowRetryPolicyDefinition
    )
    capabilities: dict[str, object] = field(default_factory=dict)
    steps: tuple[WorkflowStepDefinition, ...] = ()
    work_units: tuple[WorkflowWorkUnitDefinition, ...] = ()

    def step(self, key: str) -> WorkflowStepDefinition:
        normalized = _normalize_key(key, field_name="step key")
        for definition in self.steps:
            if definition.key == normalized:
                return definition
        raise LookupError(
            f"Workflow {self.workflow_type_key} has no step: {normalized}"
        )

    def has_step(self, key: str) -> bool:
        normalized = _normalize_key(key, field_name="step key")
        return any(definition.key == normalized for definition in self.steps)

    def work_unit(self, key: str) -> WorkflowWorkUnitDefinition:
        normalized = _normalize_key(key, field_name="work unit key")
        for definition in self.work_units:
            if definition.key == normalized:
                return definition
        raise LookupError(
            f"Workflow {self.workflow_type_key} has no work unit: {normalized}"
        )


def _normalize_key(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"Workflow {field_name} is required")
    return normalized


def workflow_step(
    *,
    key: str,
    label: str,
    kind: WorkflowStepKind,
    after: str | Iterable[str] = (),
    supports: str | Iterable[str] = (),
    required: bool = True,
    retryable: bool = False,
    description: str | None = None,
) -> Callable:
    if not isinstance(kind, WorkflowStepKind):
        raise ValueError("Workflow step kind must be a WorkflowStepKind enum value")
    normalized_after = _normalize_key_tuple(after, field_name="step dependency")
    normalized_supports = _normalize_key_tuple(
        supports, field_name="step support owner"
    )
    definition = WorkflowStepDefinition(
        key=_normalize_key(key, field_name="step key"),
        label=_normalize_key(label, field_name="step label"),
        kind=kind,
        after=normalized_after,
        supports=normalized_supports,
        required=required,
        retryable=retryable,
        description=str(description).strip()
        if description is not None and str(description).strip()
        else None,
    )

    def _decorate(fn: Callable) -> Callable:
        existing = getattr(fn, "__workflow_step_definitions__", ())
        setattr(fn, "__workflow_step_definitions__", tuple(existing) + (definition,))
        return fn

    return _decorate


def workflow_work_unit(
    *,
    key: str,
    step_key: str,
    label: str,
    kind: WorkflowWorkUnitKind,
    retry_policy: WorkflowWorkUnitRetryPolicy | None = None,
    idempotency_policy: WorkflowWorkUnitIdempotencyPolicy | None = None,
    required: bool = True,
    description: str | None = None,
) -> Callable:
    if not isinstance(kind, WorkflowWorkUnitKind):
        raise ValueError(
            "Workflow work unit kind must be a WorkflowWorkUnitKind enum value"
        )
    definition = WorkflowWorkUnitDefinition(
        key=_normalize_key(key, field_name="work unit key"),
        step_key=_normalize_key(step_key, field_name="work unit step key"),
        label=_normalize_key(label, field_name="work unit label"),
        kind=kind,
        retry_policy=retry_policy or WorkflowWorkUnitRetryPolicy(),
        idempotency_policy=idempotency_policy
        or WorkflowWorkUnitIdempotencyPolicy(
            required=kind
            in {WorkflowWorkUnitKind.EXTERNAL_API, WorkflowWorkUnitKind.SIDE_EFFECT}
        ),
        required=required,
        description=str(description).strip()
        if description is not None and str(description).strip()
        else None,
    )

    def _decorate(fn: Callable) -> Callable:
        existing = getattr(fn, "__workflow_work_unit_definitions__", ())
        setattr(
            fn, "__workflow_work_unit_definitions__", tuple(existing) + (definition,)
        )
        return fn

    return _decorate


def _normalize_key_tuple(
    value: str | Iterable[str], *, field_name: str
) -> tuple[str, ...]:
    if isinstance(value, str):
        return (_normalize_key(value, field_name=field_name),) if value.strip() else ()
    return tuple(_normalize_key(item, field_name=field_name) for item in value)


def infer_workflow_work_units(
    workflow_cls: type,
) -> tuple[WorkflowWorkUnitDefinition, ...]:
    discovered: list[WorkflowWorkUnitDefinition] = []
    for name in dir(workflow_cls):
        value = getattr(workflow_cls, name)
        definitions = getattr(value, "__workflow_work_unit_definitions__", ())
        for definition in definitions:
            if isinstance(definition, WorkflowWorkUnitDefinition):
                discovered.append(definition)
    by_key: dict[str, WorkflowWorkUnitDefinition] = {}
    for definition in sorted(discovered, key=lambda item: item.key):
        if definition.key in by_key:
            raise ValueError(f"Duplicate workflow work unit key: {definition.key}")
        by_key[definition.key] = definition
    return tuple(
        WorkflowWorkUnitDefinition(
            key=definition.key,
            step_key=definition.step_key,
            label=definition.label,
            kind=definition.kind,
            retry_policy=definition.retry_policy,
            idempotency_policy=definition.idempotency_policy,
            required=definition.required,
            description=definition.description,
            graph_index=index,
        )
        for index, definition in enumerate(by_key.values())
    )


def infer_workflow_steps(workflow_cls: type) -> tuple[WorkflowStepDefinition, ...]:
    discovered: list[WorkflowStepDefinition] = []
    for name in dir(workflow_cls):
        value = getattr(workflow_cls, name)
        definitions = getattr(value, "__workflow_step_definitions__", ())
        for definition in definitions:
            if isinstance(definition, WorkflowStepDefinition):
                discovered.append(definition)
    discovered.sort(key=lambda definition: definition.key)
    return _topological_steps(discovered)


def _topological_steps(
    discovered: list[WorkflowStepDefinition],
) -> tuple[WorkflowStepDefinition, ...]:
    by_key: dict[str, WorkflowStepDefinition] = {}
    for definition in discovered:
        if definition.key in by_key:
            raise ValueError(f"Duplicate workflow step key: {definition.key}")
        by_key[definition.key] = definition
    for definition in discovered:
        for dependency in definition.after:
            if dependency not in by_key:
                raise ValueError(
                    f"Workflow step {definition.key} depends on unknown step {dependency}"
                )
        for owner in definition.supports:
            if owner not in by_key:
                raise ValueError(
                    f"Workflow step {definition.key} supports unknown step {owner}"
                )
            if owner == definition.key:
                raise ValueError(
                    f"Workflow step {definition.key} cannot support itself"
                )

    ordered: list[WorkflowStepDefinition] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visited:
            return
        if key in visiting:
            raise ValueError(f"Workflow step graph contains a cycle at {key}")
        visiting.add(key)
        definition = by_key[key]
        for dependency in definition.after:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)
        ordered.append(definition)

    for key in sorted(by_key):
        visit(key)

    return tuple(
        WorkflowStepDefinition(
            key=definition.key,
            label=definition.label,
            kind=definition.kind,
            after=definition.after,
            supports=definition.supports,
            required=definition.required,
            retryable=definition.retryable,
            description=definition.description,
            graph_index=index,
        )
        for index, definition in enumerate(ordered)
    )


class WorkflowDefinitionRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, WorkflowDefinition] = {}
        self._by_handler: dict[str, WorkflowDefinition] = {}
        self._by_system_key: dict[str, WorkflowDefinition] = {}

    def register(self, definition: WorkflowDefinition) -> None:
        self._validate_definition(definition)
        if definition.workflow_type_key in self._by_key:
            raise ValueError(
                f"Duplicate workflow type key: {definition.workflow_type_key}"
            )
        if definition.handler_key in self._by_handler:
            raise ValueError(
                f"Duplicate workflow handler key: {definition.handler_key}"
            )
        if definition.system_key in self._by_system_key:
            raise ValueError(f"Duplicate workflow system key: {definition.system_key}")
        self._by_key[definition.workflow_type_key] = definition
        self._by_handler[definition.handler_key] = definition
        self._by_system_key[definition.system_key] = definition

    def get(self, workflow_type_key: str) -> WorkflowDefinition:
        normalized = _normalize_key(workflow_type_key, field_name="workflow type key")
        try:
            return self._by_key[normalized]
        except KeyError as exc:
            raise LookupError(f"Workflow type not registered: {normalized}") from exc

    def get_by_handler(self, handler_key: str) -> WorkflowDefinition:
        normalized = _normalize_key(handler_key, field_name="workflow handler key")
        try:
            return self._by_handler[normalized]
        except KeyError as exc:
            raise LookupError(
                f"Workflow type not registered for handler key: {normalized}"
            ) from exc

    def get_by_system_key(self, system_key: str) -> WorkflowDefinition:
        normalized = _normalize_key(system_key, field_name="workflow system key")
        try:
            return self._by_system_key[normalized]
        except KeyError as exc:
            raise LookupError(
                f"Workflow type not registered for system key: {normalized}"
            ) from exc

    def list(self) -> tuple[WorkflowDefinition, ...]:
        return tuple(
            sorted(
                self._by_key.values(),
                key=lambda definition: (definition.label, definition.workflow_type_key),
            )
        )

    def validate_operation_type(
        self, *, workflow_type_key: str, operation_type: str
    ) -> WorkflowStepDefinition:
        definition = self.get(workflow_type_key)
        return definition.step(operation_type)

    @staticmethod
    def _validate_definition(definition: WorkflowDefinition) -> None:
        _normalize_key(definition.workflow_type_key, field_name="workflow type key")
        _normalize_key(definition.system_key, field_name="workflow system key")
        _normalize_key(definition.handler_key, field_name="workflow handler key")
        _normalize_key(definition.label, field_name="workflow label")
        _normalize_key(
            definition.orchestration_backend,
            field_name="workflow orchestration backend",
        )
        steps = _topological_steps(list(definition.steps))
        step_keys = {step.key for step in steps}
        work_unit_keys: set[str] = set()
        for work_unit in definition.work_units:
            if work_unit.key in work_unit_keys:
                raise ValueError(f"Duplicate workflow work unit key: {work_unit.key}")
            work_unit_keys.add(work_unit.key)
            if work_unit.step_key not in step_keys:
                raise ValueError(
                    f"Workflow work unit {work_unit.key} belongs to unknown step {work_unit.step_key}"
                )
            if (
                work_unit.kind
                in {WorkflowWorkUnitKind.EXTERNAL_API, WorkflowWorkUnitKind.SIDE_EFFECT}
                and not work_unit.idempotency_policy.required
            ):
                raise ValueError(
                    f"Workflow work unit {work_unit.key} requires idempotency policy"
                )
