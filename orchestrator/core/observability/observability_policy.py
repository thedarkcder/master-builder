from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping

DEFAULT_AUDIT_RETENTION_DAYS = 365
MAX_AUDIT_RETENTION_DAYS = 3650


@dataclass(frozen=True)
class TenantObservabilityPolicy:
    audit_retention_days: int = DEFAULT_AUDIT_RETENTION_DAYS
    audit_export_enabled: bool = True
    legal_hold_enabled: bool = False
    legal_hold_reason: str | None = None


def normalize_tenant_observability_policy(
    policy_config: Mapping[str, object] | None,
) -> TenantObservabilityPolicy:
    observability: Mapping[str, object] | None = None
    if isinstance(policy_config, Mapping):
        raw_observability = policy_config.get("observability")
        if isinstance(raw_observability, Mapping):
            observability = raw_observability

    raw_retention_days = (
        observability.get("audit_retention_days") if observability else None
    )
    try:
        audit_retention_days = int(raw_retention_days or DEFAULT_AUDIT_RETENTION_DAYS)
    except (TypeError, ValueError):
        audit_retention_days = DEFAULT_AUDIT_RETENTION_DAYS
    audit_retention_days = max(1, min(audit_retention_days, MAX_AUDIT_RETENTION_DAYS))

    audit_export_enabled = True
    if observability is not None and "audit_export_enabled" in observability:
        audit_export_enabled = bool(observability.get("audit_export_enabled"))

    legal_hold_enabled = False
    if observability is not None and "legal_hold_enabled" in observability:
        legal_hold_enabled = bool(observability.get("legal_hold_enabled"))

    raw_reason = observability.get("legal_hold_reason") if observability else None
    legal_hold_reason = str(raw_reason or "").strip() or None
    if not legal_hold_enabled:
        legal_hold_reason = None

    return TenantObservabilityPolicy(
        audit_retention_days=audit_retention_days,
        audit_export_enabled=audit_export_enabled,
        legal_hold_enabled=legal_hold_enabled,
        legal_hold_reason=legal_hold_reason,
    )
