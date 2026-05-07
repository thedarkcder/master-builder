from __future__ import annotations

from collections.abc import Iterable
from orchestrator.core.runtime.agent_execution_profiles import list_supported_runtime_kinds
from orchestrator.core.runtime.agent_runtime_resolver import resolve_execution_profile_for_selector
from orchestrator.core.config import Settings

WORKFLOW_RUNTIME_SELECTORS: tuple[str, ...] = (
    "repo_setup.prepare",
    "workflow.pm",
    "workflow.dev",
    "workflow.test",
    "workflow.review",
)

_SUPPORTED_RUNTIME_KINDS = frozenset(list_supported_runtime_kinds())


def normalize_runtime_kind(value: object | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized or normalized not in _SUPPORTED_RUNTIME_KINDS:
        return None
    return normalized


def normalize_runtime_kinds(raw_value: object | None) -> list[str]:
    if raw_value is None:
        return []
    values: list[object]
    if isinstance(raw_value, str):
        values = [raw_value]
    elif isinstance(raw_value, Iterable):
        values = list(raw_value)
    else:
        values = []
    normalized: set[str] = set()
    for candidate in values:
        runtime_kind = normalize_runtime_kind(candidate)
        if runtime_kind is not None:
            normalized.add(runtime_kind)
    return sorted(normalized)


def required_runtime_kinds_for_run(run: object) -> set[str]:
    return set(normalize_runtime_kinds(getattr(run, "required_runtime_kinds_json", None)))


def resolve_required_runtime_kinds_for_workflow(
    *,
    session,
    settings: Settings,
    tenant_id: str | None,
    project_id: str | None,
    selectors: Iterable[str] = WORKFLOW_RUNTIME_SELECTORS,
) -> list[str]:
    runtime_kinds: set[str] = set()
    for selector in selectors:
        profile = resolve_execution_profile_for_selector(
            session=session,
            settings=settings,
            tenant_id=tenant_id,
            project_id=project_id,
            selector=selector,
        )
        normalized_runtime_kind = normalize_runtime_kind(profile.runtime_kind)
        if normalized_runtime_kind is not None:
            runtime_kinds.add(normalized_runtime_kind)
    return sorted(runtime_kinds)
