from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class WorkerCapability(str, Enum):
    LINUX = "linux"
    MACOS = "macos"


DEFAULT_WORKER_CAPABILITY = WorkerCapability.LINUX
KNOWN_WORKER_CAPABILITIES = frozenset(item.value for item in WorkerCapability)
WorkerCapabilitiesInput = str | Iterable[str] | None


@dataclass(frozen=True)
class WorkerCapabilityParseResult:
    capabilities: set[WorkerCapability]
    invalid_tokens: tuple[str, ...]


def parse_worker_capability(value: object) -> WorkerCapability | None:
    if isinstance(value, WorkerCapability):
        return value
    if isinstance(value, str):
        try:
            return WorkerCapability(value)
        except ValueError:
            return None
    return None


def parse_worker_capabilities_with_diagnostics(raw_value: WorkerCapabilitiesInput) -> WorkerCapabilityParseResult:
    tokens: list[str] = []
    if isinstance(raw_value, str):
        tokens = [item.strip() for item in raw_value.split(",")]
    elif isinstance(raw_value, Iterable):
        tokens = [item.strip() for item in raw_value if isinstance(item, str)]

    parsed_capabilities: set[WorkerCapability] = set()
    invalid_tokens: list[str] = []
    for token in tokens:
        if not token:
            continue
        parsed = parse_worker_capability(token)
        if parsed is None:
            invalid_tokens.append(token)
            continue
        parsed_capabilities.add(parsed)

    if not parsed_capabilities:
        parsed_capabilities.add(DEFAULT_WORKER_CAPABILITY)

    return WorkerCapabilityParseResult(
        capabilities=parsed_capabilities,
        invalid_tokens=tuple(sorted(set(invalid_tokens))),
    )


def parse_worker_capabilities_or_raise(raw_value: WorkerCapabilitiesInput, *, source: str) -> set[WorkerCapability]:
    parsed = parse_worker_capabilities_with_diagnostics(raw_value)
    if parsed.invalid_tokens:
        invalid = ", ".join(parsed.invalid_tokens)
        allowed = ", ".join(sorted(KNOWN_WORKER_CAPABILITIES))
        raise ValueError(
            f"Invalid worker capability token(s) in {source}: {invalid}. Allowed values: {allowed}."
        )
    return set(parsed.capabilities)
