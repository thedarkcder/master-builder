"""Resolve whether the extensible stage SPI is active for a tenant/project.

Precedence (most specific wins):
1. ``project.policy_overrides["stage_spi_enabled"]`` when the key is present
2. ``tenant.policy_config["stage_spi_enabled"]`` when the key is present
3. Platform default: ``settings.stage_spi_enabled`` (``ORCHESTRATOR_STAGE_SPI_ENABLED``)
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from orchestrator.storage.models import Project, Tenant


def resolve_stage_spi_enabled(
    *,
    session: Session | None,
    settings: Any,
    tenant_id: str,
    project_id: str | None,
) -> bool:
    tid = str(tenant_id or "").strip()
    pid = str(project_id or "").strip() or None
    if session is not None and pid and tid:
        project = session.get(Project, pid)
        if project is not None and str(getattr(project, "tenant_id", "") or "").strip() == tid:
            overrides = project.policy_overrides or {}
            if isinstance(overrides, dict) and "stage_spi_enabled" in overrides:
                return bool(overrides.get("stage_spi_enabled"))
    if session is not None and tid:
        tenant = session.get(Tenant, tid)
        if tenant is not None:
            policy = tenant.policy_config or {}
            if isinstance(policy, dict) and "stage_spi_enabled" in policy:
                return bool(policy.get("stage_spi_enabled"))
    return bool(getattr(settings, "stage_spi_enabled", False))
