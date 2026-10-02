from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from cryptography.fernet import Fernet

from orchestrator.core.config import get_settings
from orchestrator.core.platform.secrets import encrypt_value
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.models import (
    AtlassianOAuthConnection,
    DeploymentHostCommand,
    ProjectDeploymentRestoreRun,
)
from orchestrator.worker import _has_available_webhook_job_once
from tests.test_support.db_harness import SqliteTemplateApiTestCase


class DeploymentHostApiTests(SqliteTemplateApiTestCase):
    _secrets_encryption_key: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._secrets_encryption_key = Fernet.generate_key().decode("utf-8")
        super().setUpClass()

    @classmethod
    def class_environment_overrides(cls) -> dict[str, str]:
        return {
            "ORCHESTRATOR_ADMIN_USERNAME": "admin",
            "ORCHESTRATOR_ADMIN_PASSWORD": "secret",
            "ORCHESTRATOR_ADMIN_TOKEN_SECRET": "admin-token-secret-for-tests-0123456789",
            "ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET": "unit-test-secret",
            "ORCHESTRATOR_ADMIN_UI_BASE_URL": "http://localhost:4100",
            "ORCHESTRATOR_PUBLIC_API_BASE_URL": "http://localhost:4000",
            "ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET": "jira-oauth-state-secret",
            "ORCHESTRATOR_GITHUB_APP_SLUG": "master-builder-app",
            "ORCHESTRATOR_SECRETS_ENCRYPTION_KEY": cls._secrets_encryption_key,
        }

    @classmethod
    def bootstrap_template_state(cls) -> None:
        cls._class_client.put(
            "/api/admin/secrets/platform%2FGITHUB_APP_SLUG",
            json={"value": "master-builder-app"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FGITHUB_APP_ID",
            json={"value": "12345"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FGITHUB_APP_PRIVATE_KEY",
            json={"value": "not-a-real-key-for-tests"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FJIRA_OAUTH_CLIENT_ID",
            json={"value": "jira-client-id"},
            auth=("admin", "secret"),
        )
        cls._class_client.put(
            "/api/admin/secrets/platform%2FJIRA_OAUTH_CLIENT_SECRET",
            json={"value": "jira-client-secret"},
            auth=("admin", "secret"),
        )

    def setUp(self) -> None:
        self.database_url = self._start_test_database(name_prefix="deployment-host-api")
        get_settings.cache_clear()
        reset_db_engine_cache()

    def tearDown(self) -> None:
        self._cleanup_test_database()
        get_settings.cache_clear()
        reset_db_engine_cache()

    def _tenant_payload(self) -> dict:
        return {
            "name": "Tenant A",
            "is_enabled": True,
            "jira": {
                "connection_id": "conn-1",
                "project_keys": ["TP"],
                "ready_statuses": ["Ready for Agent"],
                "ready_jql": 'project = TP AND status = "Ready for Agent"',
                "ready_label": "agent:ready",
                "in_progress_label": "agent:in-progress",
                "blocked_label": "agent:blocked",
                "done_label": "agent:done",
                "webhook_secret_ref": "secret/webhook",
            },
            "github": {
                "mode": "github_app",
                "webhook_secret_ref": "secret/github-webhook",
                "installation_id": "12345",
            },
            "repos": {
                "allowlist": ["https://github.com/example/repo"],
                "mapping_rules_by_project_key": {
                    "TP": "https://github.com/example/repo"
                },
                "mapping_rules_by_component": {},
                "fallback_repo": None,
            },
            "policy": {
                "allow_jira_transitions": False,
                "allow_pr_creation": True,
                "allow_code_reviews": True,
                "allow_pr_remediation": True,
                "allow_manual_pr_fix_requests": True,
                "allow_label_mutations": True,
                "max_runtime_minutes": 30,
                "max_dev_test_review_loops": 2,
                "max_concurrent_runs": 2,
                "allowed_commands": ["python -m unittest"],
                "require_agents_md": False,
                "codex_model": "gpt-5.4",
                "codex_reasoning_effort": "medium",
            },
            "discord": {
                "channel_id": "discord-channel-1",
                "notify_events": ["run_started"],
            },
        }

    def _insert_jira_connection(self, connection_id: str = "conn-1") -> None:
        session_factory = create_session_factory(self.database_url)
        settings = get_settings()
        now = datetime.now(timezone.utc)
        with session_factory() as session:
            session.add(
                AtlassianOAuthConnection(
                    connection_id=connection_id,
                    account_id="account-1",
                    account_email="test@example.com",
                    cloud_id="cloud-1",
                    site_url="https://example.atlassian.net",
                    scopes=["read:jira-work", "write:jira-work"],
                    access_token_encrypted=encrypt_value(
                        plaintext="access-token",
                        encryption_key=settings.secrets_encryption_key,
                    ),
                    refresh_token_encrypted=encrypt_value(
                        plaintext="refresh-token",
                        encryption_key=settings.secrets_encryption_key,
                    ),
                    access_token_expires_at=now + timedelta(hours=1),
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def test_registered_host_claims_no_command_when_queue_is_empty(self) -> None:
        self._insert_jira_connection()
        create_tenant = self.client.post(
            "/api/admin/tenants", json=self._tenant_payload(), auth=("admin", "secret")
        )
        self.assertEqual(create_tenant.status_code, 201, create_tenant.text)
        create_host_response = self.client.post(
            "/api/admin/deployment-hosts",
            json={
                "label": "empty-queue-host",
                "capabilities": ["local_preview_routes"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            create_host_response.status_code, 201, create_host_response.text
        )
        bootstrap_token = create_host_response.json()["bootstrap_token"]
        register_response = self.client.post(
            "/api/internal/deployment-hosts/register",
            json={
                "bootstrap_token": bootstrap_token,
                "agent_version": "test-agent",
                "advertised_capabilities": ["local_preview_routes"],
            },
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)
        access_token = register_response.json()["access_token"]

        claim_response = self.client.post(
            "/api/internal/deployment-hosts/commands/claim",
            headers={"Authorization": f"Bearer {access_token}"},
        )

        self.assertEqual(claim_response.status_code, 200, claim_response.text)
        self.assertIsNone(claim_response.json()["command"])

    def _create_project_with_database_backup(self) -> tuple[str, str]:
        create_project = self.client.post(
            "/api/admin/tenants/tenant-a/projects",
            json={
                "name": "restore-app",
                "github_repository": "https://github.com/example/restore-app",
                "jira_project_key": "RST",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_project.status_code, 201, create_project.text)
        project_id = create_project.json()["project_id"]
        create_app = self.client.post(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/apps",
            json={
                "name": "restore-app",
                "slug": "restore-app",
                "source_path": ".",
                "detected_runtime": "python",
                "detected_language": "python",
                "analysis_source": "test_fixture",
                "build_strategy": "dockerfile",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(create_app.status_code, 201, create_app.text)
        app_id = create_app.json()["app_id"]
        password_secret_response = self.client.put(
            "/api/admin/tenants/tenant-a/secrets/RESTORE_DB_PASSWORD",
            json={"value": "secret"},
            auth=("admin", "secret"),
        )
        self.assertEqual(
            password_secret_response.status_code, 200, password_secret_response.text
        )
        deployment_config_response = self.client.put(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-config",
            json={
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "dockerfile",
                "resources": [
                    {
                        "key": "db",
                        "kind": "postgres",
                        "name": "restore-db",
                        "config": {
                            "coolify_uuid": "db-uuid-1",
                            "coolify_container_name": "coolify-db-container",
                            "postgres_user": "app",
                            "postgres_password_secret_ref": "RESTORE_DB_PASSWORD",
                            "postgres_db": "app",
                        },
                    }
                ],
                "backup_policies": [
                    {
                        "key": "db-daily",
                        "resource_key": "db",
                        "schedule": "0 2 * * *",
                        "config": {"coolify_backup_uuid": "backup-uuid-1"},
                    }
                ],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            deployment_config_response.status_code, 200, deployment_config_response.text
        )
        resource_config = deployment_config_response.json()["resources"][0]["config"]
        self.assertNotIn("postgres_password", resource_config)
        self.assertEqual(
            resource_config["postgres_password_secret_ref"], "RESTORE_DB_PASSWORD"
        )
        return project_id, app_id

    def test_restore_run_dispatches_to_managed_host_command(self) -> None:
        self._insert_jira_connection()
        create_tenant = self.client.post(
            "/api/admin/tenants", json=self._tenant_payload(), auth=("admin", "secret")
        )
        self.assertEqual(create_tenant.status_code, 201, create_tenant.text)

        coolify_secret_response = self.client.put(
            "/api/admin/secrets/platform%2FCOOLIFY_API_TOKEN",
            json={"value": "coolify-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(coolify_secret_response.status_code, 200)

        create_host_response = self.client.post(
            "/api/admin/deployment-hosts",
            json={
                "label": "Builder EU West",
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "capabilities": ["restore_database", "postgres", "mysql", "mariadb"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            create_host_response.status_code, 201, create_host_response.text
        )
        host_id = create_host_response.json()["host"]["host_id"]
        bootstrap_token = create_host_response.json()["bootstrap_token"]

        register_response = self.client.post(
            "/api/internal/deployment-hosts/register",
            json={
                "bootstrap_token": bootstrap_token,
                "agent_version": "1.0.0",
                "advertised_capabilities": [
                    "restore_database",
                    "postgres",
                    "mysql",
                    "mariadb",
                ],
            },
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)

        deployment_plane_response = self.client.put(
            "/api/admin/tenants/tenant-a/deployment-plane",
            json={
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "apps.example.com",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "project-uuid-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-uuid-1",
                "coolify_destination_uuid": "destination-uuid-1",
                "managed_host_id": host_id,
                "secret_refs": {
                    "coolify_api_token": "platform/COOLIFY_API_TOKEN",
                },
                "state": "active",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            deployment_plane_response.status_code, 200, deployment_plane_response.text
        )

        project_id, app_id = self._create_project_with_database_backup()

        with patch(
            "orchestrator.api.admin.deployment_restore_service.CoolifyApiClient.list_database_backup_executions",
            return_value=[
                {
                    "id": "execution-uuid-1",
                    "status": "completed",
                    "created_at": "2026-04-14T12:00:00Z",
                    "completed_at": "2026-04-14T12:05:00Z",
                    "filename": "app.dump",
                    "path": "/var/lib/coolify/backups/app.dump",
                }
            ],
        ):
            restore_response = self.client.post(
                f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore",
                json={
                    "backup_key": "db-daily",
                    "resource_key": "db",
                    "execution_uuid": "execution-uuid-1",
                    "confirmation_value": "restore-app",
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(restore_response.status_code, 201, restore_response.text)
        self.assertEqual(restore_response.json()["status"], "queued")
        self.assertEqual(restore_response.json()["host_id"], host_id)
        self.assertIsNotNone(restore_response.json()["command_id"])

        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            restore_run = session.get(
                ProjectDeploymentRestoreRun, restore_response.json()["restore_run_id"]
            )
            self.assertIsNotNone(restore_run)
            assert restore_run is not None
            self.assertEqual(restore_run.host_id, host_id)
            self.assertIsNotNone(restore_run.command_id)

            command = session.get(DeploymentHostCommand, restore_run.command_id)
            self.assertIsNotNone(command)
            assert command is not None
            self.assertEqual(command.host_id, host_id)
            self.assertEqual(command.kind, "restore_database")
            self.assertEqual(command.status, "queued")

    def test_internal_host_agent_claims_and_completes_restore_command(self) -> None:
        self._insert_jira_connection()
        create_tenant = self.client.post(
            "/api/admin/tenants", json=self._tenant_payload(), auth=("admin", "secret")
        )
        self.assertEqual(create_tenant.status_code, 201, create_tenant.text)

        coolify_secret_response = self.client.put(
            "/api/admin/secrets/platform%2FCOOLIFY_API_TOKEN",
            json={"value": "coolify-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(coolify_secret_response.status_code, 200)

        create_host_response = self.client.post(
            "/api/admin/deployment-hosts",
            json={
                "label": "Builder EU West",
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "capabilities": ["restore_database", "postgres"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            create_host_response.status_code, 201, create_host_response.text
        )
        host_id = create_host_response.json()["host"]["host_id"]
        bootstrap_token = create_host_response.json()["bootstrap_token"]

        register_response = self.client.post(
            "/api/internal/deployment-hosts/register",
            json={
                "bootstrap_token": bootstrap_token,
                "agent_version": "1.0.0",
                "advertised_capabilities": ["restore_database", "postgres"],
            },
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)
        self.assertEqual(register_response.json()["host"]["host_id"], host_id)
        access_token = register_response.json()["access_token"]

        heartbeat_response = self.client.post(
            "/api/internal/deployment-hosts/heartbeat",
            json={
                "agent_version": "1.0.1",
                "advertised_capabilities": ["restore_database", "postgres"],
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(heartbeat_response.status_code, 200, heartbeat_response.text)
        self.assertEqual(heartbeat_response.json()["state"], "active")

        deployment_plane_response = self.client.put(
            "/api/admin/tenants/tenant-a/deployment-plane",
            json={
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "apps.example.com",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "project-uuid-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-uuid-1",
                "coolify_destination_uuid": "destination-uuid-1",
                "managed_host_id": host_id,
                "secret_refs": {
                    "coolify_api_token": "platform/COOLIFY_API_TOKEN",
                },
                "state": "active",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            deployment_plane_response.status_code, 200, deployment_plane_response.text
        )

        project_id, app_id = self._create_project_with_database_backup()
        with patch(
            "orchestrator.api.admin.deployment_restore_service.CoolifyApiClient.list_database_backup_executions",
            return_value=[
                {
                    "id": "execution-uuid-1",
                    "status": "completed",
                    "created_at": "2026-04-14T12:00:00Z",
                    "completed_at": "2026-04-14T12:05:00Z",
                    "filename": "app.dump",
                    "path": "/var/lib/coolify/backups/app.dump",
                }
            ],
        ):
            restore_response = self.client.post(
                f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore",
                json={
                    "backup_key": "db-daily",
                    "resource_key": "db",
                    "execution_uuid": "execution-uuid-1",
                    "confirmation_value": "restore-app",
                },
                auth=("admin", "secret"),
            )
        self.assertEqual(restore_response.status_code, 201, restore_response.text)
        restore_run_id = restore_response.json()["restore_run_id"]
        command_id = restore_response.json()["command_id"]

        claim_response = self.client.post(
            "/api/internal/deployment-hosts/commands/claim",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(claim_response.status_code, 200, claim_response.text)
        self.assertEqual(claim_response.json()["command"]["command_id"], command_id)
        claim_id = claim_response.json()["command"]["claim_id"]

        start_response = self.client.post(
            f"/api/internal/deployment-hosts/commands/{command_id}/start",
            json={"claim_id": claim_id},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)

        result_response = self.client.post(
            f"/api/internal/deployment-hosts/commands/{command_id}/result",
            json={
                "claim_id": claim_id,
                "status": "succeeded",
                "result": {"stdout": "restore complete"},
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(result_response.status_code, 200, result_response.text)
        self.assertEqual(result_response.json()["status"], "succeeded")

        restore_detail = self.client.get(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore-runs/{restore_run_id}",
            auth=("admin", "secret"),
        )
        self.assertEqual(restore_detail.status_code, 200, restore_detail.text)
        self.assertEqual(restore_detail.json()["status"], "succeeded")
        self.assertEqual(restore_detail.json()["command_id"], command_id)

    def test_host_bootstrap_token_cannot_be_replayed_after_registration(self) -> None:
        self._insert_jira_connection()
        create_tenant = self.client.post(
            "/api/admin/tenants", json=self._tenant_payload(), auth=("admin", "secret")
        )
        self.assertEqual(create_tenant.status_code, 201, create_tenant.text)

        create_host_response = self.client.post(
            "/api/admin/deployment-hosts",
            json={
                "label": "Builder EU West",
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "capabilities": ["restore_database", "postgres"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            create_host_response.status_code, 201, create_host_response.text
        )
        bootstrap_token = create_host_response.json()["bootstrap_token"]

        first_register = self.client.post(
            "/api/internal/deployment-hosts/register",
            json={
                "bootstrap_token": bootstrap_token,
                "agent_version": "1.0.0",
                "advertised_capabilities": ["restore_database", "postgres"],
            },
        )
        self.assertEqual(first_register.status_code, 200, first_register.text)
        first_access_token = first_register.json()["access_token"]

        second_register = self.client.post(
            "/api/internal/deployment-hosts/register",
            json={
                "bootstrap_token": bootstrap_token,
                "agent_version": "1.0.1",
                "advertised_capabilities": ["restore_database", "postgres"],
            },
        )
        self.assertEqual(second_register.status_code, 401, second_register.text)

        fresh_heartbeat = self.client.post(
            "/api/internal/deployment-hosts/heartbeat",
            json={
                "agent_version": "1.0.1",
                "advertised_capabilities": ["restore_database", "postgres"],
            },
            headers={"Authorization": f"Bearer {first_access_token}"},
        )
        self.assertEqual(fresh_heartbeat.status_code, 200, fresh_heartbeat.text)

    def test_claim_route_fails_stale_running_restore_command(self) -> None:
        self._insert_jira_connection()
        create_tenant = self.client.post(
            "/api/admin/tenants", json=self._tenant_payload(), auth=("admin", "secret")
        )
        self.assertEqual(create_tenant.status_code, 201, create_tenant.text)

        coolify_secret_response = self.client.put(
            "/api/admin/secrets/platform%2FCOOLIFY_API_TOKEN",
            json={"value": "coolify-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(coolify_secret_response.status_code, 200)

        create_host_response = self.client.post(
            "/api/admin/deployment-hosts",
            json={
                "label": "Builder EU West",
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "capabilities": ["restore_database", "postgres"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            create_host_response.status_code, 201, create_host_response.text
        )
        host_id = create_host_response.json()["host"]["host_id"]
        bootstrap_token = create_host_response.json()["bootstrap_token"]

        register_response = self.client.post(
            "/api/internal/deployment-hosts/register",
            json={
                "bootstrap_token": bootstrap_token,
                "agent_version": "1.0.0",
                "advertised_capabilities": ["restore_database", "postgres"],
            },
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)
        access_token = register_response.json()["access_token"]

        deployment_plane_response = self.client.put(
            "/api/admin/tenants/tenant-a/deployment-plane",
            json={
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "apps.example.com",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "project-uuid-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-uuid-1",
                "coolify_destination_uuid": "destination-uuid-1",
                "managed_host_id": host_id,
                "secret_refs": {
                    "coolify_api_token": "platform/COOLIFY_API_TOKEN",
                },
                "state": "active",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            deployment_plane_response.status_code, 200, deployment_plane_response.text
        )

        project_id, app_id = self._create_project_with_database_backup()
        with patch(
            "orchestrator.api.admin.deployment_restore_service.CoolifyApiClient.list_database_backup_executions",
            return_value=[
                {
                    "id": "execution-uuid-1",
                    "status": "completed",
                    "created_at": "2026-04-14T12:00:00Z",
                    "completed_at": "2026-04-14T12:05:00Z",
                    "filename": "app.dump",
                    "path": "/var/lib/coolify/backups/app.dump",
                }
            ],
        ):
            restore_response = self.client.post(
                f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore",
                json={
                    "backup_key": "db-daily",
                    "resource_key": "db",
                    "execution_uuid": "execution-uuid-1",
                    "confirmation_value": "restore-app",
                },
                auth=("admin", "secret"),
            )
        self.assertEqual(restore_response.status_code, 201, restore_response.text)
        command_id = restore_response.json()["command_id"]
        restore_run_id = restore_response.json()["restore_run_id"]

        claim_response = self.client.post(
            "/api/internal/deployment-hosts/commands/claim",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(claim_response.status_code, 200, claim_response.text)
        claim_id = claim_response.json()["command"]["claim_id"]
        start_response = self.client.post(
            f"/api/internal/deployment-hosts/commands/{command_id}/start",
            json={"claim_id": claim_id},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)

        session_factory = create_session_factory(self.database_url)
        stale_time = datetime.now(timezone.utc) - timedelta(seconds=5)
        with session_factory() as session:
            command = session.get(DeploymentHostCommand, command_id)
            assert command is not None
            command.lease_expires_at = stale_time
            session.commit()

        reclaim_response = self.client.post(
            "/api/internal/deployment-hosts/commands/claim",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(reclaim_response.status_code, 200, reclaim_response.text)
        self.assertIsNone(reclaim_response.json()["command"])

        restore_detail = self.client.get(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore-runs/{restore_run_id}",
            auth=("admin", "secret"),
        )
        self.assertEqual(restore_detail.status_code, 200, restore_detail.text)
        self.assertEqual(restore_detail.json()["status"], "failed")
        self.assertIn("expired", str(restore_detail.json()["last_error"]).lower())

    def test_worker_probe_fails_stale_running_restore_command_without_host_reclaim(
        self,
    ) -> None:
        self._insert_jira_connection()
        create_tenant = self.client.post(
            "/api/admin/tenants", json=self._tenant_payload(), auth=("admin", "secret")
        )
        self.assertEqual(create_tenant.status_code, 201, create_tenant.text)

        coolify_secret_response = self.client.put(
            "/api/admin/secrets/platform%2FCOOLIFY_API_TOKEN",
            json={"value": "coolify-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(coolify_secret_response.status_code, 200)

        create_host_response = self.client.post(
            "/api/admin/deployment-hosts",
            json={
                "label": "Builder EU West",
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "capabilities": ["restore_database", "postgres"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            create_host_response.status_code, 201, create_host_response.text
        )
        host_id = create_host_response.json()["host"]["host_id"]
        bootstrap_token = create_host_response.json()["bootstrap_token"]

        register_response = self.client.post(
            "/api/internal/deployment-hosts/register",
            json={
                "bootstrap_token": bootstrap_token,
                "agent_version": "1.0.0",
                "advertised_capabilities": ["restore_database", "postgres"],
            },
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)
        access_token = register_response.json()["access_token"]

        deployment_plane_response = self.client.put(
            "/api/admin/tenants/tenant-a/deployment-plane",
            json={
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "apps.example.com",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "project-uuid-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-uuid-1",
                "coolify_destination_uuid": "destination-uuid-1",
                "managed_host_id": host_id,
                "secret_refs": {
                    "coolify_api_token": "platform/COOLIFY_API_TOKEN",
                },
                "state": "active",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            deployment_plane_response.status_code, 200, deployment_plane_response.text
        )

        project_id, app_id = self._create_project_with_database_backup()
        with patch(
            "orchestrator.api.admin.deployment_restore_service.CoolifyApiClient.list_database_backup_executions",
            return_value=[
                {
                    "id": "execution-uuid-1",
                    "status": "completed",
                    "created_at": "2026-04-14T12:00:00Z",
                    "completed_at": "2026-04-14T12:05:00Z",
                    "filename": "app.dump",
                    "path": "/var/lib/coolify/backups/app.dump",
                }
            ],
        ):
            restore_response = self.client.post(
                f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore",
                json={
                    "backup_key": "db-daily",
                    "resource_key": "db",
                    "execution_uuid": "execution-uuid-1",
                    "confirmation_value": "restore-app",
                },
                auth=("admin", "secret"),
            )
        self.assertEqual(restore_response.status_code, 201, restore_response.text)
        command_id = restore_response.json()["command_id"]
        restore_run_id = restore_response.json()["restore_run_id"]

        claim_response = self.client.post(
            "/api/internal/deployment-hosts/commands/claim",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(claim_response.status_code, 200, claim_response.text)
        claim_id = claim_response.json()["command"]["claim_id"]
        start_response = self.client.post(
            f"/api/internal/deployment-hosts/commands/{command_id}/start",
            json={"claim_id": claim_id},
            headers={"Authorization": f"Bearer {access_token}"},
        )
        self.assertEqual(start_response.status_code, 200, start_response.text)

        session_factory = create_session_factory(self.database_url)
        stale_time = datetime.now(timezone.utc) - timedelta(seconds=5)
        with session_factory() as session:
            command = session.get(DeploymentHostCommand, command_id)
            assert command is not None
            command.lease_expires_at = stale_time
            session.commit()

        has_job = _has_available_webhook_job_once(session_factory=session_factory)
        self.assertFalse(has_job)

        restore_detail = self.client.get(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore-runs/{restore_run_id}",
            auth=("admin", "secret"),
        )
        self.assertEqual(restore_detail.status_code, 200, restore_detail.text)
        self.assertEqual(restore_detail.json()["status"], "failed")
        self.assertIn("expired", str(restore_detail.json()["last_error"]).lower())

    def test_restore_run_requires_active_managed_host(self) -> None:
        self._insert_jira_connection()
        create_tenant = self.client.post(
            "/api/admin/tenants", json=self._tenant_payload(), auth=("admin", "secret")
        )
        self.assertEqual(create_tenant.status_code, 201, create_tenant.text)

        coolify_secret_response = self.client.put(
            "/api/admin/secrets/platform%2FCOOLIFY_API_TOKEN",
            json={"value": "coolify-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(coolify_secret_response.status_code, 200)

        deployment_plane_response = self.client.put(
            "/api/admin/tenants/tenant-a/deployment-plane",
            json={
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "apps.example.com",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "project-uuid-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-uuid-1",
                "coolify_destination_uuid": "destination-uuid-1",
                "secret_refs": {
                    "coolify_api_token": "platform/COOLIFY_API_TOKEN",
                },
                "state": "active",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            deployment_plane_response.status_code, 200, deployment_plane_response.text
        )

        project_id, app_id = self._create_project_with_database_backup()

        restore_response = self.client.post(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore",
            json={
                "backup_key": "db-daily",
                "resource_key": "db",
                "execution_uuid": "execution-uuid-1",
                "confirmation_value": "restore-app",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(restore_response.status_code, 409, restore_response.text)
        self.assertIn("managed host", restore_response.json()["detail"].lower())

    def test_restore_run_uses_single_active_host_when_plane_has_no_explicit_assignment(
        self,
    ) -> None:
        self._insert_jira_connection()
        create_tenant = self.client.post(
            "/api/admin/tenants", json=self._tenant_payload(), auth=("admin", "secret")
        )
        self.assertEqual(create_tenant.status_code, 201, create_tenant.text)

        coolify_secret_response = self.client.put(
            "/api/admin/secrets/platform%2FCOOLIFY_API_TOKEN",
            json={"value": "coolify-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(coolify_secret_response.status_code, 200)

        create_host_response = self.client.post(
            "/api/admin/deployment-hosts",
            json={
                "label": "Builder EU West",
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "capabilities": ["restore_database", "postgres", "mysql", "mariadb"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            create_host_response.status_code, 201, create_host_response.text
        )
        host_id = create_host_response.json()["host"]["host_id"]
        bootstrap_token = create_host_response.json()["bootstrap_token"]

        register_response = self.client.post(
            "/api/internal/deployment-hosts/register",
            json={
                "bootstrap_token": bootstrap_token,
                "agent_version": "1.0.0",
                "advertised_capabilities": [
                    "restore_database",
                    "postgres",
                    "mysql",
                    "mariadb",
                ],
            },
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)

        deployment_plane_response = self.client.put(
            "/api/admin/tenants/tenant-a/deployment-plane",
            json={
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "apps.example.com",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "project-uuid-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-uuid-1",
                "coolify_destination_uuid": "destination-uuid-1",
                "secret_refs": {
                    "coolify_api_token": "platform/COOLIFY_API_TOKEN",
                },
                "state": "active",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            deployment_plane_response.status_code, 200, deployment_plane_response.text
        )

        project_id, app_id = self._create_project_with_database_backup()
        with patch(
            "orchestrator.api.admin.deployment_restore_service.CoolifyApiClient.list_database_backup_executions",
            return_value=[
                {
                    "id": "execution-uuid-1",
                    "status": "completed",
                    "created_at": "2026-04-14T12:00:00Z",
                    "completed_at": "2026-04-14T12:05:00Z",
                    "filename": "app.dump",
                    "path": "/var/lib/coolify/backups/app.dump",
                }
            ],
        ):
            restore_response = self.client.post(
                f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore",
                json={
                    "backup_key": "db-daily",
                    "resource_key": "db",
                    "execution_uuid": "execution-uuid-1",
                    "confirmation_value": "restore-app",
                },
                auth=("admin", "secret"),
            )

        self.assertEqual(restore_response.status_code, 201, restore_response.text)
        self.assertEqual(restore_response.json()["host_id"], host_id)

    def test_restore_run_rejects_default_host_with_region_mismatch(self) -> None:
        self._insert_jira_connection()
        create_tenant = self.client.post(
            "/api/admin/tenants", json=self._tenant_payload(), auth=("admin", "secret")
        )
        self.assertEqual(create_tenant.status_code, 201, create_tenant.text)

        coolify_secret_response = self.client.put(
            "/api/admin/secrets/platform%2FCOOLIFY_API_TOKEN",
            json={"value": "coolify-token"},
            auth=("admin", "secret"),
        )
        self.assertEqual(coolify_secret_response.status_code, 200)

        create_host_response = self.client.post(
            "/api/admin/deployment-hosts",
            json={
                "label": "Builder US East",
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "us-east",
                "capabilities": ["restore_database", "postgres"],
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            create_host_response.status_code, 201, create_host_response.text
        )
        bootstrap_token = create_host_response.json()["bootstrap_token"]

        register_response = self.client.post(
            "/api/internal/deployment-hosts/register",
            json={
                "bootstrap_token": bootstrap_token,
                "agent_version": "1.0.0",
                "advertised_capabilities": ["restore_database", "postgres"],
            },
        )
        self.assertEqual(register_response.status_code, 200, register_response.text)

        deployment_plane_response = self.client.put(
            "/api/admin/tenants/tenant-a/deployment-plane",
            json={
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "apps.example.com",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "project-uuid-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-uuid-1",
                "coolify_destination_uuid": "destination-uuid-1",
                "secret_refs": {
                    "coolify_api_token": "platform/COOLIFY_API_TOKEN",
                },
                "state": "active",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(
            deployment_plane_response.status_code, 200, deployment_plane_response.text
        )

        project_id, app_id = self._create_project_with_database_backup()

        restore_response = self.client.post(
            f"/api/admin/tenants/tenant-a/projects/{project_id}/apps/{app_id}/deployment-backups/restore",
            json={
                "backup_key": "db-daily",
                "resource_key": "db",
                "execution_uuid": "execution-uuid-1",
                "confirmation_value": "restore-app",
            },
            auth=("admin", "secret"),
        )
        self.assertEqual(restore_response.status_code, 409, restore_response.text)
        self.assertIn("region", restore_response.json()["detail"].lower())


if __name__ == "__main__":
    unittest.main()
