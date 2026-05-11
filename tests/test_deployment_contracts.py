from __future__ import annotations

import importlib.util
import os
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select

from orchestrator.api.admin.project_service import AdminProjectService
from orchestrator.api.admin.deployment_release_service import (
    _coolify_docker_compose_routes,
    _release_service_urls,
    build_deployment_environment_values,
    create_project_deployment_release,
    submit_internal_coolify_release,
    update_project_deployment_release_status,
    verify_release_route_bindings,
)
from orchestrator.api.deployment_schemas import (
    ProjectDeploymentConfigRead,
    ProjectDeploymentConfigWrite,
    ProjectDeploymentPolicyWrite,
)
from orchestrator.api.schemas import (
    ProjectDeploymentReleaseCreate,
    ProjectDeploymentReleaseStatusUpdate,
    TenantDeploymentPlaneRead,
)
from orchestrator.core.deployment_github_events import (
    GitHubDeploymentReleaseRequest,
    create_deployment_releases_for_github_push,
)
from orchestrator.core.deployment_runtime import (
    _coolify_observation_for_release,
    _resolve_release_observation_transition,
    _select_release_for_event,
)
from orchestrator.core.deployment_setup.compose_normalizer import (
    CoolifyComposeNormalizationResult,
    normalize_compose_for_coolify,
)
from orchestrator.core.deployment_setup.planner import DeploymentPlannerResponse
from orchestrator.core.deployment_setup.start import project_deployment_setup_workflow_id
from orchestrator.temporal.activities.project_deployment_setup import (
    _create_initial_setup_release,
    _ensure_deployment_compose_artifact,
    _supersede_failed_deployment_setup_executions,
    _sync_npm_lockfiles_for_deployment_branch,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import (
    Project,
    ProjectApp,
    ProjectAppAnalysisRun,
    ProjectDeploymentRelease,
    Tenant,
    WorkflowExecution,
    WorkflowOperation,
)


def _run_git_for_test(args: list[str], *, cwd: Path) -> str:
    process = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    if process.returncode != 0:
        raise AssertionError((process.stderr or process.stdout or "git command failed").strip())
    return process.stdout


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

    def test_release_create_requires_branch_and_commit(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentReleaseCreate(git_ref="main")
        with self.assertRaises(ValidationError):
            ProjectDeploymentReleaseCreate(git_ref="main", commit_sha="")
        with self.assertRaises(ValidationError):
            ProjectDeploymentReleaseCreate(git_ref="main", commit_sha="not-a-sha")

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
                        "coolify_github_app_uuid": "github-app-1",
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
                        "domains": [{"key": "primary", "service_key": "web", "host": "web.apps.example.com"}],
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

    def test_successful_deployment_setup_supersedes_previous_failed_setup_executions(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                WorkflowExecution(
                    workflow_id="project_deployment_setup:old",
                    execution_id="old-execution",
                    workflow_type_key="project_deployment_setup",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    source_system="deployment_setup",
                    source_ref="project-1",
                    source_external_id="project_deployment_setup:old",
                    display_name="Old deployment setup",
                    source_description=None,
                    repo_url=None,
                    branch=None,
                    pr_url=None,
                    orchestration_backend="temporal",
                    dedupe_scope="deployment_setup",
                    status="failed",
                    last_error="previous failure",
                    active_run_id=None,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=now,
                    finished_at=now,
                    updated_at=now,
                )
            )
            session.add(
                WorkflowExecution(
                    workflow_id="project_deployment_setup:new",
                    execution_id="new-execution",
                    workflow_type_key="project_deployment_setup",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    source_system="deployment_setup",
                    source_ref="project-1",
                    source_external_id="project_deployment_setup:new",
                    display_name="New deployment setup",
                    source_description=None,
                    repo_url=None,
                    branch=None,
                    pr_url=None,
                    orchestration_backend="temporal",
                    dedupe_scope="deployment_setup",
                    status="running",
                    last_error=None,
                    active_run_id=None,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
            )
            session.commit()

            superseded = _supersede_failed_deployment_setup_executions(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                replacement_workflow_id="project_deployment_setup:new",
            )
            session.commit()

            old_workflow = session.get(WorkflowExecution, "project_deployment_setup:old")
            assert old_workflow is not None
            self.assertEqual(superseded, 1)
            self.assertEqual(old_workflow.status, "superseded")
            self.assertEqual(old_workflow.source_workflow_id, "project_deployment_setup:new")

    def test_deployment_config_write_separates_volumes_from_resources(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentConfigWrite.model_validate(
                {
                    "resources": [
                        {
                            "key": "app-data",
                            "kind": "volume",
                            "name": "app-data",
                            "config": {"mount_path": "/data"},
                        }
                    ]
                }
            )

        config = ProjectDeploymentConfigWrite.model_validate(
            {
                "resources": [{"key": "db", "kind": "postgres", "name": "Primary database"}],
                "volumes": [
                    {
                        "key": "app-data",
                        "type": "persistent",
                        "name": "App data",
                        "config": {"mount_path": "/data"},
                    }
                ],
            }
        )

        self.assertEqual(config.resources[0].key, "db")
        self.assertEqual(config.volumes[0].key, "app-data")

    def test_deployment_config_write_separates_services_from_resources(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentConfigWrite.model_validate(
                {
                    "resources": [
                        {
                            "key": "web",
                            "kind": "service",
                            "name": "Web",
                            "config": {"service_type": "website"},
                        }
                    ]
                }
            )

        config = ProjectDeploymentConfigWrite.model_validate(
            {
                "services": [
                    {
                        "key": "web",
                        "kind": "website",
                        "name": "Web",
                        "compose_service": "web",
                        "container_port": 3000,
                        "public": True,
                    }
                ],
            }
        )

        self.assertEqual(config.services[0].key, "web")
        self.assertEqual(config.services[0].kind, "website")

    def test_docker_compose_deployment_config_requires_committed_artifact_source(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentConfigWrite.model_validate({"source_strategy": "docker_compose"})

        config = ProjectDeploymentConfigWrite.model_validate(
            {
                "source_strategy": "docker_compose",
                "source_branch": "main",
                "source_commit_sha": "1d9c3852526117d9a71fedff2dd55164f6d45b20",
                "deployment_branch": "mb/deploy/project/main-1d9c38525261",
                "deployment_commit_sha": "c304d09f38ff8ac83ff7422cc25c517dc8ed1496",
                "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
            }
        )

        self.assertEqual(config.source_branch, "main")
        self.assertEqual(config.deployment_branch, "mb/deploy/project/main-1d9c38525261")
        self.assertEqual(config.deployment_compose_path, ".master-builder/deployments/docker-compose.yml")

    def test_deployment_volume_migration_splits_legacy_resource_volumes(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260509_0120_split_deployment_volumes.py"
        )
        spec = importlib.util.spec_from_file_location("split_deployment_volumes_migration", migration_path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        config, changed = migration._split_deployment_config(
            {
                "resources": [
                    {"key": "db", "kind": "postgres"},
                    {
                        "key": "app-data",
                        "kind": "volume",
                        "name": "app-data",
                        "config": {"mount_path": "/data"},
                    },
                ],
                "deployment_plan": {
                    "resources": [
                        {
                            "key": "search-data",
                            "kind": "persistent_volume",
                            "name": "search-data",
                            "config": {"mount_path": "/usr/share/elasticsearch/data"},
                        }
                    ]
                },
            }
        )

        self.assertTrue(changed)
        self.assertEqual(config["resources"], [{"key": "db", "kind": "postgres"}])
        self.assertEqual(config["volumes"][0]["key"], "app-data")
        self.assertEqual(config["deployment_plan"]["resources"], [])
        self.assertEqual(config["deployment_plan"]["volumes"][0]["key"], "search-data")

    def test_deployment_service_migration_splits_legacy_resource_services(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260509_0121_split_deployment_services.py"
        )
        spec = importlib.util.spec_from_file_location("split_deployment_services_migration", migration_path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        config, changed = migration._split_deployment_config(
            {
                "resources": [
                    {"key": "db", "kind": "postgres"},
                    {
                        "key": "web",
                        "kind": "service",
                        "name": "Web",
                        "config": {
                            "service_type": "website",
                            "compose_service": "web",
                            "source_path": "web/app",
                            "container_port": 3000,
                            "public": True,
                            "source": "deployment_planner",
                        },
                    },
                ],
                "deployment_plan": {
                    "resources": [
                        {
                            "key": "api",
                            "kind": "service",
                            "name": "API",
                            "config": {
                                "service_type": "api",
                                "compose_service": "api",
                                "container_port": 8080,
                            },
                        }
                    ],
                },
            }
        )

        self.assertTrue(changed)
        self.assertEqual(config["resources"], [{"key": "db", "kind": "postgres"}])
        self.assertEqual(config["services"][0]["key"], "web")
        self.assertEqual(config["services"][0]["kind"], "website")
        self.assertEqual(config["deployment_plan"]["resources"], [])
        self.assertEqual(config["deployment_plan"]["services"][0]["key"], "api")
        self.assertEqual(config["deployment_plan"]["services"][0]["kind"], "api")

    def test_deployment_service_cleanup_migration_removes_legacy_service_resource_kinds(self) -> None:
        migration_path = (
            Path(__file__).resolve().parents[1]
            / "orchestrator"
            / "storage"
            / "migrations"
            / "versions"
            / "20260509_0122_cleanup_legacy_service_resources.py"
        )
        spec = importlib.util.spec_from_file_location("cleanup_legacy_service_resources_migration", migration_path)
        assert spec is not None and spec.loader is not None
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)

        config, changed = migration._cleanup_deployment_config(
            {
                "resources": [
                    {
                        "key": "kafka",
                        "kind": "service",
                        "name": "kafka",
                        "config": {"service_type": "kafka", "compose_service": "kafka"},
                    },
                    {
                        "key": "dejavu",
                        "kind": "service",
                        "name": "dejavu",
                        "config": {"service_type": "admin_ui", "compose_service": "dejavu"},
                    },
                    {
                        "key": "web",
                        "kind": "service",
                        "name": "Web",
                        "config": {"service_type": "website", "compose_service": "web"},
                    },
                ]
            }
        )

        self.assertTrue(changed)
        self.assertEqual(config["resources"], [{"key": "kafka", "kind": "kafka", "name": "kafka", "config": {"compose_service": "kafka"}}])
        self.assertEqual(config["services"][0]["key"], "web")
        self.assertEqual(config["services"][0]["kind"], "website")
        self.assertEqual(config["services"][0]["compose_service"], "web")

    def test_deployment_config_write_accepts_environment_literals_and_secret_refs(self) -> None:
        config = ProjectDeploymentConfigWrite.model_validate(
            {
                "environment": {"NODE_ENV": "production"},
                "secret_refs": {"DATABASE_URL": "tenant/tenant-1/DATABASE_URL"},
            }
        )

        self.assertEqual(config.environment, {"NODE_ENV": "production"})
        self.assertEqual(config.secret_refs, {"DATABASE_URL": "tenant/tenant-1/DATABASE_URL"})

    def test_app_deployment_config_cannot_be_disabled(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentConfigWrite.model_validate({"enabled": False})

        read_config = ProjectDeploymentConfigRead.model_validate({"enabled": False, "environment_name": "production"})

        self.assertIs(read_config.enabled, True)

    def test_app_deployment_config_storage_does_not_persist_enabled_override(self) -> None:
        self._seed_tenant_project_app()
        service = self._admin_project_service()

        with self.session_factory() as session:
            service.update_project_app_deployment_config(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                app_id="app-1",
                payload=ProjectDeploymentConfigWrite(enabled=True, environment_name="production"),
            )

            app = session.get(ProjectApp, "app-1")
            assert app is not None
            self.assertNotIn("enabled", app.deployment_config)
            self.assertEqual(app.deployment_config["environment_name"], "production")

    def test_deployment_domains_are_service_bound(self) -> None:
        config = ProjectDeploymentConfigWrite.model_validate(
            {
                "domains": [
                    {
                        "key": "web-custom",
                        "service_key": "web",
                        "host": "web.example.com",
                    },
                    {
                        "key": "api-custom",
                        "service_key": "api",
                        "host": "api.example.com",
                    },
                ]
            }
        )

        self.assertEqual(config.domains[0].service_key, "web")
        self.assertEqual(config.domains[1].service_key, "api")

        with self.assertRaises(ValidationError):
            ProjectDeploymentConfigWrite.model_validate(
                {
                    "domains": [
                        {
                            "key": "legacy-domain",
                            "host": "web.example.com",
                        }
                    ]
                }
            )

    def test_project_deployment_policy_requires_production_branch(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentPolicyWrite.model_validate({"enabled": True})

        config = ProjectDeploymentPolicyWrite.model_validate(
            {
                "enabled": True,
                "production_branch": "refs/heads/main",
                "provider": "internal_coolify",
                "generated_domain_policy": "production",
                "branch_settings": {
                    "refs/heads/main": {
                        "environment": {"APP_MODE": "production"},
                        "secret_refs": {"DATABASE_URL": "tenant/tenant-1/DATABASE_URL"},
                    }
                },
                "resources": [{"key": "db-primary", "kind": "postgres", "name": "Primary database"}],
            }
        )

        self.assertEqual(config.production_branch, "main")
        self.assertEqual(config.branch_settings["main"].environment, {"APP_MODE": "production"})
        self.assertEqual(config.branch_settings["main"].secret_refs, {"DATABASE_URL": "tenant/tenant-1/DATABASE_URL"})
        self.assertEqual(config.resources[0].key, "db-primary")

    def test_project_deployment_policy_rejects_unsupported_preview_pr_events(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentPolicyWrite.model_validate(
                {
                    "enabled": True,
                    "production_branch": "main",
                    "preview_prs_enabled": True,
                }
            )

    def test_app_deployment_config_rejects_project_release_policy_fields(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentConfigWrite.model_validate(
                {
                    "auto_deploy_enabled": True,
                    "production_branch": "main",
                }
            )

    def test_project_deployment_policy_is_not_app_deployment_config(self) -> None:
        self._seed_tenant_project_app()
        service = self._admin_project_service()

        with self.session_factory() as session:
            service.update_project_deployment_policy(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                payload=ProjectDeploymentPolicyWrite(enabled=True, production_branch="main"),
            )
            service.update_project_app_deployment_config(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                app_id="app-1",
                payload=ProjectDeploymentConfigWrite(enabled=True, environment_name="production"),
            )

            project = session.get(Project, "project-1")
            app = session.get(ProjectApp, "app-1")
            assert project is not None
            assert app is not None
            self.assertEqual(
                project.deployment_config,
                {
                    "enabled": True,
                    "production_branch": "main",
                    "preview_prs_enabled": False,
                    "provider": "internal_coolify",
                    "generated_domain_policy": "production",
                    "branch_settings": {},
                    "services": [],
                    "resources": [],
                    "volumes": [],
                },
            )
            self.assertNotIn("production_branch", app.deployment_config)
            self.assertNotIn("auto_deploy_enabled", app.deployment_config)

    def test_complete_project_deployment_setup_saves_policy_and_starts_durable_workflow(self) -> None:
        self._seed_tenant_project_app()
        service = self._admin_project_service()

        captured: dict[str, object] = {}

        class _FakeTemporalClient:
            async def start_workflow(self, run_method, payload, **kwargs):  # noqa: ANN001
                captured["run_method"] = run_method
                captured["payload"] = payload
                captured["kwargs"] = kwargs

        async def _connect_temporal(_settings):  # noqa: ANN001
            return _FakeTemporalClient()

        with (
            self.session_factory() as session,
            patch("orchestrator.temporal.workflow_engine.connect_temporal_client", side_effect=_connect_temporal),
        ):
            result = service.complete_project_deployment_setup(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                payload=ProjectDeploymentPolicyWrite(enabled=True, production_branch="main"),
                requested_by_user_id="admin",
            )

            project = session.get(Project, "project-1")
            assert project is not None
            self.assertEqual(project.deployment_config["enabled"], True)
            self.assertEqual(project.deployment_config["production_branch"], "main")
            self.assertTrue(result.workflow_id.startswith("project_deployment_setup:"))
            self.assertLessEqual(len(result.workflow_id), 64)
            self.assertEqual(result.status, "started")
            workflow = session.get(WorkflowExecution, result.workflow_id)
            assert workflow is not None
            self.assertEqual(workflow.workflow_type_key, "project_deployment_setup")
            self.assertEqual(workflow.status, "queued")
            operations = session.execute(
                select(WorkflowOperation).where(WorkflowOperation.workflow_id == result.workflow_id)
            ).scalars().all()
            self.assertEqual(
                {operation.operation_type for operation in operations},
                {"repo_deployment_analysis", "deployment_configuration", "initial_release"},
            )
            setup_run = session.get(ProjectAppAnalysisRun, result.workflow_id)
            self.assertIsNone(setup_run)
            self.assertEqual(captured["payload"].workflow_id, result.workflow_id)

    def test_project_deployment_setup_workflow_id_is_unique_per_setup_run(self) -> None:
        first = project_deployment_setup_workflow_id(tenant_id="tenant-1", project_id="project-1")
        second = project_deployment_setup_workflow_id(tenant_id="tenant-1", project_id="project-1")

        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("project_deployment_setup:"))
        self.assertTrue(second.startswith("project_deployment_setup:"))
        self.assertLessEqual(len(first), 64)
        self.assertLessEqual(len(second), 64)

    def test_complete_project_deployment_setup_rejects_disabled_policy(self) -> None:
        self._seed_tenant_project_app()
        service = self._admin_project_service()

        with self.session_factory() as session:
            with self.assertRaises(HTTPException) as exc:
                service.complete_project_deployment_setup(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    payload=ProjectDeploymentPolicyWrite(enabled=False),
                    requested_by_user_id="admin",
                )

            self.assertEqual(exc.exception.status_code, status.HTTP_409_CONFLICT)

    def test_deployment_config_write_rejects_raw_secret_like_environment_values(self) -> None:
        with self.assertRaises(ValidationError):
            ProjectDeploymentConfigWrite.model_validate(
                {
                    "environment": {"DATABASE_PASSWORD": "raw-secret"},
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

    def test_deployment_environment_values_merge_project_and_app_config_and_resolve_secret_refs(self) -> None:
        now = datetime.now(timezone.utc)
        project = Project(
            project_id="project-1",
            tenant_id="tenant-1",
            name="Project 1",
            github_repository="https://github.com/example/repo",
            jira_project_key="TP",
            policy_overrides={},
            environment={"PROJECT_LEVEL": "enabled", "APP_MODE": "project-default"},
            secret_refs={"DATABASE_URL": "tenant/tenant-1/DATABASE_URL"},
            deployment_config={
                "enabled": True,
                "production_branch": "main",
                "branch_settings": {
                    "main": {
                        "environment": {"BRANCH_LEVEL": "enabled", "APP_MODE": "branch-default"},
                        "secret_refs": {"BRANCH_SECRET": "tenant/tenant-1/BRANCH_SECRET"},
                    }
                },
            },
            discord_config=None,
            is_archived=False,
            created_at=now,
            updated_at=now,
        )
        project_deployment = ProjectDeploymentConfigRead.model_validate(
            {
                "environment": {"APP_MODE": "app-override"},
                "secret_refs": {"API_TOKEN": "tenant/tenant-1/API_TOKEN"},
            }
        )

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.admin.deployment_release_service._resolve_secret_value",
                side_effect=lambda **kwargs: f"value-for-{kwargs['secret_ref'].split('/')[-1]}",
            ),
        ):
            values = build_deployment_environment_values(
                session=session,
                project=project,
                project_deployment=project_deployment,
                git_ref="main",
                encryption_key="test-key",
            )

        self.assertEqual(
            values,
            {
                "APP_MODE": "app-override",
                "BRANCH_LEVEL": "enabled",
                "PROJECT_LEVEL": "enabled",
                "API_TOKEN": "value-for-API_TOKEN",
                "BRANCH_SECRET": "value-for-BRANCH_SECRET",
                "DATABASE_URL": "value-for-DATABASE_URL",
            },
        )

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

    def test_listing_project_apps_returns_project_level_deployments_only(self) -> None:
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
            session.add_all(
                [
                    ProjectApp(
                        app_id="deployment-1",
                        tenant_id="tenant-1",
                        project_id="project-1",
                        name="Production",
                        slug="production",
                        source_path=".",
                        detection_confidence=1.0,
                        detected_runtime="compose",
                        detected_language=None,
                        analysis_source="deployment_setup",
                        build_strategy="docker_compose",
                        exposed_port=None,
                        healthcheck=None,
                        start_command=None,
                        env_schema_json={},
                        secret_schema_json={},
                        deployment_config={},
                        status="ready",
                        created_at=now,
                        updated_at=now,
                    ),
                    ProjectApp(
                        app_id="stale-folder-app",
                        tenant_id="tenant-1",
                        project_id="project-1",
                        name="docker",
                        slug="docker",
                        source_path="api/docker",
                        detection_confidence=1.0,
                        detected_runtime="compose",
                        detected_language=None,
                        analysis_source="deployment_setup",
                        build_strategy="docker_compose",
                        exposed_port=None,
                        healthcheck=None,
                        start_command=None,
                        env_schema_json={},
                        secret_schema_json={},
                        deployment_config={},
                        status="ready",
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            session.commit()

            page = self._admin_project_service().list_project_apps(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                limit=25,
                offset=0,
            )

            self.assertEqual([app.app_id for app in page], ["deployment-1"])

            with self.assertRaises(HTTPException) as exc:
                self._admin_project_service().get_project_app(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="stale-folder-app",
                )

            self.assertEqual(exc.exception.status_code, status.HTTP_404_NOT_FOUND)

    def test_delete_project_app_does_not_clear_project_deployment_policy(self) -> None:
        self._seed_tenant_project_app()
        with self.session_factory() as session:
            project = session.get(Project, "project-1")
            assert project is not None
            project.deployment_config = {"enabled": True, "production_branch": "main"}
            app = session.get(ProjectApp, "app-1")
            assert app is not None
            app.source_path = "."
            session.commit()

            self._admin_project_service().delete_project_app(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                app_id="app-1",
            )

            self.assertIsNone(session.get(ProjectApp, "app-1"))
            self.assertEqual(session.get(Project, "project-1").deployment_config, {"enabled": True, "production_branch": "main"})  # type: ignore[union-attr]

    def test_delete_project_app_rejects_active_deployment(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-active",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="app-1",
                    provider="internal_coolify",
                    status="deploying",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="main",
                    commit_sha="abcdef1",
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            with self.assertRaises(HTTPException) as raised:
                self._admin_project_service().delete_project_app(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="app-1",
                )

            self.assertEqual(raised.exception.status_code, status.HTTP_409_CONFLICT)
            self.assertIsNotNone(session.get(ProjectApp, "app-1"))

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
                    git_ref="main",
                    commit_sha="abcdef1",
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
                        payload=ProjectDeploymentReleaseCreate(app_id="app-1", git_ref="main", commit_sha="abcdef1"),
                        requested_by_user_id="admin",
                    )

            releases = session.query(ProjectDeploymentRelease).all()
            self.assertEqual(len(releases), 1)
            self.assertEqual(releases[0].status, "failed")
            self.assertEqual(releases[0].last_error, "provider down")

    def test_project_level_release_does_not_reuse_unscoped_legacy_provider_state(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                ProjectDeploymentRelease(
                    release_id="legacy-release",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id=None,
                    provider="internal_coolify",
                    status="live",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="main",
                    commit_sha="abcdef0",
                    requested_by_user_id=None,
                    deployment_snapshot={"app_id": "deleted-folder-app"},
                    provider_context={"service_uuid": "legacy-service-uuid"},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        captured: dict[str, object] = {}

        def fake_provider_submit(**kwargs: object) -> dict[str, object]:
            captured["existing_service_uuid"] = kwargs.get("existing_service_uuid")
            captured["existing_application_uuid"] = kwargs.get("existing_application_uuid")
            return {
                "application_uuid": "new-app-uuid",
                "deployment_uuid": "new-deployment-uuid",
                "route_bindings": [],
            }

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release",
                side_effect=fake_provider_submit,
            ),
        ):
            create_project_deployment_release(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                payload=ProjectDeploymentReleaseCreate(app_id="app-1", git_ref="main", commit_sha="abcdef1"),
                requested_by_user_id="admin",
            )

        self.assertIsNone(captured["existing_service_uuid"])
        self.assertIsNone(captured["existing_application_uuid"])

    def test_github_push_creates_release_for_matching_auto_deploy_policy(self) -> None:
        self._seed_tenant_project_app()
        with self.session_factory() as session:
            project = session.get(Project, "project-1")
            assert project is not None
            project.deployment_config = {"enabled": True, "production_branch": "main"}
            app = session.get(ProjectApp, "app-1")
            assert app is not None
            app.deployment_config = {
                "enabled": False,
                "environment_name": "production",
                "source_strategy": "dockerfile",
            }
            session.commit()

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release",
                return_value={
                    "application_uuid": "app-uuid",
                    "deployment_uuid": "deployment-uuid",
                    "route_bindings": [],
                },
            ),
        ):
            tenant = session.get(Tenant, "tenant-1")
            project = session.get(Project, "project-1")
            assert tenant is not None
            assert project is not None

            result = create_deployment_releases_for_github_push(
                session=session,
                tenant=tenant,
                project=project,
                request=GitHubDeploymentReleaseRequest(
                    branch="main",
                    commit_sha="abcdef1234567890",
                    delivery_id="delivery-1",
                ),
            )

            self.assertEqual(len(result.created_releases), 1)
            release = session.get(ProjectDeploymentRelease, result.created_releases[0].release_id)
            assert release is not None
            self.assertEqual(release.git_ref, "main")
            self.assertEqual(release.commit_sha, "abcdef1234567890")

    def test_manual_deployment_release_uses_route_app_scope(self) -> None:
        self._seed_tenant_project_app()
        service = self._admin_project_service()
        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release",
                return_value={
                    "application_uuid": "app-uuid",
                    "deployment_uuid": "deployment-uuid",
                    "route_bindings": [],
                },
            ),
        ):
            release = service.create_project_app_deployment_release(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                app_id="app-1",
                payload=ProjectDeploymentReleaseCreate(
                    app_id="wrong-app",
                    git_ref="main",
                    commit_sha="abcdef1234567890",
                    reason="Manual deploy",
                ),
                requested_by_user_id="admin",
            )

            self.assertEqual(release.app_id, "app-1")
            self.assertEqual(release.git_ref, "main")
            self.assertEqual(release.commit_sha, "abcdef1234567890")

    def test_github_push_releases_only_project_level_deployment_app(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            project = session.get(Project, "project-1")
            assert project is not None
            project.deployment_config = {"enabled": True, "production_branch": "main"}
            session.add(
                ProjectApp(
                    app_id="legacy-folder-app",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    name="api docker",
                    slug="api-docker",
                    source_path="api/docker",
                    detection_confidence=0.95,
                    detected_runtime="docker",
                    detected_language=None,
                    analysis_source="deployment_setup",
                    build_strategy="docker_compose",
                    exposed_port=None,
                    healthcheck=None,
                    start_command=None,
                    env_schema_json={},
                    secret_schema_json={},
                    deployment_config={
                        "enabled": True,
                        "environment_name": "production",
                        "source_strategy": "docker_compose",
                    },
                    status="ready",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release",
                return_value={
                    "application_uuid": "app-uuid",
                    "deployment_uuid": "deployment-uuid",
                    "route_bindings": [],
                },
            ),
        ):
            tenant = session.get(Tenant, "tenant-1")
            project = session.get(Project, "project-1")
            assert tenant is not None
            assert project is not None

            result = create_deployment_releases_for_github_push(
                session=session,
                tenant=tenant,
                project=project,
                request=GitHubDeploymentReleaseRequest(
                    branch="main",
                    commit_sha="abcdef1234567890",
                    delivery_id="delivery-1",
                ),
            )

            self.assertEqual([release.app_id for release in result.created_releases], ["app-1"])

    def test_github_push_does_not_create_release_for_non_matching_branch(self) -> None:
        self._seed_tenant_project_app()
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-1")
            project = session.get(Project, "project-1")
            assert tenant is not None
            assert project is not None
            project.deployment_config = {"enabled": True, "production_branch": "main"}
            session.commit()

            result = create_deployment_releases_for_github_push(
                session=session,
                tenant=tenant,
                project=project,
                request=GitHubDeploymentReleaseRequest(
                    branch="feature/test",
                    commit_sha="abcdef1234567890",
                    delivery_id="delivery-2",
                ),
            )

            self.assertEqual(result.created_releases, ())
            self.assertEqual(result.skipped_app_ids, ())

    def test_project_deployment_setup_initial_release_uses_generated_artifact_branch(self) -> None:
        self._seed_tenant_project_app()
        with self.session_factory() as session:
            project = session.get(Project, "project-1")
            app = session.get(ProjectApp, "app-1")
            assert project is not None
            assert app is not None
            project.deployment_config = {"enabled": True, "production_branch": "main"}
            app.deployment_config = {
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "docker_compose",
                "deployment_branch": "mb/deploy/project-1/main-abcdef123456",
                "deployment_commit_sha": "abcdef1234567890",
                "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
                "services": [
                    {
                        "key": "web",
                        "kind": "website",
                        "name": "Web",
                        "compose_service": "web",
                        "source_path": ".",
                        "container_port": 8000,
                    }
                ],
                "resources": [],
                "volumes": [],
            }
            session.commit()

        with (
            self.session_factory() as session,
            patch(
                "orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release",
                return_value={
                    "application_uuid": "app-uuid",
                    "deployment_uuid": "deployment-uuid",
                    "route_bindings": [],
                },
            ),
        ):
            tenant = session.get(Tenant, "tenant-1")
            project = session.get(Project, "project-1")
            app = session.get(ProjectApp, "app-1")
            assert tenant is not None
            assert project is not None
            assert app is not None

            release = _create_initial_setup_release(
                session=session,
                tenant=tenant,
                project=project,
                deployment_app=app,
                deployment_branch="mb/deploy/project-1/main-abcdef123456",
                deployment_commit_sha="abcdef1234567890",
                workflow_id="project_deployment_setup:test",
            )

            self.assertEqual(release.git_ref, "mb/deploy/project-1/main-abcdef123456")
            self.assertEqual(release.commit_sha, "abcdef1234567890")
            self.assertEqual(release.app_id, "app-1")

    def test_project_deployment_setup_syncs_npm_lockfiles_for_artifact_branch(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            service_dir = repo_dir / "web" / "admin"
            service_dir.mkdir(parents=True)
            (service_dir / "package.json").write_text('{"name":"admin","dependencies":{}}\n', encoding="utf-8")
            captured: list[tuple[tuple[str, ...], Path]] = []

            def fake_run_command(args: list[str], *, cwd: Path) -> str:
                captured.append((tuple(args), cwd))
                (cwd / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
                return ""

            lockfiles = _sync_npm_lockfiles_for_deployment_branch(
                repo_dir=repo_dir,
                npm_service_source_paths=("web/admin",),
                run_command_fn=fake_run_command,
            )

            self.assertEqual(lockfiles, ("web/admin/package-lock.json",))
            self.assertEqual(
                captured,
                [
                    (
                        ("npm", "install", "--package-lock-only", "--ignore-scripts", "--no-audit", "--no-fund"),
                        service_dir.resolve(),
                    )
                ],
            )

    def test_project_deployment_setup_commits_coolify_runtime_env_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            _run_git_for_test(["init"], cwd=repo_dir)
            _run_git_for_test(["config", "user.name", "Test Bot"], cwd=repo_dir)
            _run_git_for_test(["config", "user.email", "test@example.com"], cwd=repo_dir)
            _run_git_for_test(["checkout", "-b", "main"], cwd=repo_dir)
            (repo_dir / "README.md").write_text("source\n", encoding="utf-8")
            _run_git_for_test(["add", "README.md"], cwd=repo_dir)
            _run_git_for_test(["commit", "-m", "source"], cwd=repo_dir)
            source_commit = _run_git_for_test(["rev-parse", "HEAD"], cwd=repo_dir).strip()
            _run_git_for_test(["remote", "add", "origin", str(repo_dir)], cwd=repo_dir)

            branch, _commit, compose_path = _ensure_deployment_compose_artifact(
                repo_dir=repo_dir,
                project_id="project-1",
                source_branch="main",
                source_commit_sha=source_commit,
                compose_raw="services:\n  web:\n    image: example/web\n",
                npm_service_source_paths=(),
                token="token",
            )

            self.assertEqual(branch, f"mb/deploy/project-1/main-{source_commit[:12]}")
            self.assertEqual(compose_path, ".master-builder/deployments/docker-compose.yml")
            self.assertEqual(
                (repo_dir / ".env").read_text(encoding="utf-8"),
                "# Generated by Master Builder. Runtime values are managed by Coolify.\n",
            )
            staged_files = _run_git_for_test(["show", "--name-only", "--format=", "HEAD"], cwd=repo_dir)
            self.assertIn(".master-builder/deployments/docker-compose.yml", staged_files)
            self.assertIn(".env", staged_files)

    def test_project_deployment_setup_compose_does_not_embed_proxy_route_labels(self) -> None:
        self._seed_tenant_project_app()
        plan = DeploymentPlannerResponse.model_validate(
            {
                "deployment": {
                    "name": "production",
                    "services": [
                        {
                            "key": "app-web",
                            "name": "App Web",
                            "kind": "website",
                            "source_path": "web/app",
                            "build_strategy": "npm",
                            "compose_service": "app-web",
                            "container_port": 8080,
                            "depends_on": [],
                        }
                    ],
                    "routes": [{"service_key": "app-web", "visibility": "public"}],
                    "resources": [],
                    "volumes": [],
                    "compose_raw": (
                        "services:\n"
                        "  app-web:\n"
                        "    image: example/app-web\n"
                        "    expose:\n"
                        "      - '8080'\n"
                    ),
                }
            }
        ).deployment

        normalized = normalize_compose_for_coolify(plan.compose_raw).compose_raw

        self.assertNotIn("traefik.", normalized)
        self.assertNotIn("coolify:", normalized)

    def test_project_deployment_setup_rejects_npm_service_without_package_json(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            (repo_dir / "web" / "admin").mkdir(parents=True)

            with self.assertRaisesRegex(RuntimeError, "missing package.json"):
                _sync_npm_lockfiles_for_deployment_branch(
                    repo_dir=repo_dir,
                    npm_service_source_paths=("web/admin",),
                    run_command_fn=lambda args, *, cwd: "",
                )

    def test_deployment_release_rejects_managed_host_only_plane(self) -> None:
        self._seed_tenant_project_app()
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-1")
            assert tenant is not None
            tenant.deployment_plane_config = {
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "apps.example.com",
                "platform_subdomain": "coolify",
                "managed_host_id": "host-1",
                "state": "active",
            }
            app = session.get(ProjectApp, "app-1")
            assert app is not None
            app.name = "development"
            app.slug = "development"
            app.source_path = "api/docker"
            app.build_strategy = "docker_compose"
            app.deployment_config = {
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "docker_compose",
                "environment": {},
                "secret_refs": {},
                "domains": [],
                "resources": [],
                "backup_policies": [],
            }
            session.commit()

            with self.assertRaises(HTTPException) as raised:
                create_project_deployment_release(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    payload=ProjectDeploymentReleaseCreate(app_id="app-1", git_ref="main", commit_sha="abcdef1"),
                    requested_by_user_id="admin",
                )
            self.assertEqual(raised.exception.status_code, status.HTTP_409_CONFLICT)
            self.assertIn("Tenant deployment plane is missing Coolify configuration fields", str(raised.exception.detail))
            self.assertEqual(session.query(ProjectDeploymentRelease).count(), 0)

    def test_internal_coolify_release_submits_docker_compose_app(self) -> None:
        class FakeCoolifyClient:
            def __init__(self) -> None:
                self.application_payload: dict[str, object] | None = None
                self.env_payload: dict[str, object] | None = None
                self.started_application_uuid: str | None = None

            def create_private_github_app_application(self, *, payload: dict[str, object]) -> str:
                self.application_payload = payload
                return "application-1"

            def bulk_update_application_envs(self, *, application_uuid: str, payload: dict[str, object]) -> dict[str, object]:
                self.env_payload = payload
                return {"application_uuid": application_uuid, **payload}

            def start_application(self, *, application_uuid: str) -> str:
                self.started_application_uuid = application_uuid
                return "deployment-1"

            def get_application(self, *, application_uuid: str) -> dict[str, object]:
                return {"uuid": application_uuid}

        fake_client = FakeCoolifyClient()
        now = datetime.now(timezone.utc)
        tenant = Tenant(
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
        project = Project(
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
        project_app = ProjectApp(
            app_id="app-1",
            tenant_id="tenant-1",
            project_id="project-1",
            name="development",
            slug="development",
            source_path=".",
            detection_confidence=1.0,
            detected_runtime="docker",
            detected_language=None,
            analysis_source="test",
            build_strategy="docker_compose",
            exposed_port=None,
            healthcheck=None,
            start_command=None,
            env_schema_json={},
            secret_schema_json={},
            deployment_config={"deployment_compose_path": ".master-builder/deployments/docker-compose.yml"},
            status="ready",
            created_at=now,
            updated_at=now,
        )
        tenant_plane = TenantDeploymentPlaneRead.model_validate(
            {
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "bsktpay-2.localhost:8088",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "coolify-project-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-1",
                "coolify_destination_uuid": "destination-1",
                "coolify_github_app_uuid": "github-app-1",
                "secret_refs": {"coolify_api_token": "platform/COOLIFY_API_TOKEN"},
                "state": "active",
            }
        )
        project_deployment = ProjectDeploymentConfigRead.model_validate(
            {
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "docker_compose",
                "domains": [],
                "services": [
                    {
                        "key": "web",
                        "kind": "website",
                        "name": "Web",
                        "compose_service": "web",
                        "container_port": 3000,
                        "public": True,
                    }
                ],
                "resources": [],
                "backup_policies": [],
            }
        )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.admin.deployment_release_service._resolve_secret_value", return_value="token"),
            patch(
                "orchestrator.api.admin.deployment_release_service._load_normalized_compose_for_release",
                return_value=CoolifyComposeNormalizationResult(
                    compose_raw="services:\n  web:\n    image: example/web\n    expose:\n      - '3000'\n",
                    exposed_ports_by_service={"web": ["3000"]},
                ),
            ),
            patch("orchestrator.api.admin.deployment_release_service.CoolifyApiClient", return_value=fake_client),
        ):
            result = submit_internal_coolify_release(
                session=session,
                tenant=tenant,
                project=project,
                project_app=project_app,
                tenant_plane=tenant_plane,
                project_deployment=project_deployment,
                payload=ProjectDeploymentReleaseCreate(git_ref="main", commit_sha="abcdef1"),
                existing_application_uuid=None,
                existing_service_uuid=None,
            )

        self.assertEqual(result["application_uuid"], "application-1")
        self.assertEqual(result["deployment_uuid"], "deployment-1")
        self.assertEqual(result["coolify_resource_type"], "application")
        self.assertEqual(
            result["route_bindings"][0]["host"],
            "web.development.main.project-1.tenant-1.bsktpay-2.localhost",
        )
        self.assertEqual(result["route_bindings"][0]["proxy_port"], 8088)
        self.assertEqual(result["route_bindings"][0]["service_key"], "web")
        assert fake_client.application_payload is not None
        self.assertEqual(fake_client.application_payload["name"], "development")
        self.assertEqual(fake_client.application_payload["build_pack"], "dockercompose")
        self.assertEqual(
            fake_client.application_payload["docker_compose_location"],
            "/.master-builder/deployments/docker-compose.yml",
        )
        self.assertEqual(
            fake_client.application_payload["docker_compose_domains"],
            [{"name": "web", "domain": "http://web.development.main.project-1.tenant-1.bsktpay-2.localhost"}],
        )
        self.assertNotIn("docker_compose_raw", fake_client.application_payload)
        self.assertEqual(fake_client.started_application_uuid, "application-1")

    def test_release_read_derives_generated_service_url_from_route_binding(self) -> None:
        now = datetime.now(timezone.utc)
        release = ProjectDeploymentRelease(
            release_id="release-1",
            tenant_id="tenant-1",
            project_id="project-1",
            app_id="app-1",
            provider="internal_coolify",
            status="live",
            environment_name="production",
            source_strategy="docker_compose",
            git_ref="main",
            commit_sha="abcdef1",
            requested_by_user_id=None,
            deployment_snapshot={},
            provider_context={
                "route_bindings": [
                    {
                        "service_key": "web",
                        "service_name": "Web",
                        "service_kind": "website",
                        "scheme": "http",
                        "host": "web.development.main.project-1.tenant-1.bsktpay-2.localhost",
                        "proxy_port": 8088,
                        "url_kind": "generated",
                    }
                ]
            },
            requested_at=now,
            started_at=now,
            completed_at=now,
            created_at=now,
            updated_at=now,
        )

        service_urls = _release_service_urls(release)

        self.assertEqual(
            service_urls[0].url,
            "http://web.development.main.project-1.tenant-1.bsktpay-2.localhost:8088",
        )
        self.assertEqual(service_urls[0].host, "web.development.main.project-1.tenant-1.bsktpay-2.localhost")
        self.assertEqual(service_urls[0].proxy_port, 8088)

    def test_release_route_verification_fails_provider_miss_without_marking_live(self) -> None:
        now = datetime.now(timezone.utc)
        release = ProjectDeploymentRelease(
            release_id="release-1",
            tenant_id="tenant-1",
            project_id="project-1",
            app_id="app-1",
            provider="internal_coolify",
            status="route_activating",
            environment_name="production",
            source_strategy="docker_compose",
            git_ref="main",
            commit_sha="abcdef1",
            requested_by_user_id=None,
            deployment_snapshot={},
            provider_context={
                "route_bindings": [
                    {
                        "service_key": "web",
                        "service_name": "Web",
                        "service_kind": "website",
                        "scheme": "http",
                        "host": "web.development.main.project-1.tenant-1.bsktpay-2.localhost",
                        "proxy_port": 8088,
                        "url_kind": "generated",
                    }
                ]
            },
            requested_at=now,
            started_at=now,
            completed_at=None,
            created_at=now,
            updated_at=now,
        )

        result = verify_release_route_bindings(release, fetch_url_fn=lambda _url: "provider route not active")

        self.assertFalse(result.ok)
        self.assertIn("provider route not active", result.error or "")

    def test_release_route_verification_allows_application_http_response(self) -> None:
        now = datetime.now(timezone.utc)
        release = ProjectDeploymentRelease(
            release_id="release-1",
            tenant_id="tenant-1",
            project_id="project-1",
            app_id="app-1",
            provider="internal_coolify",
            status="route_activating",
            environment_name="production",
            source_strategy="docker_compose",
            git_ref="main",
            commit_sha="abcdef1",
            requested_by_user_id=None,
            deployment_snapshot={},
            provider_context={
                "route_bindings": [
                    {
                        "service_key": "api",
                        "service_name": "API",
                        "service_kind": "api",
                        "scheme": "http",
                        "host": "api.development.main.project-1.tenant-1.bsktpay-2.localhost",
                        "proxy_port": 8088,
                        "url_kind": "generated",
                    }
                ]
            },
            requested_at=now,
            started_at=now,
            completed_at=None,
            created_at=now,
            updated_at=now,
        )

        result = verify_release_route_bindings(release, fetch_url_fn=lambda _url: None)

        self.assertTrue(result.ok)

    def test_docker_compose_generated_routes_require_explicit_public_service(self) -> None:
        now = datetime.now(timezone.utc)
        tenant = Tenant(
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
        project = Project(
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
        project_app = ProjectApp(
            app_id="app-1",
            tenant_id="tenant-1",
            project_id="project-1",
            name="production",
            slug="production",
            source_path=".",
            detection_confidence=1.0,
            detected_runtime="compose",
            detected_language=None,
            analysis_source="deployment_setup",
            build_strategy="docker_compose",
            exposed_port=None,
            healthcheck=None,
            start_command=None,
            env_schema_json={},
            secret_schema_json={},
            deployment_config={},
            status="ready",
            created_at=now,
            updated_at=now,
        )
        tenant_plane = TenantDeploymentPlaneRead.model_validate(
            {
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "bsktpay-2.localhost:8088",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "coolify-project-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-1",
                "coolify_destination_uuid": "destination-1",
                "coolify_github_app_uuid": "github-app-1",
                "secret_refs": {"coolify_api_token": "platform/COOLIFY_API_TOKEN"},
                "state": "active",
            }
        )
        project_deployment = ProjectDeploymentConfigRead.model_validate(
            {
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "docker_compose",
                "domains": [],
                "services": [
                    {
                        "key": "web",
                        "kind": "website",
                        "name": "Web",
                        "compose_service": "web",
                        "container_port": 3000,
                        "public": True,
                    },
                    {
                        "key": "dejavu",
                        "kind": "website",
                        "name": "dejavu",
                        "compose_service": "dejavu",
                        "container_port": 1358,
                        "public": False,
                    },
                ],
                "resources": [],
                "backup_policies": [],
            }
        )

        routes, ports, route_bindings = _coolify_docker_compose_routes(
            tenant=tenant,
            project=project,
            project_app=project_app,
            tenant_plane=tenant_plane,
            project_deployment=project_deployment,
            git_ref="main",
            exposed_ports_by_service={"web": ["3000"], "dejavu": ["1358"]},
        )

        self.assertEqual(set(routes), {"web"})
        self.assertEqual(set(ports), {"web"})
        self.assertEqual([route_binding["service_key"] for route_binding in route_bindings], ["web"])

    def test_coolify_generated_routes_are_provider_domains_not_compose_labels(self) -> None:
        now = datetime.now(timezone.utc)
        tenant = Tenant(
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
        project = Project(
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
        project_app = ProjectApp(
            app_id="app-1",
            tenant_id="tenant-1",
            project_id="project-1",
            name="production",
            slug="production",
            source_path=".",
            detection_confidence=1.0,
            detected_runtime="compose",
            detected_language=None,
            analysis_source="deployment_setup",
            build_strategy="docker_compose",
            exposed_port=None,
            healthcheck=None,
            start_command=None,
            env_schema_json={},
            secret_schema_json={},
            deployment_config={},
            status="ready",
            created_at=now,
            updated_at=now,
        )
        tenant_plane = TenantDeploymentPlaneRead.model_validate(
            {
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "region": "eu-west",
                "base_domain": "bsktpay-2.localhost:8088",
                "platform_subdomain": "builder",
                "api_base_url": "https://builder.apps.example.com/api/v1",
                "coolify_project_uuid": "coolify-project-1",
                "coolify_environment_name": "production",
                "coolify_server_uuid": "server-1",
                "coolify_destination_uuid": "destination-1",
                "coolify_github_app_uuid": "github-app-1",
                "secret_refs": {"coolify_api_token": "platform/COOLIFY_API_TOKEN"},
                "state": "active",
            }
        )
        project_deployment = ProjectDeploymentConfigRead.model_validate(
            {
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "docker_compose",
                "domains": [],
                "services": [
                    {
                        "key": "customer-api",
                        "kind": "api",
                        "name": "Customer API",
                        "compose_service": "customer-api",
                        "container_port": 8080,
                        "public": True,
                    }
                ],
                "resources": [],
                "backup_policies": [],
            }
        )

        routes, ports, route_bindings = _coolify_docker_compose_routes(
            tenant=tenant,
            project=project,
            project_app=project_app,
            tenant_plane=tenant_plane,
            project_deployment=project_deployment,
            git_ref="main",
            exposed_ports_by_service={"customer-api": ["8080"]},
        )

        self.assertEqual(
            routes["customer-api"],
            "http://customer-api.production.main.project-1.tenant-1.bsktpay-2.localhost",
        )
        self.assertEqual(ports["customer-api"], "8080")
        self.assertEqual(route_bindings[0]["host"], "customer-api.production.main.project-1.tenant-1.bsktpay-2.localhost")
        self.assertEqual(route_bindings[0]["proxy_port"], 8088)

    def test_internal_coolify_release_restarts_existing_docker_compose_application(self) -> None:
        class FakeCoolifyClient:
            def __init__(self) -> None:
                self.application_patch_payload: dict[str, object] | None = None
                self.started_application_uuid: str | None = None

            def update_application(self, *, application_uuid: str, payload: dict[str, object]) -> dict[str, object]:
                self.application_patch_payload = payload
                return {"application_uuid": application_uuid, **payload}

            def bulk_update_application_envs(self, *, application_uuid: str, payload: dict[str, object]) -> dict[str, object]:
                return {"application_uuid": application_uuid, **payload}

            def start_application(self, *, application_uuid: str) -> str:
                self.started_application_uuid = application_uuid
                return "deployment-2"

            def get_application(self, *, application_uuid: str) -> dict[str, object]:
                return {"uuid": application_uuid}

        now = datetime.now(timezone.utc)
        tenant = Tenant(
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
        project = Project(
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
        project_app = ProjectApp(
            app_id="app-1",
            tenant_id="tenant-1",
            project_id="project-1",
            name="development",
            slug="development",
            source_path=".",
            detection_confidence=1.0,
            detected_runtime="docker",
            detected_language=None,
            analysis_source="test",
            build_strategy="docker_compose",
            exposed_port=None,
            healthcheck=None,
            start_command=None,
            env_schema_json={},
            secret_schema_json={},
            deployment_config={"deployment_compose_path": ".master-builder/deployments/docker-compose.yml"},
            status="ready",
            created_at=now,
            updated_at=now,
        )
        tenant_plane = TenantDeploymentPlaneRead.model_validate(
            {
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
                "coolify_github_app_uuid": "github-app-1",
                "secret_refs": {"coolify_api_token": "platform/COOLIFY_API_TOKEN"},
                "state": "active",
            }
        )
        project_deployment = ProjectDeploymentConfigRead.model_validate(
            {
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "docker_compose",
                "domains": [],
                "services": [
                    {
                        "key": "web",
                        "kind": "website",
                        "name": "Web",
                        "compose_service": "web",
                        "container_port": 3000,
                        "public": True,
                    }
                ],
                "resources": [],
                "backup_policies": [],
            }
        )
        fake_client = FakeCoolifyClient()

        with (
            self.session_factory() as session,
            patch("orchestrator.api.admin.deployment_release_service._resolve_secret_value", return_value="token"),
            patch(
                "orchestrator.api.admin.deployment_release_service._load_normalized_compose_for_release",
                return_value=CoolifyComposeNormalizationResult(
                    compose_raw="services:\n  web:\n    image: example/web\n    expose:\n      - '3000'\n",
                    exposed_ports_by_service={"web": ["3000"]},
                ),
            ),
            patch("orchestrator.api.admin.deployment_release_service.CoolifyApiClient", return_value=fake_client),
        ):
            result = submit_internal_coolify_release(
                session=session,
                tenant=tenant,
                project=project,
                project_app=project_app,
                tenant_plane=tenant_plane,
                project_deployment=project_deployment,
                payload=ProjectDeploymentReleaseCreate(git_ref="main", commit_sha="abcdef1"),
                existing_application_uuid="application-existing",
                existing_service_uuid=None,
            )

        self.assertEqual(result["application_uuid"], "application-existing")
        self.assertEqual(result["deployment_uuid"], "deployment-2")
        self.assertEqual(fake_client.started_application_uuid, "application-existing")
        assert fake_client.application_patch_payload is not None
        self.assertEqual(fake_client.application_patch_payload["build_pack"], "dockercompose")

    def test_release_reconciliation_observes_coolify_service_status(self) -> None:
        class FakeCoolifyClient:
            def get_service(self, *, service_uuid: str) -> dict[str, object]:
                self.service_uuid = service_uuid
                return {"uuid": service_uuid, "status": "running:unknown"}

        now = datetime.now(timezone.utc)
        release = ProjectDeploymentRelease(
            release_id="release-1",
            tenant_id="tenant-1",
            project_id="project-1",
            app_id="app-1",
            provider="internal_coolify",
            status="provisioning",
            environment_name="production",
            source_strategy="docker_compose",
            git_ref="main",
            commit_sha="abcdef1",
            provider_context={"service_uuid": "service-1"},
            deployment_snapshot={},
            created_at=now,
            updated_at=now,
        )

        client = FakeCoolifyClient()
        observation = _coolify_observation_for_release(client=client, release=release)

        assert observation is not None
        self.assertEqual(client.service_uuid, "service-1")
        self.assertEqual(observation.status, "running:unknown")

    def test_release_reconciliation_does_not_store_success_logs_as_error(self) -> None:
        class FakeCoolifyClient:
            def get_deployment(self, *, deployment_uuid: str) -> dict[str, object]:
                self.deployment_uuid = deployment_uuid
                return {
                    "deployment_uuid": deployment_uuid,
                    "status": "finished",
                    "application": {"uuid": "application-1", "status": "running:unknown"},
                    "logs": "successful build logs are not an error",
                }

        now = datetime.now(timezone.utc)
        release = ProjectDeploymentRelease(
            release_id="release-1",
            tenant_id="tenant-1",
            project_id="project-1",
            app_id="app-1",
            provider="internal_coolify",
            status="provisioning",
            environment_name="production",
            source_strategy="docker_compose",
            git_ref="main",
            commit_sha="abcdef1",
            provider_context={"deployment_uuid": "deployment-1"},
            deployment_snapshot={},
            requested_at=now,
            created_at=now,
            updated_at=now,
        )

        client = FakeCoolifyClient()
        observation = _coolify_observation_for_release(client=client, release=release)

        assert observation is not None
        self.assertEqual(client.deployment_uuid, "deployment-1")
        self.assertEqual(observation.status, "finished")
        self.assertIsNone(observation.last_error)

    def test_release_reconciliation_does_not_mark_in_progress_coolify_release_live(self) -> None:
        next_status = _resolve_release_observation_transition(
            current_status="deploying",
            observed_status="in_progress",
            observed_application_status="exited:unhealthy",
        )

        self.assertEqual(next_status, "deploying")

    def test_release_reconciliation_requires_successful_deployment_for_live(self) -> None:
        next_status = _resolve_release_observation_transition(
            current_status="deploying",
            observed_status="in_progress",
            observed_application_status="running:healthy",
        )

        self.assertEqual(next_status, "deploying")

        completed_status = _resolve_release_observation_transition(
            current_status="deploying",
            observed_status="success",
            observed_application_status="running:healthy",
        )

        self.assertEqual(completed_status, "live")

        finished_status = _resolve_release_observation_transition(
            current_status="deploying",
            observed_status="finished",
            observed_application_status="running:unknown",
        )

        self.assertEqual(finished_status, "live")

    def test_release_status_update_allows_provider_terminal_jump_from_provisioning(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            release = ProjectDeploymentRelease(
                release_id="release-terminal-jump",
                tenant_id="tenant-1",
                project_id="project-1",
                app_id="app-1",
                provider="internal_coolify",
                status="provisioning",
                environment_name="production",
                source_strategy="docker_compose",
                git_ref="main",
                commit_sha="abcdef1",
                provider_context={"deployment_uuid": "deployment-1"},
                deployment_snapshot={},
                requested_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add(release)
            session.commit()

            updated = update_project_deployment_release_status(
                session=session,
                tenant_id="tenant-1",
                project_id="project-1",
                app_id="app-1",
                release_id="release-terminal-jump",
                payload=ProjectDeploymentReleaseStatusUpdate(
                    status="live",
                    deployment_uuid="deployment-1",
                    last_error=None,
                ),
            )

            self.assertEqual(updated.status, "live")
            persisted = session.get(ProjectDeploymentRelease, "release-terminal-jump")
            assert persisted is not None
            self.assertIsNotNone(persisted.completed_at)
