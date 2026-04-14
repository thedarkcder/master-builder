from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap_deployment_host_agent.py"
    spec = importlib.util.spec_from_file_location("bootstrap_deployment_host_agent", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load bootstrap_deployment_host_agent module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class BootstrapDeploymentHostAgentTests(unittest.TestCase):
    def test_ensure_bootstrap_noops_when_access_token_is_still_valid(self) -> None:
        module = _load_module()
        with TemporaryDirectory() as tmp_dir:
            token_path = Path(tmp_dir) / "access-token"
            token_path.write_text("access-token-1", encoding="utf-8")
            updates: list[tuple[str, dict[str, object]]] = []

            class _FakeClient:
                def __init__(self, *, base_url: str, auth_header: str) -> None:
                    self.base_url = base_url
                    self.auth_header = auth_header

                def heartbeat_deployment_host(
                    self,
                    *,
                    access_token: str,
                    agent_version: str,
                    advertised_capabilities: tuple[str, ...],
                    state: str = "active",
                ) -> dict[str, object]:
                    assert access_token == "access-token-1"
                    assert agent_version == "bootstrap"
                    assert advertised_capabilities == ("restore_database", "postgres")
                    assert state == "active"
                    return {"host_id": "host-1"}

                def list_tenants(self) -> list[dict[str, object]]:
                    return [{"tenant_id": "tenant-a"}]

                def get_tenant_deployment_plane(self, *, tenant_id: str) -> dict[str, object]:
                    assert tenant_id == "tenant-a"
                    return {
                        "provider": "internal_coolify",
                        "api_base_url": "https://builder.example.com/api/v1",
                        "managed_host_id": None,
                        "state": "active",
                        "secret_refs": {"coolify_api_token": "platform/COOLIFY_API_TOKEN"},
                    }

                def update_tenant_deployment_plane(self, *, tenant_id: str, payload: dict[str, object]) -> dict[str, object]:
                    updates.append((tenant_id, payload))
                    return payload

            config = module.DeploymentHostBootstrapConfig(
                api_base_url="http://api:4000",
                auth_header="Basic abc",
                host_label="managed-test",
                infrastructure_provider="hetzner",
                region="eu-west",
                capabilities=("restore_database", "postgres"),
                bootstrap_token_path=Path(tmp_dir) / "bootstrap-token",
                access_token_path=token_path,
                tenant_ids=(),
                assign_configured_tenants=True,
            )

            with patch.object(module, "AdminApiClient", _FakeClient):
                summary = module.ensure_deployment_host_bootstrap(config)

            self.assertEqual(summary["action"], "noop")
            self.assertEqual(summary["reason"], "access_token_valid")
            self.assertEqual(summary["host_id"], "host-1")
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0][0], "tenant-a")
            self.assertEqual(updates[0][1]["managed_host_id"], "host-1")

    def test_ensure_bootstrap_creates_host_writes_token_and_assigns_configured_tenants(self) -> None:
        module = _load_module()
        with TemporaryDirectory() as tmp_dir:
            bootstrap_token_path = Path(tmp_dir) / "bootstrap-token"
            access_token_path = Path(tmp_dir) / "access-token"
            updates: list[tuple[str, dict[str, object]]] = []

            class _FakeClient:
                def __init__(self, *, base_url: str, auth_header: str) -> None:
                    self.base_url = base_url
                    self.auth_header = auth_header

                def list_deployment_hosts(self) -> list[dict[str, object]]:
                    return []

                def create_deployment_host(
                    self,
                    *,
                    label: str,
                    infrastructure_provider: str | None,
                    region: str | None,
                    capabilities: tuple[str, ...],
                ) -> dict[str, object]:
                    return {
                        "host": {
                            "host_id": "host-1",
                            "label": label,
                            "provider": "internal_coolify",
                            "infrastructure_provider": infrastructure_provider,
                            "region": region,
                            "capabilities": list(capabilities),
                            "state": "provisioning",
                        },
                        "bootstrap_token": "bootstrap-token-1",
                    }

                def register_deployment_host(
                    self,
                    *,
                    bootstrap_token: str,
                    agent_version: str,
                    advertised_capabilities: tuple[str, ...],
                ) -> dict[str, object]:
                    assert bootstrap_token == "bootstrap-token-1"
                    assert agent_version == "bootstrap"
                    assert advertised_capabilities == ("restore_database", "postgres")
                    return {
                        "host": {"host_id": "host-1"},
                        "access_token": "access-token-1",
                    }

                def list_tenants(self) -> list[dict[str, object]]:
                    return [{"tenant_id": "tenant-a"}, {"tenant_id": "tenant-b"}]

                def get_tenant_deployment_plane(self, *, tenant_id: str) -> dict[str, object]:
                    if tenant_id == "tenant-a":
                        return {
                            "provider": "internal_coolify",
                            "infrastructure_provider": "hetzner",
                            "region": "eu-west",
                            "api_base_url": "https://builder.example.com/api/v1",
                            "secret_refs": {"coolify_api_token": "platform/COOLIFY_API_TOKEN"},
                            "state": "active",
                            "managed_host_id": None,
                        }
                    return {
                        "provider": "internal_coolify",
                        "infrastructure_provider": None,
                        "region": None,
                        "api_base_url": None,
                        "secret_refs": {},
                        "state": "unconfigured",
                        "managed_host_id": None,
                    }

                def update_tenant_deployment_plane(self, *, tenant_id: str, payload: dict[str, object]) -> dict[str, object]:
                    updates.append((tenant_id, payload))
                    return payload

            config = module.DeploymentHostBootstrapConfig(
                api_base_url="http://api:4000",
                auth_header="Basic abc",
                host_label="managed-test",
                infrastructure_provider="hetzner",
                region="eu-west",
                capabilities=("restore_database", "postgres"),
                bootstrap_token_path=bootstrap_token_path,
                access_token_path=access_token_path,
                tenant_ids=(),
                assign_configured_tenants=True,
            )

            with patch.object(module, "AdminApiClient", _FakeClient):
                summary = module.ensure_deployment_host_bootstrap(config)

            self.assertEqual(summary["action"], "created")
            self.assertEqual(summary["host_id"], "host-1")
            self.assertEqual(summary["assigned_tenants"], ["tenant-a"])
            self.assertEqual(bootstrap_token_path.read_text(encoding="utf-8"), "bootstrap-token-1")
            self.assertEqual(access_token_path.read_text(encoding="utf-8"), "access-token-1")
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0][0], "tenant-a")
            self.assertEqual(updates[0][1]["managed_host_id"], "host-1")

    def test_ensure_bootstrap_recreates_host_when_stored_credentials_are_stale(self) -> None:
        module = _load_module()
        with TemporaryDirectory() as tmp_dir:
            bootstrap_token_path = Path(tmp_dir) / "bootstrap-token"
            access_token_path = Path(tmp_dir) / "access-token"
            bootstrap_token_path.write_text("stale-bootstrap-token", encoding="utf-8")
            access_token_path.write_text("stale-access-token", encoding="utf-8")
            updates: list[tuple[str, dict[str, object]]] = []

            class _FakeClient:
                def __init__(self, *, base_url: str, auth_header: str) -> None:
                    self.base_url = base_url
                    self.auth_header = auth_header

                def heartbeat_deployment_host(
                    self,
                    *,
                    access_token: str,
                    agent_version: str,
                    advertised_capabilities: tuple[str, ...],
                    state: str = "active",
                ) -> dict[str, object]:
                    raise module.BootstrapApiError("stale access token")

                def register_deployment_host(
                    self,
                    *,
                    bootstrap_token: str,
                    agent_version: str,
                    advertised_capabilities: tuple[str, ...],
                ) -> dict[str, object]:
                    if bootstrap_token == "stale-bootstrap-token":
                        raise module.BootstrapApiError("stale bootstrap token")
                    return {
                        "host": {"host_id": "host-2"},
                        "access_token": "fresh-access-token",
                    }

                def create_deployment_host(
                    self,
                    *,
                    label: str,
                    infrastructure_provider: str | None,
                    region: str | None,
                    capabilities: tuple[str, ...],
                ) -> dict[str, object]:
                    return {
                        "host": {
                            "host_id": "host-2",
                            "label": label,
                            "provider": "internal_coolify",
                            "infrastructure_provider": infrastructure_provider,
                            "region": region,
                            "capabilities": list(capabilities),
                            "state": "provisioning",
                        },
                        "bootstrap_token": "fresh-bootstrap-token",
                    }

                def list_tenants(self) -> list[dict[str, object]]:
                    return [{"tenant_id": "tenant-a"}]

                def get_tenant_deployment_plane(self, *, tenant_id: str) -> dict[str, object]:
                    assert tenant_id == "tenant-a"
                    return {
                        "provider": "internal_coolify",
                        "api_base_url": "https://builder.example.com/api/v1",
                        "managed_host_id": None,
                        "state": "active",
                        "secret_refs": {"coolify_api_token": "platform/COOLIFY_API_TOKEN"},
                    }

                def update_tenant_deployment_plane(self, *, tenant_id: str, payload: dict[str, object]) -> dict[str, object]:
                    updates.append((tenant_id, payload))
                    return payload

            config = module.DeploymentHostBootstrapConfig(
                api_base_url="http://api:4000",
                auth_header="Basic abc",
                host_label="managed-test",
                infrastructure_provider="hetzner",
                region="eu-west",
                capabilities=("restore_database", "postgres"),
                bootstrap_token_path=bootstrap_token_path,
                access_token_path=access_token_path,
                tenant_ids=(),
                assign_configured_tenants=True,
            )

            with patch.object(module, "AdminApiClient", _FakeClient):
                summary = module.ensure_deployment_host_bootstrap(config)

            self.assertEqual(summary["action"], "created")
            self.assertEqual(summary["host_id"], "host-2")
            self.assertEqual(bootstrap_token_path.read_text(encoding="utf-8"), "fresh-bootstrap-token")
            self.assertEqual(access_token_path.read_text(encoding="utf-8"), "fresh-access-token")
            self.assertEqual(len(updates), 1)
            self.assertEqual(updates[0][1]["managed_host_id"], "host-2")


if __name__ == "__main__":
    unittest.main()
