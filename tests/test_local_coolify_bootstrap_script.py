from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import bootstrap_local_coolify


class LocalCoolifyBootstrapScriptTests(unittest.TestCase):
    def test_bootstrap_registers_local_ssh_docker_host_with_coolify(self) -> None:
        script = Path("scripts/bootstrap_local_coolify.py").read_text(encoding="utf-8")

        self.assertIn("database_url = _psycopg_database_url(_required_env(\"ORCHESTRATOR_DATABASE_URL\"))", script)
        self.assertIn("def _psycopg_database_url(database_url: str) -> str:", script)
        self.assertIn("LOCAL_SSH_HOST_CONTAINER = \"master-builder-coolify-ssh-host\"", script)
        self.assertIn("LOCAL_SSH_HOST_KEY_NAME = \"Master Builder local Docker host key\"", script)
        self.assertIn("def _configure_coolify_local_server(", script)
        self.assertIn("private_key_id={int(private_key_id)}", script)
        self.assertIn("set is_reachable=true", script)
        self.assertIn("set ip={_sql(LOCAL_SSH_HOST_CONTAINER)}", script)
        self.assertIn("plane[\"base_domain\"] = _resolve_local_preview_base_domain(", script)
        self.assertIn("plane[\"api_base_url\"] = _resolve_local_coolify_api_base_url(", script)
        self.assertIn("secret_refs[\"coolify_api_token\"] = _coolify_token_ref(tenant)", script)
        self.assertIn("plane[\"secret_refs\"] = secret_refs", script)
        self.assertIn("for target_tenant in tenants:", script)

    def test_coolify_token_backfills_missing_tenant_secret_from_existing_working_token(self) -> None:
        tenants = [
            bootstrap_local_coolify.TenantPlane(
                tenant_id="tenant-a",
                name="Tenant A",
                github_config={},
                deployment_plane_config={"secret_refs": {"coolify_api_token": "tenant/tenant-a/COOLIFY_API_TOKEN"}},
            ),
            bootstrap_local_coolify.TenantPlane(
                tenant_id="tenant-b",
                name="Tenant B",
                github_config={},
                deployment_plane_config={"secret_refs": {"coolify_api_token": "tenant/tenant-b/COOLIFY_API_TOKEN"}},
            ),
        ]
        secrets_by_ref = {"tenant/tenant-a/COOLIFY_API_TOKEN": "working-token"}
        upserts: list[tuple[str, str]] = []

        def _managed_secret(_connection, _encryption_key, secret_ref):
            return secrets_by_ref.get(secret_ref)

        def _upsert_managed_secret(_connection, _encryption_key, secret_ref, token):
            secrets_by_ref[secret_ref] = token
            upserts.append((secret_ref, token))

        with (
            patch.object(bootstrap_local_coolify, "_managed_secret", side_effect=_managed_secret),
            patch.object(bootstrap_local_coolify, "_upsert_managed_secret", side_effect=_upsert_managed_secret),
            patch.object(bootstrap_local_coolify, "_coolify_token_works", return_value=True),
            patch.object(bootstrap_local_coolify, "_create_coolify_token") as create_token,
        ):
            token = bootstrap_local_coolify._coolify_token(
                connection=object(),
                encryption_key="key",
                coolify_local_api_base_url="http://localhost:8000/api/v1",
                coolify_container_name="coolify",
                tenants=tenants,
            )

        self.assertEqual(token, "working-token")
        self.assertEqual(upserts, [("tenant/tenant-b/COOLIFY_API_TOKEN", "working-token")])
        create_token.assert_not_called()

    def test_bootstrap_verifies_ssh_from_inside_coolify_container(self) -> None:
        script = Path("scripts/bootstrap_local_coolify.py").read_text(encoding="utf-8")

        self.assertIn("_ensure_local_ssh_host(root_dir=root_dir, coolify_container_name=coolify_container_name)", script)
        self.assertIn("_wait_for_local_ssh_host(coolify_container_name=coolify_container_name, private_key=private_key)", script)
        self.assertIn("\"docker\",", script)
        self.assertIn("\"exec\",", script)
        self.assertIn("coolify_container_name", script)
        self.assertIn("docker compose version", script)

    def test_bootstrap_does_not_delete_existing_local_coolify_host(self) -> None:
        script = Path("scripts/bootstrap_local_coolify.py").read_text(encoding="utf-8")

        self.assertIn("def _docker_container_exists(container_name: str) -> bool:", script)
        self.assertIn("def _ensure_container_running(container_name: str) -> None:", script)
        self.assertIn("def _ensure_container_network(container_name: str, network_name: str) -> None:", script)
        self.assertNotIn("[\"docker\", \"rm\", \"-f\", LOCAL_SSH_HOST_CONTAINER]", script)

    def test_local_ssh_host_image_includes_docker_compose(self) -> None:
        dockerfile = Path("scripts/local_coolify_ssh_host.Dockerfile").read_text(encoding="utf-8")

        self.assertIn("FROM docker:27.5.1-cli-alpine3.21", dockerfile)
        self.assertIn("docker-cli-compose", dockerfile)

    def test_local_preview_base_domain_replaces_localhost_with_lan_ip_domain(self) -> None:
        base_domain = bootstrap_local_coolify._resolve_local_preview_base_domain(
            existing_base_domain="bsktpay-2.localhost:8088",
            configured_base_domain="",
            configured_lan_ip="192.168.0.118",
            proxy_port="8088",
        )

        self.assertEqual(base_domain, "192-168-0-118.sslip.io:8088")

    def test_local_preview_base_domain_preserves_real_configured_domain(self) -> None:
        base_domain = bootstrap_local_coolify._resolve_local_preview_base_domain(
            existing_base_domain="apps.example.com",
            configured_base_domain="",
            configured_lan_ip="192.168.0.118",
            proxy_port="8088",
        )

        self.assertEqual(base_domain, "apps.example.com")

    def test_local_preview_base_domain_allows_explicit_override(self) -> None:
        base_domain = bootstrap_local_coolify._resolve_local_preview_base_domain(
            existing_base_domain="bsktpay-2.localhost:8088",
            configured_base_domain="preview.internal.test:8088",
            configured_lan_ip="192.168.0.118",
            proxy_port="8088",
        )

        self.assertEqual(base_domain, "preview.internal.test:8088")

    def test_local_preview_base_domain_rejects_loopback_lan_ip(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "reachable from another LAN device"):
            bootstrap_local_coolify._resolve_local_preview_base_domain(
                existing_base_domain="bsktpay-2.localhost:8088",
                configured_base_domain="",
                configured_lan_ip="127.0.0.1",
                proxy_port="8088",
            )

    def test_local_coolify_api_base_url_rewrites_host_local_variants(self) -> None:
        self.assertEqual(
            bootstrap_local_coolify._resolve_local_coolify_api_base_url("http://localhost:8000/api/v1"),
            "http://host.docker.internal:8000/api/v1",
        )
        self.assertEqual(
            bootstrap_local_coolify._resolve_local_coolify_api_base_url("http://127.0.0.1:8000/api/v1"),
            "http://host.docker.internal:8000/api/v1",
        )

    def test_local_coolify_api_base_url_preserves_remote_values(self) -> None:
        self.assertEqual(
            bootstrap_local_coolify._resolve_local_coolify_api_base_url("https://coolify.example.com/api/v1"),
            "https://coolify.example.com/api/v1",
        )

    def test_bootstrap_does_not_use_environment_name_as_global_environment_uuid(self) -> None:
        script = Path("scripts/bootstrap_local_coolify.py").read_text(encoding="utf-8")

        self.assertIn("environment_uuid = _coolify_uuid()", script)
        self.assertIn("select {_sql(environment_uuid)}, {_sql(environment_name)}, p.id", script)
        self.assertNotIn("select {_sql(environment_name)}, {_sql(environment_name)}, p.id", script)
