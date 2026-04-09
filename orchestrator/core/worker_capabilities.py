from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from orchestrator.core.worker_capability_normalization import DEFAULT_WORKER_CAPABILITY
from orchestrator.core.worker_capability_normalization import WorkerCapability
from orchestrator.core.worker_capability_normalization import WorkerCapabilitiesInput
from orchestrator.core.worker_capability_normalization import parse_worker_capability
from orchestrator.core.worker_capability_normalization import parse_worker_capabilities_or_raise
from orchestrator.core.worker_capability_normalization import parse_worker_capabilities_with_diagnostics
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot

WORKER_CAPABILITY_LABEL_PREFIX = "worker:"


@dataclass(frozen=True)
class WorkerLabelParseResult:
    selected_capability: WorkerCapability | None
    invalid_labels: tuple[str, ...]
    conflicting_capabilities: tuple[WorkerCapability, ...]


@dataclass(frozen=True)
class WorkerCapabilityContext:
    current: WorkerCapability
    available: tuple[WorkerCapability, ...]

    @property
    def current_value(self) -> str:
        return self.current.value

    @property
    def available_values(self) -> tuple[str, ...]:
        return tuple(capability.value for capability in self.available)


def resolve_worker_capability_context(*, raw_value: WorkerCapabilitiesInput, source: str) -> WorkerCapabilityContext:
    parsed = sorted(
        parse_worker_capabilities_or_raise(raw_value, source=source),
        key=lambda capability: capability.value,
    )
    if not parsed:
        parsed = [DEFAULT_WORKER_CAPABILITY]
    return WorkerCapabilityContext(
        current=parsed[0],
        available=tuple(parsed),
    )


def worker_label_for_capability(capability: object) -> str:
    parsed = parse_worker_capability(capability)
    if parsed is None:
        raise ValueError(f"Unsupported worker capability '{capability}'")
    return f"{WORKER_CAPABILITY_LABEL_PREFIX}{parsed.value}"


def parse_worker_capabilities(raw_value: WorkerCapabilitiesInput) -> set[WorkerCapability]:
    if isinstance(raw_value, Iterable) and not isinstance(raw_value, str):
        values = [item for item in raw_value if isinstance(item, str)]
        return set(parse_worker_capabilities_with_diagnostics(values).capabilities)
    return set(parse_worker_capabilities_with_diagnostics(raw_value).capabilities)


def parse_worker_capabilities_strict(raw_value: WorkerCapabilitiesInput, *, source: str) -> set[WorkerCapability]:
    return parse_worker_capabilities_or_raise(raw_value, source=source)


def parse_worker_capabilities_diagnostics(
    raw_value: WorkerCapabilitiesInput,
) -> tuple[set[WorkerCapability], tuple[str, ...]]:
    parsed = parse_worker_capabilities_with_diagnostics(raw_value)
    return set(parsed.capabilities), parsed.invalid_tokens


def parse_worker_capability_labels(
    issue_labels: list[str] | None,
) -> WorkerLabelParseResult:
    invalid_labels: list[str] = []
    valid_capabilities: set[WorkerCapability] = set()
    for raw_label in issue_labels or []:
        label = str(raw_label).strip()
        if not label.startswith(WORKER_CAPABILITY_LABEL_PREFIX):
            continue
        suffix = label.split(":", 1)[1].strip()
        parsed = parse_worker_capability(suffix)
        if parsed is None:
            invalid_labels.append(label)
            continue
        valid_capabilities.add(parsed)
    conflicting = tuple(sorted(valid_capabilities, key=lambda capability: capability.value))
    selected: WorkerCapability | None = conflicting[0] if len(conflicting) == 1 else None
    return WorkerLabelParseResult(
        selected_capability=selected,
        invalid_labels=tuple(sorted(set(invalid_labels))),
        conflicting_capabilities=conflicting if len(conflicting) > 1 else (),
    )


def infer_required_worker_capability(
    *,
    issue_summary: str | None,
    issue_description: str | None,
    issue_labels: list[str] | None,
    tenant_id: str | None = None,
    project_id: str | None = None,
    issue_key: str | None = None,
    run_id: str | None = None,
) -> str:
    _ = issue_summary
    _ = issue_description
    _ = tenant_id
    _ = project_id
    _ = issue_key
    _ = run_id
    normalized_labels = [str(label).strip() for label in (issue_labels or []) if str(label).strip()]
    label_parse = parse_worker_capability_labels(normalized_labels)
    if label_parse.selected_capability is not None:
        return label_parse.selected_capability.value
    return ""


def required_worker_capability_for_run(run) -> WorkerCapability | None:  # noqa: ANN001
    snapshot = ExecutionSnapshot.load(getattr(run, "plan", None))
    if snapshot is None:
        return None
    plan_required = parse_worker_capability(snapshot.workflow.requeue_target)
    if plan_required is not None:
        return plan_required
    return None
