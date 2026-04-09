from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit


def _load_module():
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "wire_internal_coolify_environment.py"
    spec = importlib.util.spec_from_file_location("wire_internal_coolify_environment", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load wire_internal_coolify_environment module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class WireInternalCoolifyEnvironmentTests(unittest.TestCase):
    def test_tenant_secret_ref_builds_scoped_ref(self) -> None:
        module = _load_module()
        self.assertEqual(
            module.tenant_secret_ref(tenant_id="tenant-alpha", secret_key="COOLIFY_API_TOKEN"),
            "tenant/tenant-alpha/COOLIFY_API_TOKEN",
        )
        with self.assertRaises(module.WireConfigError):
            module.tenant_secret_ref(tenant_id="tenant-alpha", secret_key="platform/COOLIFY_API_TOKEN")

    def test_merge_deployment_plane_payload_preserves_and_merges_secret_refs(self) -> None:
        module = _load_module()
        existing_plane = {
            "provider": "internal_coolify",
            "state": "degraded",
            "api_base_url": "https://coolify.example.com/api/v1",
            "secret_refs": {"coolify_api_token": "tenant/tenant-alpha/COOLIFY_API_TOKEN"},
            "last_error": "temporary",
        }
        merged = module.merge_deployment_plane_payload(
            existing_plane=existing_plane,
            deployment_state="active",
            infrastructure_provider="aws",
            region="us-east-1",
            base_domain="apps.example.com",
            platform_subdomain="builder",
            coolify_api_base_url="https://coolify.example.com/api/v1",
            coolify_project_uuid="project-uuid-1",
            coolify_environment_name="production",
            coolify_server_uuid="server-uuid-1",
            coolify_destination_uuid="destination-uuid-1",
            secret_ref_updates={"coolify_webhook_token": "tenant/tenant-alpha/COOLIFY_WEBHOOK_TOKEN"},
        )
        self.assertEqual(merged["provider"], "internal_coolify")
        self.assertEqual(merged["state"], "active")
        self.assertEqual(merged["infrastructure_provider"], "aws")
        self.assertEqual(merged["region"], "us-east-1")
        self.assertEqual(merged["base_domain"], "apps.example.com")
        self.assertEqual(merged["platform_subdomain"], "builder")
        self.assertEqual(merged["coolify_project_uuid"], "project-uuid-1")
        self.assertEqual(merged["coolify_environment_name"], "production")
        self.assertEqual(
            merged["secret_refs"],
            {
                "coolify_api_token": "tenant/tenant-alpha/COOLIFY_API_TOKEN",
                "coolify_webhook_token": "tenant/tenant-alpha/COOLIFY_WEBHOOK_TOKEN",
            },
        )

    def test_build_webhook_url_encodes_project_and_token(self) -> None:
        module = _load_module()
        url = module.build_webhook_url(
            public_api_base_url="https://orchestrator.example.com/",
            tenant_id="tenant-alpha",
            project_id="project/with spaces",
            token="webhook/token?x=1",
        )
        parts = urlsplit(url)
        self.assertEqual(parts.scheme, "https")
        self.assertEqual(parts.netloc, "orchestrator.example.com")
        self.assertEqual(parts.query, "")
        self.assertEqual(
            parts.path,
            "/deployments/coolify/webhook/tenant-alpha/project%2Fwith%20spaces/webhook%2Ftoken%3Fx%3D1",
        )

    def test_build_wire_config_generates_webhook_token_when_requested(self) -> None:
        module = _load_module()
        args = module.parse_args(
            [
                "--api-base-url",
                "https://orchestrator.example.com",
                "--tenant-id",
                "tenant-alpha",
                "--admin-username",
                "admin",
                "--admin-password",
                "secret",
                "--generate-webhook-token",
                "--tenant-secret-env",
                "BACKUP_BUCKET=BACKUP_BUCKET_ENV",
                "--plane-secret-ref",
                "backup_bucket=BACKUP_BUCKET",
            ]
        )
        config = module.build_wire_config(
            args=args,
            environ={
                "COOLIFY_API_TOKEN": "coolify-api-token-value",
                "BACKUP_BUCKET_ENV": "mb-backups",
            },
        )
        self.assertTrue(config.webhook_token_value)
        self.assertEqual(config.webhook_token_source, "generated")
        self.assertEqual(config.tenant_secret_values["COOLIFY_API_TOKEN"], "coolify-api-token-value")
        self.assertEqual(config.tenant_secret_values["BACKUP_BUCKET"], "mb-backups")
        self.assertEqual(
            config.deployment_plane_secret_ref_updates["coolify_api_token"],
            "tenant/tenant-alpha/COOLIFY_API_TOKEN",
        )
        self.assertEqual(
            config.deployment_plane_secret_ref_updates["backup_bucket"],
            "tenant/tenant-alpha/BACKUP_BUCKET",
        )


if __name__ == "__main__":
    unittest.main()
