from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.core.stages.spi_policy import resolve_stage_spi_enabled
from orchestrator.storage.models import Project, Tenant


def test_resolve_stage_spi_platform_fallback() -> None:
    session = MagicMock()
    session.get.return_value = None
    settings = SimpleNamespace(stage_spi_enabled=True)
    assert (
        resolve_stage_spi_enabled(
            session=session, settings=settings, tenant_id="t1", project_id="p1"
        )
        is True
    )
    settings2 = SimpleNamespace(stage_spi_enabled=False)
    assert (
        resolve_stage_spi_enabled(
            session=session, settings=settings2, tenant_id="t1", project_id="p1"
        )
        is False
    )


def test_resolve_stage_spi_project_override_wins() -> None:
    project = SimpleNamespace(
        tenant_id="t1", policy_overrides={"stage_spi_enabled": True}
    )
    tenant = SimpleNamespace(policy_config={"stage_spi_enabled": False})

    def _get(model, key):  # type: ignore[no-untyped-def]
        if model is Project and key == "p1":
            return project
        if model is Tenant and key == "t1":
            return tenant
        return None

    session = MagicMock()
    session.get.side_effect = _get
    settings = SimpleNamespace(stage_spi_enabled=False)
    assert (
        resolve_stage_spi_enabled(
            session=session, settings=settings, tenant_id="t1", project_id="p1"
        )
        is True
    )


def test_resolve_stage_spi_tenant_override_when_no_project_key() -> None:
    tenant = SimpleNamespace(policy_config={"stage_spi_enabled": True})

    def _get(model, key):  # type: ignore[no-untyped-def]
        if model is Tenant and key == "t1":
            return tenant
        return None

    session = MagicMock()
    session.get.side_effect = _get
    settings = SimpleNamespace(stage_spi_enabled=False)
    assert (
        resolve_stage_spi_enabled(
            session=session, settings=settings, tenant_id="t1", project_id=None
        )
        is True
    )


def test_resolve_stage_spi_project_wrong_tenant_ignored() -> None:
    project = SimpleNamespace(
        tenant_id="other", policy_overrides={"stage_spi_enabled": True}
    )

    def _get(model, key):  # type: ignore[no-untyped-def]
        if model is Project and key == "p1":
            return project
        return None

    session = MagicMock()
    session.get.side_effect = _get
    settings = SimpleNamespace(stage_spi_enabled=False)
    assert (
        resolve_stage_spi_enabled(
            session=session, settings=settings, tenant_id="t1", project_id="p1"
        )
        is False
    )


def test_resolve_stage_spi_session_none_uses_platform() -> None:
    settings = SimpleNamespace(stage_spi_enabled=True)
    assert (
        resolve_stage_spi_enabled(
            session=None, settings=settings, tenant_id="t1", project_id="p1"
        )
        is True
    )
