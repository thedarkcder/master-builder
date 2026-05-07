from __future__ import annotations

import os
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException, status
from pydantic import ValidationError

from orchestrator.api.admin.project_service import AdminProjectService
from orchestrator.api.admin.deployment_release_service import create_project_deployment_release
from orchestrator.api.deployment_schemas import ProjectDeploymentConfigRead, ProjectDeploymentConfigWrite
from orchestrator.api.schemas import ProjectDeploymentReleaseCreate
from orchestrator.core.deployment_runtime import _select_release_for_event
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, ProjectApp, ProjectDeploymentRelease, Tenant


class DeploymentContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/deployment_contracts.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        reset_db_engine_cache()

    def _seed_tenant_project_app(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant 1",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config=None,
                    deployment_plane_config={
                        "provider": "internal_coolify",
                        "infrastructure_provider": "hetzner",
                        "region": "eu-west",
                        "base_domain": "apps.example.com",
                        "platform_subdomain": "builder",
                        "api_base_url": "https://builder.apps.example.com/api/v1",
                        "coolify_project_uuid": "coolify-project-1",
                        "coolify_environment_name": "production",
                        "coolify_server_uuid": "server-1",
                        "coolify_destination_uuid": "destination-1",
                        "secret_refs": {"coolify_api_token": "platform/COOLIFY_API_TOKEN"},
                        "state": "active",
                    },
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-1",
                    name="Project 1",
                    github_repository="https://github.com/example/repo",
                    jira_project_key="TP",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config=None,
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ProjectApp(
                    app_id="app-1",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    name="Web",
                    slug="web",
                    source_path=".",
                    detection_confidence=1.0,
                    detected_runtime="python",
                    detected_language="python",
                    analysis_source="test",
                    build_strategy="dockerfile",
                    exposed_port=8000,
                    healthcheck="/health",
                    start_command="uvicorn app:app",
                    env_schema_json={},
                    secret_schema_json={},
                    deployment_config={
                        "enabled": True,
                        "environment_name": "production",
                        "source_strategy": "dockerfile",
                        "domains": [{"key": "primary", "host": "web.apps.example.com"}],
                        "resources": [],
                        "backup_policies": [],
                    },
                    status="ready",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    @staticmethod
    def _admin_project_service() -> AdminProjectService:
        return AdminProjectService(
            normalize_project_repo=lambda value: value,
            normalize_project_key=lambda value: value,
            normalize_project_policy_overrides=lambda value: dict(value or {}),
            normalize_string_map=lambda value: dict(value or {}),
            normalize_project_architecture_docs_config=lambda value: dict(value or {}),
            normalize_project_discord_config=lambda value: dict(value or {}),
            with_preserved_discord_system_fields=lambda existing, incoming: dict(incoming or {}),
            resolve_project_discord_channel_binding=lambda **kwargs: {},
            sync_tenant_jira_project_keys=lambda **kwargs: None,
            ensure_project_repository_checkout=lambda **kwargs: None,
            resolve_project_run_board_id=lambda **kwargs: None,
            project_to_schema=lambda **kwargs: {},
            settings_factory=lambda: object(),
        )

    def test_deployment_config_write_rejects_raw_secret_material(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentConfigWrite.model_validate(
                {
                    "resources": [
                        {
                            "key": "db",
                            "kind": "postgres",
                            "config": {"postgres_password": "raw-secret"},
                        }
                    ]
                }
            )

    def test_deployment_config_read_redacts_legacy_raw_secret_material(self) -> None:
        config = ProjectDeploymentConfigRead.model_validate(
            {
                "resources": [
                    {
                        "key": "db",
                        "kind": "postgres",
                        "config": {
                            "postgres_user": "app",
                            "postgres_password": "raw-secret",
                            "postgres_password_secret_ref": "RESTORE_DB_PASSWORD",
                        },
                    }
                ]
            }
        )

        resource_config = config.model_dump()["resources"][0]["config"]
        self.assertNotIn("postgres_password", resource_config)
        self.assertEqual(resource_config["postgres_password_secret_ref"], "RESTORE_DB_PASSWORD")

    def test_listing_project_apps_does_not_create_default_app(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                Tenant(
                    tenant_id="tenant-1",
                    name="Tenant 1",
                    is_enabled=True,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="project-1",
                    tenant_id="tenant-1",
                    name="Project 1",
                    github_repository="https://github.com/example/repo",
                    jira_project_key="TP",
                    policy_overrides={},
                    environment={},
                    secret_refs={},
                    discord_config=None,
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            apps = self._admin_project_service().list_project_apps(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
            )

            self.assertEqual(apps, [])
            self.assertEqual(session.query(ProjectApp).count(), 0)

    def test_deployment_webhook_requires_strong_release_identifier(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-1",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="app-1",
                    provider="internal_coolify",
                    status="deploying",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref=None,
                    commit_sha=None,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={"deployment_uuid": "deployment-1", "application_uuid": "app-uuid-1"},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            self.assertIsNone(
                _select_release_for_event(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    deployment_uuid=None,
                    application_uuid=None,
                )
            )
            self.assertIsNone(
                _select_release_for_event(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    deployment_uuid="unknown",
                    application_uuid=None,
                )
            )
            matched = _select_release_for_event(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                deployment_uuid="deployment-1",
                application_uuid=None,
            )
            self.assertIsNotNone(matched)
            assert matched is not None
            self.assertEqual(matched.release_id, "release-1")

    def test_release_row_is_durable_when_provider_submission_fails(self) -> None:
        self._seed_tenant_project_app()
        with self.session_factory() as session:
            with patch(
                "orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release",
                side_effect=HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="provider down"),
            ):
                with self.assertRaises(HTTPException):
                    create_project_deployment_release(
                        session=session,
                        tenant_id="tenant-1",
                        project_id="project-1",
                        payload=ProjectDeploymentReleaseCreate(app_id="app-1", git_ref="main"),
                        requested_by_user_id="admin",
                    )

            releases = session.query(ProjectDeploymentRelease).all()
            self.assertEqual(len(releases), 1)
            self.assertEqual(releases[0].status, "failed")
            self.assertEqual(releases[0].last_error, "provider down")
