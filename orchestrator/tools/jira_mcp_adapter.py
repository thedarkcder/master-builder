from __future__ import annotations

from dataclasses import dataclass

REQUIRED_CAPABILITIES = {
    "search_issues",
    "get_issue",
    "add_comment",
    "add_labels",
    "remove_labels",
}

OPTIONAL_CAPABILITIES = {"transition_issue"}


@dataclass(frozen=True)
class JiraCapabilityReport:
    available: set[str]
    missing_required: set[str]
    optional_available: set[str]

    @property
    def is_healthy(self) -> bool:
        return not self.missing_required


class JiraMcpAdapter:
    def __init__(self, declared_capabilities: list[str] | None = None):
        self._declared_capabilities = set(declared_capabilities or [])

    def discover_capabilities(self) -> JiraCapabilityReport:
        missing_required = REQUIRED_CAPABILITIES - self._declared_capabilities
        optional_available = OPTIONAL_CAPABILITIES.intersection(
            self._declared_capabilities
        )
        return JiraCapabilityReport(
            available=set(self._declared_capabilities),
            missing_required=missing_required,
            optional_available=optional_available,
        )
