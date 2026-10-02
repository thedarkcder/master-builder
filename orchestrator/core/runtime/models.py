from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_KNOWN_MODEL_METADATA: dict[str, tuple[str, str | None]] = {
    "gpt-5.4": ("GPT-5.4", "General Codex-capable model"),
    "gpt-5.5": ("GPT-5.5", "Latest general Codex-capable model"),
    "gpt-5.4-mini": ("GPT-5.4 Mini", "Lower-latency general Codex-capable model"),
    "gpt-5.3-codex": ("GPT-5.3 Codex", "Primary coding model"),
}
_REASONING_EFFORTS = ("low", "medium", "high")


@dataclass(frozen=True)
class CodexModelOption:
    model_id: str
    label: str
    description: str | None = None


@dataclass(frozen=True)
class CodexReasoningOption:
    effort_id: str
    label: str
    description: str | None = None


def normalize_codex_model(value: Any) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def normalize_codex_reasoning_effort(value: Any) -> str | None:
    normalized = str(value or "").strip().lower()
    if normalized in _REASONING_EFFORTS:
        return normalized
    return None


def parse_supported_codex_models(
    *, default_model: str | None, configured_models: str | None
) -> list[CodexModelOption]:
    normalized_default = normalize_codex_model(default_model)
    seen: set[str] = set()
    ordered_model_ids: list[str] = []
    candidates: list[str] = []
    if normalized_default is not None:
        candidates.append(normalized_default)
    candidates.extend(str(configured_models or "").split(","))
    for candidate in candidates:
        normalized = normalize_codex_model(candidate)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        ordered_model_ids.append(normalized)

    options: list[CodexModelOption] = []
    for model_id in ordered_model_ids:
        label, description = _KNOWN_MODEL_METADATA.get(model_id, (model_id, None))
        options.append(
            CodexModelOption(
                model_id=model_id,
                label=label,
                description=description,
            )
        )
    return options


def parse_supported_reasoning_efforts(
    *, default_effort: str
) -> list[CodexReasoningOption]:
    normalized_default = normalize_codex_reasoning_effort(default_effort) or "medium"
    ordered_efforts = [
        normalized_default,
        *[effort for effort in _REASONING_EFFORTS if effort != normalized_default],
    ]
    descriptions = {
        "low": "Fastest responses with less deliberation",
        "medium": "Balanced depth and speed",
        "high": "Most deliberate reasoning mode",
    }
    return [
        CodexReasoningOption(
            effort_id=effort,
            label=effort.title(),
            description=descriptions.get(effort),
        )
        for effort in ordered_efforts
    ]


def resolve_effective_codex_model(
    *,
    tenant_policy: dict[str, Any] | None,
    project_overrides: dict[str, Any] | None,
    default_model: str,
) -> str:
    project_model = normalize_codex_model((project_overrides or {}).get("codex_model"))
    if project_model is not None:
        return project_model
    tenant_model = normalize_codex_model((tenant_policy or {}).get("codex_model"))
    if tenant_model is not None:
        return tenant_model
    resolved_default = normalize_codex_model(default_model)
    if resolved_default is None:
        raise ValueError(
            "default_model must be provided when resolving effective codex model"
        )
    return resolved_default


def resolve_effective_codex_reasoning_effort(
    *,
    tenant_policy: dict[str, Any] | None,
    project_overrides: dict[str, Any] | None,
    default_effort: str,
) -> str:
    project_effort = normalize_codex_reasoning_effort(
        (project_overrides or {}).get("codex_reasoning_effort")
    )
    if project_effort is not None:
        return project_effort
    tenant_effort = normalize_codex_reasoning_effort(
        (tenant_policy or {}).get("codex_reasoning_effort")
    )
    if tenant_effort is not None:
        return tenant_effort
    return normalize_codex_reasoning_effort(default_effort) or "medium"
