from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

POLICY_OVERRIDE_FIELDS = {
    "allow_jira_transitions",
    "allow_pr_creation",
    "allow_label_mutations",
    "max_dev_test_review_loops",
    "max_concurrent_runs",
    "allowed_commands",
    "require_agents_md",
}

_BOOLEAN_CAP_FIELDS = {
    "allow_jira_transitions",
    "allow_pr_creation",
    "allow_label_mutations",
}

_NUMERIC_CAP_FIELDS = {
    "max_dev_test_review_loops",
    "max_concurrent_runs",
}


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(1, parsed)


def normalize_project_policy_overrides(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    if raw is None:
        return {}
    normalized: dict[str, Any] = {}
    for key in POLICY_OVERRIDE_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if key in _BOOLEAN_CAP_FIELDS or key == "require_agents_md":
            if isinstance(value, bool):
                normalized[key] = value
            continue
        if key in _NUMERIC_CAP_FIELDS:
            coerced = _coerce_positive_int(value)
            if coerced is not None:
                normalized[key] = coerced
            continue
        if key == "allowed_commands" and isinstance(value, list):
            commands = [str(item).strip() for item in value if str(item).strip()]
            normalized[key] = commands
    return normalized


def resolve_effective_policy(
    *,
    tenant_policy: Mapping[str, Any],
    project_overrides: Mapping[str, Any] | None,
) -> dict[str, Any]:
    effective = deepcopy(dict(tenant_policy))
    # Runtime limits are no longer policy-managed.
    effective.pop("max_runtime_minutes", None)
    overrides = normalize_project_policy_overrides(project_overrides)

    for field in _NUMERIC_CAP_FIELDS:
        tenant_value = _coerce_positive_int(effective.get(field))
        override_value = _coerce_positive_int(overrides.get(field))
        if tenant_value is None and override_value is None:
            continue
        if tenant_value is None:
            effective[field] = override_value
            continue
        if override_value is None:
            effective[field] = tenant_value
            continue
        effective[field] = min(tenant_value, override_value)

    for field in _BOOLEAN_CAP_FIELDS:
        tenant_value = bool(effective.get(field))
        override_value = overrides.get(field)
        if isinstance(override_value, bool):
            effective[field] = tenant_value and override_value
        else:
            effective[field] = tenant_value

    if "allowed_commands" in overrides:
        tenant_commands = [str(item).strip() for item in effective.get("allowed_commands") or [] if str(item).strip()]
        override_commands = [str(item).strip() for item in overrides.get("allowed_commands") or [] if str(item).strip()]
        if tenant_commands:
            tenant_set = set(tenant_commands)
            effective["allowed_commands"] = [command for command in override_commands if command in tenant_set]
        else:
            effective["allowed_commands"] = []

    if "require_agents_md" in overrides and isinstance(overrides["require_agents_md"], bool):
        effective["require_agents_md"] = bool(effective.get("require_agents_md")) or overrides["require_agents_md"]

    return effective
