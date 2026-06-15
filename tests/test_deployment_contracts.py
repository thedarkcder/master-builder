from __future__ import annotations

import importlib.util
import os
import subprocess
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select

from orchestrator.api.admin.project_service import AdminProjectService
from orchestrator.api.admin.deployment_release_service import (
    _coolify_build_pack,
    _coolify_docker_compose_routes,
    _fetch_route_activation,
    _normalize_local_coolify_api_base_url,
    _release_service_urls,
    build_deployment_environment_values,
    create_project_deployment_release,
    destroy_project_deployment_preview_release,
    get_project_deployment_release_logs,
    list_project_deployment_releases,
    submit_internal_coolify_release,
    update_project_deployment_release_status,
    verify_release_route_bindings,
)
from orchestrator.api.admin.runs.service import create_run_preview_admin
from orchestrator.api.deployment_schemas import (
    ProjectDeploymentConfigRead,
    ProjectDeploymentConfigWrite,
    ProjectDeploymentPolicyWrite,
    ProjectDeploymentServiceUrlRead,
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
from orchestrator.core.deployment_previews import (
    create_run_preview_deployment,
    destroy_run_preview_deployments_for_pr,
)
from orchestrator.core.deployment_runtime import (
    CoolifyDeploymentObservation,
    _coolify_api_base_url as _runtime_coolify_api_base_url,
    _coolify_observation_for_release,
    _resolve_release_observation_transition,
    _select_release_for_event,
    reconcile_deployment_release,
)
from orchestrator.core.deployment_setup.compose_normalizer import (
    CoolifyComposeNormalizationResult,
    normalize_compose_for_coolify,
)
from orchestrator.core.deployment_setup.artifacts import (
    ensure_deployment_compose_artifact as _ensure_deployment_compose_artifact,
    validate_npm_lockfiles_for_deployment_branch as _validate_npm_lockfiles_for_deployment_branch,
)
from orchestrator.core.deployment_setup.planner import DeploymentPlannerResponse
from orchestrator.core.deployment_setup.start import project_deployment_setup_workflow_id
from orchestrator.temporal.activities.project_deployment_setup import (
    _create_initial_setup_release,
    _supersede_failed_deployment_setup_executions,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import (
    DeploymentHost,
    DeploymentHostCommand,
    Project,
    ProjectApp,
    ProjectAppAnalysisRun,
    ProjectDeploymentRelease,
    Run,
    Tenant,
    WorkflowExecutionArtifact,
    WorkflowExecution,
    WorkflowOperation,
)
from orchestrator.tools.coolify_api import CoolifyApiError


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

    def test_project_deployment_release_logs_read_from_coolify_deployment(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            release = ProjectDeploymentRelease(
                release_id="release-logs-1",
                tenant_id="tenant-1",
                project_id="project-1",
                app_id="app-1",
                provider="internal_coolify",
                release_kind="production",
                status="deploying",
                environment_name="production",
                source_strategy="docker_compose",
                git_ref="main",
                commit_sha="abcdef1",
                requested_by_user_id=None,
                deployment_snapshot={},
                provider_context={
                    "deployment_uuid": "deployment-1",
                    "application_uuid": "application-1",
                },
                delivery_metadata={},
                last_error=None,
                requested_at=now,
                started_at=now,
                completed_at=None,
                destroyed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(release)
            session.commit()

            class FakeCoolifyClient:
                def get_deployment(self, *, deployment_uuid: str) -> dict[str, object]:
                    self.deployment_uuid = deployment_uuid
                    return {
                        "deployment_uuid": deployment_uuid,
                        "application_id": "application-1",
                        "status": "running",
                        "logs": "Pulling image\nStarting container",
                    }

            fake_client = FakeCoolifyClient()
            with patch(
                "orchestrator.api.admin.deployment_release_service._coolify_client_for_release",
                return_value=fake_client,
            ):
                logs = get_project_deployment_release_logs(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="app-1",
                    release_id="release-logs-1",
                )

        self.assertEqual(logs.deployment_uuid, "deployment-1")
        self.assertEqual(logs.application_uuid, "application-1")
        self.assertEqual(logs.status, "running")
        self.assertEqual(logs.logs, "Pulling image\nStarting container")
        self.assertFalse(logs.truncated)
        self.assertEqual(fake_client.deployment_uuid, "deployment-1")

    def test_project_deployment_release_logs_falls_back_when_deployment_uuid_is_stale(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            release = ProjectDeploymentRelease(
                release_id="release-logs-stale",
                tenant_id="tenant-1",
                project_id="project-1",
                app_id="app-1",
                provider="internal_coolify",
                release_kind="run_preview",
                status="deploying",
                environment_name="production",
                source_strategy="docker_compose",
                git_ref="feature/ap-293",
                commit_sha="abcdef2",
                requested_by_user_id=None,
                deployment_snapshot={},
                provider_context={
                    "deployment_uuid": "stale-deployment",
                    "application_uuid": "application-1",
                },
                delivery_metadata={},
                last_error=None,
                requested_at=now,
                started_at=now,
                completed_at=None,
                destroyed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(release)
            session.commit()

            class FakeCoolifyClient:
                def get_deployment(self, *, deployment_uuid: str) -> dict[str, object]:
                    self.deployment_uuid = deployment_uuid
                    raise CoolifyApiError(
                        "Coolify API request failed (404) for GET /deployments/stale-deployment: Not Found",
                        status_code=404,
                        method="GET",
                        path=f"/deployments/{deployment_uuid}",
                        body="Not Found",
                    )

                def list_application_deployments(self, *, application_uuid: str, take: int = 1) -> list[dict[str, object]]:
                    self.application_uuid = application_uuid
                    self.take = take
                    return [
                        {
                            "deployment_uuid": "latest-deployment",
                            "application_id": application_uuid,
                            "status": "running",
                            "logs": "Fallback deployment logs",
                        }
                    ]

            fake_client = FakeCoolifyClient()
            with patch(
                "orchestrator.api.admin.deployment_release_service._coolify_client_for_release",
                return_value=fake_client,
            ):
                logs = get_project_deployment_release_logs(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="app-1",
                    release_id="release-logs-stale",
                )

        self.assertEqual(fake_client.deployment_uuid, "stale-deployment")
        self.assertEqual(fake_client.application_uuid, "application-1")
        self.assertEqual(fake_client.take, 1)
        self.assertEqual(logs.deployment_uuid, "latest-deployment")
        self.assertEqual(logs.application_uuid, "application-1")
        self.assertEqual(logs.status, "running")
        self.assertEqual(logs.logs, "Fallback deployment logs")

    def _seed_preview_run(self, *, session, now: datetime, commit_sha: str):  # noqa: ANN001
        tenant = Tenant(
            tenant_id="tenant-preview",
            name="Tenant Preview",
            is_enabled=True,
            jira_config={},
            github_config={},
            repos_config={},
            policy_config={},
            discord_config=None,
            deployment_plane_config={},
            created_at=now,
            updated_at=now,
        )
        project = Project(
            project_id="project-preview",
            tenant_id=tenant.tenant_id,
            name="Project Preview",
            github_repository="https://github.com/example/repo",
            jira_project_key="TP",
            policy_overrides={},
            environment={},
            secret_refs={},
            discord_config=None,
            deployment_config={
                "enabled": True,
                "production_branch": "main",
                "preview_prs_enabled": True,
            },
            is_archived=False,
            created_at=now,
            updated_at=now,
        )
        workflow = WorkflowExecution(
            workflow_id="workflow-preview-1",
            execution_id="execution-preview-1",
            workflow_type_key="development_team_run",
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            source_system="jira",
            source_ref="AP-123",
            source_external_id="AP-123",
            display_name="AP-123",
            source_description=None,
            repo_url=project.github_repository,
            branch="feature/AP-123",
            pr_url=None,
            orchestration_backend="temporal",
            dedupe_scope="issue_execution",
            status="running",
            last_error=None,
            active_run_id="run-1",
            latest_checkpoint_id=None,
            source_workflow_id=None,
            source_run_id=None,
            created_at=now,
            started_at=now,
            finished_at=None,
            updated_at=now,
        )
        run = Run(
            run_id="run-1",
            workflow_id=workflow.workflow_id,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            issue_key="AP-123",
            issue_summary="Preview issue",
            issue_description="Preview issue description",
            repo_url=project.github_repository,
            branch="feature/AP-123",
            pr_url=None,
            attempt_number=1,
            parent_run_id=None,
            entry_mode="fresh",
            entry_stage="pm",
            entry_checkpoint_id=None,
            dedupe_scope="issue_execution",
            status="running",
            last_error=None,
            pre_check_outcome=None,
            required_worker_capability=None,
            required_runtime_kinds_json=[],
            claim_id=None,
            plan={},
            created_at=now,
            dispatch_claimed_at=None,
            started_at=now,
            last_heartbeat_at=now,
            worker_service_instance_id=None,
            finished_at=None,
        )
        app = ProjectApp(
            app_id="app-1",
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            name="Web",
            slug="web",
            source_path=".",
            detection_confidence=1.0,
            detected_runtime="node",
            detected_language="typescript",
            analysis_source="deployment_setup",
            build_strategy="dockerfile",
            exposed_port=3000,
            healthcheck="/health",
            start_command=None,
            env_schema_json={},
            secret_schema_json={},
            deployment_config={
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "dockerfile",
                "resources": [],
                "services": [],
                "volumes": [],
            },
            status="ready",
            created_at=now,
            updated_at=now,
        )
        artifact = WorkflowExecutionArtifact(
            artifact_id="artifact-1",
            tenant_id=tenant.tenant_id,
            project_id=project.project_id,
            workflow_id=workflow.workflow_id,
            run_id=run.run_id,
            artifact_kind="execution_branch",
            status="pushed",
            repo_url=project.github_repository,
            branch="run/ap-123/run-1",
            commit_sha=commit_sha,
            diff_stat_json={},
            pushed_at=now,
            created_at=now,
            updated_at=now,
        )
        session.add_all([tenant, project, workflow, run, app, artifact])
        session.flush()
        return tenant, project, run

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

    def test_run_preview_deployment_uses_pushed_execution_artifact_and_marks_mobile_delivery(self) -> None:
        now = datetime.now(timezone.utc)
        commit_sha = "a" * 40
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha=commit_sha)
            session.add(
                ProjectApp(
                    app_id="mobile-1",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    name="iOS App",
                    slug="ios-app",
                    source_path="apps/ios",
                    detection_confidence=1.0,
                    detected_runtime="ios",
                    detected_language="swift",
                    analysis_source="test",
                    build_strategy=None,
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
            )
            session.commit()

            captured: dict[str, object] = {}

            def fake_create_release(**kwargs):
                payload = kwargs["payload"]
                captured["payload"] = payload
                return SimpleNamespace(release_id="release-preview-1")

            with (
                patch("orchestrator.core.deployment_previews.create_project_deployment_release", side_effect=fake_create_release),
                patch(
                    "orchestrator.core.deployment_previews.destroy_project_deployment_preview_release",
                    return_value=SimpleNamespace(release_id="release-existing"),
                ),
            ):
                result = create_run_preview_deployment(
                    session=session,
                    tenant=tenant,
                    project=project,
                    run=run,
                    settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                    pr_url="https://github.com/example/repo/pull/12",
                )

        payload = captured["payload"]
        self.assertTrue(result.created)
        self.assertEqual(payload.release_kind, "run_preview")
        self.assertEqual(payload.git_ref, "run/ap-123/run-1")
        self.assertEqual(payload.commit_sha, commit_sha)
        self.assertEqual(payload.source_run_id, "run-1")
        self.assertEqual(payload.pr_number, 12)
        self.assertEqual(payload.delivery_metadata["mobile_delivery"]["status"], "pending_fastlane_distribution")

    def test_run_preview_generation_is_idempotent_for_existing_run_preview(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            existing = ProjectDeploymentRelease(
                release_id="release-existing",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                app_id="app-1",
                provider="internal_coolify",
                release_kind="run_preview",
                status="queued",
                environment_name="production",
                source_strategy="dockerfile",
                git_ref="run/ap-123/run-1",
                commit_sha="a" * 40,
                source_run_id=run.run_id,
                pr_number=12,
                requested_by_user_id=None,
                deployment_snapshot={},
                provider_context={},
                delivery_metadata={},
                last_error=None,
                requested_at=now,
                started_at=None,
                completed_at=None,
                destroyed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(existing)
            session.commit()

            with patch("orchestrator.core.deployment_previews.create_project_deployment_release") as create_release:
                result = create_run_preview_deployment(
                    session=session,
                    tenant=tenant,
                    project=project,
                    run=run,
                    settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                    pr_url="https://github.com/example/repo/pull/12",
                )

        self.assertFalse(result.created)
        self.assertEqual(result.reason, "existing")
        self.assertEqual(result.release.release_id, "release-existing")
        create_release.assert_not_called()

    def test_run_preview_generation_reuses_inflight_preview_with_pending_provider_route(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            tenant.deployment_plane_config = {"base_domain": "192-168-0-118.sslip.io:8088"}
            existing = ProjectDeploymentRelease(
                release_id="release-existing",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                app_id="app-1",
                provider="internal_coolify",
                release_kind="run_preview",
                status="provisioning",
                environment_name="production",
                source_strategy="nixpacks",
                git_ref="feature/CAP-8",
                commit_sha="a" * 40,
                source_run_id=run.run_id,
                pr_number=12,
                requested_by_user_id=None,
                deployment_snapshot={},
                provider_context={
                    "base_domain": "192-168-0-118.sslip.io:8088",
                    "application_uuid": "app-existing",
                    "deployment_uuid": "deployment-existing",
                    "route_bindings": [
                        {
                            "service_key": "repo",
                            "service_name": "repo",
                            "service_kind": "website",
                            "scheme": "http",
                            "host": "app-existing.master-builder-coolify-ssh-host.sslip.io",
                            "url_kind": "generated",
                            "status": "pending",
                        }
                    ],
                },
                delivery_metadata={},
                last_error=None,
                requested_at=now,
                started_at=now,
                completed_at=None,
                destroyed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(existing)
            session.commit()

            def fake_reconcile(*, session, release):  # noqa: ANN001
                session.add(release)
                return False

            with (
                patch("orchestrator.core.deployment_previews.reconcile_deployment_release", side_effect=fake_reconcile),
                patch("orchestrator.core.deployment_previews.create_project_deployment_release") as create_release,
                patch("orchestrator.core.deployment_previews.destroy_project_deployment_preview_release") as destroy_release,
            ):
                result = create_run_preview_deployment(
                    session=session,
                    tenant=tenant,
                    project=project,
                    run=run,
                    settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                    pr_url="https://github.com/example/repo/pull/12",
                )

        self.assertFalse(result.created)
        self.assertEqual(result.reason, "existing")
        self.assertEqual(result.release.release_id, "release-existing")
        create_release.assert_not_called()
        destroy_release.assert_not_called()

    def test_run_preview_generation_recreates_when_existing_base_domain_is_stale(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            tenant.deployment_plane_config = {"base_domain": "192-168-0-118.sslip.io:8088"}
            existing = ProjectDeploymentRelease(
                release_id="release-existing",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                app_id="app-1",
                provider="internal_coolify",
                release_kind="run_preview",
                status="live",
                environment_name="production",
                source_strategy="dockerfile",
                git_ref="run/ap-123/run-1",
                commit_sha="a" * 40,
                source_run_id=run.run_id,
                pr_number=12,
                requested_by_user_id=None,
                deployment_snapshot={},
                provider_context={"base_domain": "bsktpay-2.localhost:8088", "application_uuid": "old-app"},
                delivery_metadata={},
                last_error=None,
                requested_at=now,
                started_at=now,
                completed_at=now,
                destroyed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(existing)
            session.commit()

            def fake_create_release(**kwargs):
                payload = kwargs["payload"]
                self.assertEqual(payload.source_run_id, "run-1")
                return SimpleNamespace(release_id="release-new")

            with (
                patch("orchestrator.core.deployment_previews.create_project_deployment_release", side_effect=fake_create_release),
                patch(
                    "orchestrator.core.deployment_previews.destroy_project_deployment_preview_release",
                    return_value=SimpleNamespace(release_id="release-existing"),
                ),
            ):
                result = create_run_preview_deployment(
                    session=session,
                    tenant=tenant,
                    project=project,
                    run=run,
                    settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                    pr_url="https://github.com/example/repo/pull/12",
                )

        self.assertTrue(result.created)
        self.assertEqual(result.release.release_id, "release-new")

    def test_run_preview_generation_recreates_when_existing_route_scheme_is_stale(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            tenant.deployment_plane_config = {"base_domain": "192-168-0-118.sslip.io:8088"}
            existing = ProjectDeploymentRelease(
                release_id="release-existing",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                app_id="app-1",
                provider="internal_coolify",
                release_kind="run_preview",
                status="live",
                environment_name="production",
                source_strategy="dockerfile",
                git_ref="run/ap-123/run-1",
                commit_sha="a" * 40,
                source_run_id=run.run_id,
                pr_number=12,
                requested_by_user_id=None,
                deployment_snapshot={},
                provider_context={
                    "base_domain": "192-168-0-118.sslip.io:8088",
                    "application_uuid": "old-app",
                    "route_bindings": [
                        {
                            "service_key": "web",
                            "service_name": "Web",
                            "service_kind": "website",
                            "scheme": "https",
                            "host": "web.production.run.align.192-168-0-118.sslip.io",
                            "proxy_port": 8088,
                            "url_kind": "generated",
                        }
                    ],
                },
                delivery_metadata={},
                last_error=None,
                requested_at=now,
                started_at=now,
                completed_at=now,
                destroyed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(existing)
            session.commit()

            def fake_create_release(**kwargs):
                payload = kwargs["payload"]
                self.assertEqual(payload.source_run_id, "run-1")
                return SimpleNamespace(release_id="release-new")

            with (
                patch("orchestrator.core.deployment_previews.create_project_deployment_release", side_effect=fake_create_release),
                patch(
                    "orchestrator.core.deployment_previews.destroy_project_deployment_preview_release",
                    return_value=SimpleNamespace(release_id="release-existing"),
                ),
            ):
                result = create_run_preview_deployment(
                    session=session,
                    tenant=tenant,
                    project=project,
                    run=run,
                    settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                    pr_url="https://github.com/example/repo/pull/12",
                )

        self.assertTrue(result.created)
        self.assertEqual(result.release.release_id, "release-new")

    def test_run_preview_generation_retires_existing_active_preview_before_force_regenerate(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-existing",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="live",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="run/ap-123/run-1",
                    commit_sha="a" * 40,
                    source_run_id=run.run_id,
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={"base_domain": "bsktpay-2.localhost:8088", "application_uuid": "old-app"},
                    delivery_metadata={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            with patch(
                "orchestrator.core.deployment_previews.destroy_project_deployment_preview_release",
                return_value=SimpleNamespace(release_id="release-existing"),
            ) as destroy_mock:
                with patch(
                    "orchestrator.core.deployment_previews.create_project_deployment_release",
                    return_value=SimpleNamespace(release_id="release-new"),
                ) as create_release:
                    result = create_run_preview_deployment(
                        session=session,
                        tenant=tenant,
                        project=project,
                        run=run,
                        settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                        pr_url="https://github.com/example/repo/pull/12",
                        force=True,
                    )

        self.assertTrue(result.created)
        self.assertEqual(result.release.release_id, "release-new")
        destroy_mock.assert_called_once()
        create_release.assert_called_once()

    def test_run_preview_generation_retires_stale_active_preview_before_recreate(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            tenant.deployment_plane_config = {"base_domain": "192-168-0-118.sslip.io:8088"}
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-existing",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="live",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="run/ap-123/run-1",
                    commit_sha="a" * 40,
                    source_run_id=run.run_id,
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={
                        "base_domain": "192-168-0-118.sslip.io:8088",
                        "application_uuid": "old-app",
                        "route_bindings": [
                            {
                                "service_key": "web",
                                "service_name": "Web",
                                "service_kind": "website",
                                "scheme": "https",
                                "host": "web.production.run.align.192-168-0-118.sslip.io",
                                "proxy_port": 8088,
                                "url_kind": "generated",
                            }
                        ],
                    },
                    delivery_metadata={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            with patch(
                "orchestrator.core.deployment_previews.destroy_project_deployment_preview_release",
                return_value=SimpleNamespace(release_id="release-existing"),
            ) as destroy_mock:
                with patch(
                    "orchestrator.core.deployment_previews.create_project_deployment_release",
                    return_value=SimpleNamespace(release_id="release-new"),
                ) as create_release:
                    result = create_run_preview_deployment(
                        session=session,
                        tenant=tenant,
                        project=project,
                        run=run,
                        settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                        pr_url="https://github.com/example/repo/pull/12",
                    )

        self.assertTrue(result.created)
        self.assertEqual(result.release.release_id, "release-new")
        destroy_mock.assert_called_once()
        create_release.assert_called_once()

    def test_run_preview_generation_force_bypasses_existing_preview(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            existing = ProjectDeploymentRelease(
                release_id="release-existing",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                app_id="app-1",
                provider="internal_coolify",
                release_kind="run_preview",
                status="live",
                environment_name="production",
                source_strategy="dockerfile",
                git_ref="run/ap-123/run-1",
                commit_sha="a" * 40,
                source_run_id=run.run_id,
                pr_number=12,
                requested_by_user_id=None,
                deployment_snapshot={},
                provider_context={"base_domain": "bsktpay-2.localhost:8088", "application_uuid": "old-app"},
                delivery_metadata={},
                last_error=None,
                requested_at=now,
                started_at=now,
                completed_at=now,
                destroyed_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(existing)
            session.commit()

            with patch(
                "orchestrator.core.deployment_previews.destroy_project_deployment_preview_release",
                return_value=SimpleNamespace(release_id="release-existing"),
            ) as destroy_mock:
                with patch(
                    "orchestrator.core.deployment_previews.create_project_deployment_release",
                    return_value=SimpleNamespace(release_id="release-new"),
                ) as create_release:
                    result = create_run_preview_deployment(
                        session=session,
                        tenant=tenant,
                        project=project,
                        run=run,
                        settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                        pr_url="https://github.com/example/repo/pull/12",
                        force=True,
                    )

        self.assertTrue(result.created)
        self.assertEqual(result.release.release_id, "release-new")
        destroy_mock.assert_called_once()
        create_release.assert_called_once()

    def test_run_preview_release_does_not_reuse_coolify_app_from_stale_base_domain(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        captured: dict[str, object] = {}
        with self.session_factory() as session:
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-old-domain",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="live",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="run/ap-123/run-1",
                    commit_sha="a" * 40,
                    source_run_id="run-1",
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={"base_domain": "bsktpay-2.localhost:8088", "application_uuid": "old-app"},
                    delivery_metadata={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            def fake_submit(**kwargs):
                captured["existing_application_uuid"] = kwargs["existing_application_uuid"]
                return {
                    "api_base_url": "https://builder.apps.example.com/api/v1",
                    "application_uuid": "new-app",
                    "deployment_uuid": "deployment-new",
                    "git_branch": "run/ap-123/run-1",
                    "environment_name": "production",
                    "route_bindings": [],
                }

            with patch("orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release", side_effect=fake_submit):
                release = create_project_deployment_release(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    payload=ProjectDeploymentReleaseCreate(
                        app_id="app-1",
                        release_kind="run_preview",
                        git_ref="run/ap-123/run-1",
                        commit_sha="b" * 40,
                        source_run_id="run-1",
                    ),
                    requested_by_user_id=None,
                )

        self.assertIsNone(captured["existing_application_uuid"])
        self.assertEqual(release.provider_context["application_uuid"], "new-app")

    def test_run_preview_release_does_not_reuse_coolify_app_from_stale_route_scheme(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        captured: dict[str, object] = {}
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-1")
            assert tenant is not None
            tenant.deployment_plane_config = {
                **dict(tenant.deployment_plane_config or {}),
                "base_domain": "192-168-0-118.sslip.io:8088",
            }
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-stale-scheme",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="live",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="run/ap-123/run-1",
                    commit_sha="a" * 40,
                    source_run_id="run-1",
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={
                        "base_domain": "192-168-0-118.sslip.io:8088",
                        "application_uuid": "old-app",
                        "route_bindings": [
                            {
                                "service_key": "web",
                                "service_name": "Web",
                                "service_kind": "website",
                                "scheme": "https",
                                "host": "web.production.run.align.192-168-0-118.sslip.io",
                                "proxy_port": 8088,
                                "url_kind": "generated",
                            }
                        ],
                    },
                    delivery_metadata={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            def fake_submit(**kwargs):
                captured["existing_application_uuid"] = kwargs["existing_application_uuid"]
                return {
                    "api_base_url": "https://builder.apps.example.com/api/v1",
                    "application_uuid": "new-app",
                    "deployment_uuid": "deployment-new",
                    "git_branch": "run/ap-123/run-1",
                    "environment_name": "production",
                    "route_bindings": [],
                }

            with patch("orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release", side_effect=fake_submit):
                release = create_project_deployment_release(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    payload=ProjectDeploymentReleaseCreate(
                        app_id="app-1",
                        release_kind="run_preview",
                        git_ref="run/ap-123/run-1",
                        commit_sha="b" * 40,
                        source_run_id="run-1",
                    ),
                    requested_by_user_id=None,
                )

        self.assertIsNone(captured["existing_application_uuid"])
        self.assertEqual(release.provider_context["application_uuid"], "new-app")

    def test_run_preview_release_reuses_coolify_app_for_current_route_scheme(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        captured: dict[str, object] = {}
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-1")
            assert tenant is not None
            tenant.deployment_plane_config = {
                **dict(tenant.deployment_plane_config or {}),
                "base_domain": "192-168-0-118.sslip.io:8088",
            }
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-current-preview",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="live",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="run/ap-123/run-1",
                    commit_sha="a" * 40,
                    source_run_id="run-1",
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={
                        "base_domain": "192-168-0-118.sslip.io:8088",
                        "application_uuid": "old-app",
                        "route_bindings": [
                            {
                                "service_key": "web",
                                "service_name": "Web",
                                "service_kind": "website",
                                "scheme": "http",
                                "host": "web-production-run-ap-123-run-1.align.192-168-0-118.sslip.io",
                                "proxy_port": 8088,
                                "internal_url": "http://host.docker.internal:8088",
                                "url_kind": "generated",
                            }
                        ],
                    },
                    delivery_metadata={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            def fake_submit(**kwargs):
                captured["existing_application_uuid"] = kwargs["existing_application_uuid"]
                return {
                    "api_base_url": "https://builder.apps.example.com/api/v1",
                    "application_uuid": "old-app",
                    "deployment_uuid": "deployment-new",
                    "git_branch": "run/ap-123/run-1",
                    "environment_name": "production",
                    "route_bindings": [],
                }

            with patch("orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release", side_effect=fake_submit):
                release = create_project_deployment_release(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    payload=ProjectDeploymentReleaseCreate(
                        app_id="app-1",
                        release_kind="run_preview",
                        git_ref="run/ap-123/run-1",
                        commit_sha="b" * 40,
                        source_run_id="run-1",
                    ),
                    requested_by_user_id=None,
                )

        self.assertEqual(captured["existing_application_uuid"], "old-app")
        self.assertEqual(release.provider_context["application_uuid"], "old-app")

    def test_destroy_run_preview_release_for_replacement_retains_existing_coolify_app(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-preview",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="live",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="run/ap-123/run-1",
                    commit_sha="a" * 40,
                    source_run_id="run-1",
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={
                        "base_domain": "apps.example.com",
                        "application_uuid": "old-app",
                    },
                    delivery_metadata={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            with patch("orchestrator.api.admin.deployment_release_service.CoolifyApiClient") as coolify_client_cls:
                with patch(
                    "orchestrator.api.admin.deployment_release_service.ensure_local_preview_route_cleanup_command"
                ) as cleanup_mock:
                    updated = destroy_project_deployment_preview_release(
                        session=session,
                        tenant_id="tenant-1",
                        project_id="project-1",
                        release_id="release-preview",
                        reason="preview_replaced",
                    )

        coolify_client_cls.assert_not_called()
        cleanup_mock.assert_not_called()
        self.assertEqual(updated.status, "destroyed")
        self.assertTrue(updated.provider_context["application_retained_for_replacement"])

    def test_run_preview_generation_retries_after_failed_preview_release(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-failed",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="failed",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="run/ap-123/run-1",
                    commit_sha="a" * 40,
                    source_run_id=run.run_id,
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={},
                    delivery_metadata={},
                    last_error="Coolify API is unreachable",
                    requested_at=now,
                    started_at=None,
                    completed_at=now,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            def fake_create_release(**kwargs):
                payload = kwargs["payload"]
                self.assertEqual(payload.source_run_id, "run-1")
                return SimpleNamespace(release_id="release-new")

            with patch("orchestrator.core.deployment_previews.create_project_deployment_release", side_effect=fake_create_release):
                result = create_run_preview_deployment(
                    session=session,
                    tenant=tenant,
                    project=project,
                    run=run,
                    settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                    pr_url="https://github.com/example/repo/pull/12",
                )

        self.assertTrue(result.created)
        self.assertEqual(result.release.release_id, "release-new")

    def test_run_preview_generation_stops_after_failed_preview_retry_limit(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            for index in range(2):
                session.add(
                    ProjectDeploymentRelease(
                        release_id=f"release-failed-{index}",
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                        app_id="app-1",
                        provider="internal_coolify",
                        release_kind="run_preview",
                        status="failed",
                        environment_name="production",
                        source_strategy="dockerfile",
                        git_ref="run/ap-123/run-1",
                        commit_sha="a" * 40,
                        source_run_id=run.run_id,
                        pr_number=12,
                        requested_by_user_id=None,
                        deployment_snapshot={},
                        provider_context={},
                        delivery_metadata={},
                        last_error="Coolify deployment failed",
                        requested_at=now + timedelta(seconds=index),
                        started_at=now + timedelta(seconds=index),
                        completed_at=now + timedelta(seconds=index),
                        destroyed_at=None,
                        created_at=now + timedelta(seconds=index),
                        updated_at=now + timedelta(seconds=index),
                    )
                )
            session.commit()

            with patch("orchestrator.core.deployment_previews.create_project_deployment_release") as create_release:
                result = create_run_preview_deployment(
                    session=session,
                    tenant=tenant,
                    project=project,
                    run=run,
                    settings=SimpleNamespace(
                        project_repo_checkout_base_dir="/tmp/unused",
                        secrets_encryption_key="unused",
                        qa_demo_max_attempts=2,
                    ),
                    pr_url="https://github.com/example/repo/pull/12",
                )

        create_release.assert_not_called()
        self.assertFalse(result.created)
        self.assertEqual(result.reason, "preview_failed_retry_limit_reached")
        self.assertEqual(result.release.release_id, "release-failed-1")
        self.assertEqual(result.release.status, "failed")

    def test_run_preview_generation_force_bypasses_failed_preview_retry_limit(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            for index in range(2):
                session.add(
                    ProjectDeploymentRelease(
                        release_id=f"release-failed-force-{index}",
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                        app_id="app-1",
                        provider="internal_coolify",
                        release_kind="run_preview",
                        status="failed",
                        environment_name="production",
                        source_strategy="dockerfile",
                        git_ref="run/ap-123/run-1",
                        commit_sha="a" * 40,
                        source_run_id=run.run_id,
                        pr_number=12,
                        requested_by_user_id=None,
                        deployment_snapshot={},
                        provider_context={},
                        delivery_metadata={},
                        last_error="Coolify deployment failed",
                        requested_at=now + timedelta(seconds=index),
                        started_at=now + timedelta(seconds=index),
                        completed_at=now + timedelta(seconds=index),
                        destroyed_at=None,
                        created_at=now + timedelta(seconds=index),
                        updated_at=now + timedelta(seconds=index),
                    )
                )
            session.commit()

            with patch(
                "orchestrator.core.deployment_previews.create_project_deployment_release",
                return_value=SimpleNamespace(release_id="release-new-after-force"),
            ) as create_release:
                result = create_run_preview_deployment(
                    session=session,
                    tenant=tenant,
                    project=project,
                    run=run,
                    settings=SimpleNamespace(
                        project_repo_checkout_base_dir="/tmp/unused",
                        secrets_encryption_key="unused",
                        qa_demo_max_attempts=2,
                    ),
                    pr_url="https://github.com/example/repo/pull/12",
                    force=True,
                )

        create_release.assert_called_once()
        self.assertTrue(result.created)
        self.assertEqual(result.release.release_id, "release-new-after-force")

    def test_run_preview_generation_reconciles_inflight_preview_before_reuse(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-stale",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="provisioning",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="run/ap-123/run-1",
                    commit_sha="a" * 40,
                    source_run_id=run.run_id,
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={"deployment_uuid": "deployment-failed"},
                    delivery_metadata={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=None,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            def fake_reconcile(*, session, release):  # noqa: ANN001
                release.status = "failed"
                release.last_error = "Coolify deployment failed"
                session.add(release)
                return True

            def fake_create_release(**kwargs):
                payload = kwargs["payload"]
                self.assertEqual(payload.source_run_id, "run-1")
                return SimpleNamespace(release_id="release-new")

            with (
                patch("orchestrator.core.deployment_previews.reconcile_deployment_release", side_effect=fake_reconcile),
                patch("orchestrator.core.deployment_previews.create_project_deployment_release", side_effect=fake_create_release),
            ):
                result = create_run_preview_deployment(
                    session=session,
                    tenant=tenant,
                    project=project,
                    run=run,
                    settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                    pr_url="https://github.com/example/repo/pull/12",
                )

        self.assertTrue(result.created)
        self.assertEqual(result.release.release_id, "release-new")

    def test_admin_run_preview_generation_requires_succeeded_run(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            self._seed_preview_run(session=session, now=now, commit_sha="a" * 40)
            session.commit()

            with self.assertRaises(HTTPException) as raised:
                create_run_preview_admin(
                    session=session,
                    run_id="run-1",
                    run_model=Run,
                    tenant_model=Tenant,
                    project_model=Project,
                    settings=SimpleNamespace(project_repo_checkout_base_dir="/tmp/unused", secrets_encryption_key="unused"),
                )

        self.assertEqual(raised.exception.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(raised.exception.detail, "Preview generation requires a succeeded run")

    def test_run_preview_release_uses_config_override_without_mutating_app_config(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        original_branch = "mb/deploy/project-1/main-aaaaaaaaaaaa"
        preview_branch = "mb/deploy/project-1/run-preview-bbbbbbbbbbbb"
        with self.session_factory() as session:
            app = session.get(ProjectApp, "app-1")
            assert app is not None
            app.build_strategy = "docker_compose"
            app.deployment_config = {
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "docker_compose",
                "source_branch": "main",
                "source_commit_sha": "a" * 40,
                "deployment_branch": original_branch,
                "deployment_commit_sha": "a" * 40,
                "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
                "generated_compose_raw": "services:\n  web:\n    image: nginx\n    ports:\n      - '80:80'\n",
                "domains": [],
                "services": [
                    {
                        "key": "web",
                        "kind": "website",
                        "name": "Web",
                        "compose_service": "web",
                        "public": True,
                        "container_port": 80,
                    }
                ],
                "resources": [],
                "volumes": [],
                "backup_policies": [],
            }
            app.updated_at = now
            session.commit()

            captured: dict[str, object] = {}

            def fake_submit(**kwargs):
                captured["project_deployment"] = kwargs["project_deployment"]
                return {"application_uuid": "coolify-app-1", "deployment_uuid": "deployment-1"}

            with patch(
                "orchestrator.api.admin.deployment_release_service.submit_internal_coolify_release",
                side_effect=fake_submit,
            ):
                release = create_project_deployment_release(
                    session=session,
                    tenant_id="tenant-1",
                    project_id="project-1",
                    payload=ProjectDeploymentReleaseCreate(
                        app_id="app-1",
                        release_kind="run_preview",
                        git_ref=preview_branch,
                        commit_sha="b" * 40,
                        source_run_id="run-preview-1",
                        pr_number=31,
                    ),
                    requested_by_user_id=None,
                    deployment_config_override={
                        "deployment_branch": preview_branch,
                        "deployment_commit_sha": "b" * 40,
                        "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
                    },
                )

            session.refresh(app)

        submitted_config = captured["project_deployment"]
        self.assertEqual(release.status, "provisioning")
        self.assertEqual(submitted_config.deployment_branch, preview_branch)
        self.assertEqual(submitted_config.deployment_commit_sha, "b" * 40)
        self.assertEqual(app.deployment_config["deployment_branch"], original_branch)
        self.assertEqual(app.status, "ready")

    def test_pr_merge_cleanup_destroys_matching_preview_releases_only(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, _run = self._seed_preview_run(session=session, now=now, commit_sha="b" * 40)
            session.add(
                ProjectDeploymentRelease(
                    release_id="preview-release-1",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="live",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="run/ap-123/run-1",
                    commit_sha="b" * 40,
                    source_run_id="run-1",
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={"application_uuid": "coolify-app-1"},
                    delivery_metadata={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=now,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            with patch(
                "orchestrator.core.deployment_previews.destroy_project_deployment_preview_release",
                return_value=SimpleNamespace(release_id="preview-release-1"),
            ) as destroy_mock:
                result = destroy_run_preview_deployments_for_pr(
                    session=session,
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    pr_number=12,
                    reason="pr_merged",
                )

        self.assertEqual(result.destroyed_release_ids, ("preview-release-1",))
        destroy_mock.assert_called_once()

    def test_preview_release_read_includes_source_ticket_context(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant, project, run = self._seed_preview_run(session=session, now=now, commit_sha="b" * 40)
            session.add(
                ProjectDeploymentRelease(
                    release_id="preview-release-ticket-context",
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    app_id="app-1",
                    provider="internal_coolify",
                    release_kind="run_preview",
                    status="route_activating",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="mb/deploy/project-preview/feature-ap-123-abcdef12",
                    commit_sha="b" * 40,
                    source_run_id=run.run_id,
                    pr_number=12,
                    requested_by_user_id=None,
                    deployment_snapshot={},
                    provider_context={},
                    delivery_metadata={},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=None,
                    destroyed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

            releases = list_project_deployment_releases(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                app_id="app-1",
            )

        preview = next(release for release in releases if release.release_id == "preview-release-ticket-context")
        self.assertEqual(preview.source_run_id, run.run_id)
        self.assertEqual(preview.source_issue_key, "AP-123")
        self.assertEqual(preview.source_issue_summary, "Preview issue")

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

    def test_nixpacks_deployment_config_maps_to_coolify_build_pack(self) -> None:
        config = ProjectDeploymentConfigWrite.model_validate(
            {
                "environment_name": "production",
                "source_strategy": "nixpacks",
            }
        )

        self.assertEqual(config.source_strategy, "nixpacks")
        self.assertEqual(ProjectDeploymentConfigRead.model_validate(config.model_dump()).source_strategy, "nixpacks")
        self.assertEqual(_coolify_build_pack(config.source_strategy), "nixpacks")

    def test_host_worker_rewrites_local_coolify_api_url(self) -> None:
        with patch("orchestrator.api.admin.deployment_release_service.Path.exists", return_value=False):
            self.assertEqual(
                _normalize_local_coolify_api_base_url("http://host.docker.internal:8000/api/v1"),
                "http://localhost:8000/api/v1",
            )

        with patch("orchestrator.api.admin.deployment_release_service.Path.exists", return_value=True):
            self.assertEqual(
                _normalize_local_coolify_api_base_url("http://host.docker.internal:8000/api/v1"),
                "http://host.docker.internal:8000/api/v1",
            )

    def test_deployment_reconciliation_rewrites_local_coolify_api_url(self) -> None:
        tenant_plane = TenantDeploymentPlaneRead.model_validate(
            {
                "provider": "internal_coolify",
                "state": "active",
                "api_base_url": "http://host.docker.internal:8000/api/v1",
            }
        )

        with patch("orchestrator.api.admin.deployment_release_service.Path.exists", return_value=False):
            self.assertEqual(
                _runtime_coolify_api_base_url(tenant_plane=tenant_plane),
                "http://localhost:8000/api/v1",
            )

        with patch("orchestrator.api.admin.deployment_release_service.Path.exists", return_value=True):
            self.assertEqual(
                _runtime_coolify_api_base_url(tenant_plane=tenant_plane),
                "http://host.docker.internal:8000/api/v1",
            )

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

    def test_project_deployment_policy_accepts_preview_pr_events(self) -> None:
        config = ProjectDeploymentPolicyWrite.model_validate(
            {
                "enabled": True,
                "production_branch": "main",
                "preview_prs_enabled": True,
            }
        )

        self.assertTrue(config.preview_prs_enabled)

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

    def test_release_retry_reuses_prior_provider_resource_when_latest_attempt_failed_before_submission(self) -> None:
        self._seed_tenant_project_app()
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add_all(
                [
                    ProjectDeploymentRelease(
                        release_id="release-live",
                        tenant_id="tenant-1",
                        project_id="project-1",
                        app_id="app-1",
                        provider="internal_coolify",
                        release_kind="production",
                        status="live",
                        environment_name="production",
                        source_strategy="dockerfile",
                        git_ref="main",
                        commit_sha="abcdef0",
                        requested_by_user_id=None,
                        deployment_snapshot={},
                        provider_context={"application_uuid": "application-existing"},
                        delivery_metadata={},
                        last_error=None,
                        requested_at=now,
                        started_at=now,
                        completed_at=now,
                        destroyed_at=None,
                        created_at=now,
                        updated_at=now,
                    ),
                    ProjectDeploymentRelease(
                        release_id="release-failed",
                        tenant_id="tenant-1",
                        project_id="project-1",
                        app_id="app-1",
                        provider="internal_coolify",
                        release_kind="production",
                        status="failed",
                        environment_name="production",
                        source_strategy="dockerfile",
                        git_ref="main",
                        commit_sha="abcdef1",
                        requested_by_user_id=None,
                        deployment_snapshot={},
                        provider_context={},
                        delivery_metadata={},
                        last_error="provider rejected create payload",
                        requested_at=now,
                        started_at=None,
                        completed_at=now,
                        destroyed_at=None,
                        created_at=now + timedelta(microseconds=1),
                        updated_at=now,
                    ),
                ]
            )
            session.commit()

        captured: dict[str, object] = {}

        def fake_provider_submit(**kwargs: object) -> dict[str, object]:
            captured["existing_application_uuid"] = kwargs.get("existing_application_uuid")
            return {
                "application_uuid": "application-existing",
                "deployment_uuid": "deployment-retry",
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
                payload=ProjectDeploymentReleaseCreate(app_id="app-1", git_ref="main", commit_sha="abcdef2"),
                requested_by_user_id="admin",
            )

        self.assertEqual(captured["existing_application_uuid"], "application-existing")

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

    def test_project_deployment_setup_validates_committed_npm_lockfiles_without_resolving_dependencies(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            service_dir = repo_dir / "web" / "admin"
            service_dir.mkdir(parents=True)
            _run_git_for_test(["init"], cwd=repo_dir)
            _run_git_for_test(["config", "user.name", "Test Bot"], cwd=repo_dir)
            _run_git_for_test(["config", "user.email", "test@example.com"], cwd=repo_dir)
            (service_dir / "package.json").write_text(
                (
                    '{"name":"admin","devDependencies":'
                    '{"vite":"^8.0.12","@vitejs/plugin-react":"^5.1.1"}}\n'
                ),
                encoding="utf-8",
            )
            (service_dir / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
            _run_git_for_test(["add", "web/admin/package.json", "web/admin/package-lock.json"], cwd=repo_dir)
            _run_git_for_test(["commit", "-m", "add npm app"], cwd=repo_dir)

            lockfiles = _validate_npm_lockfiles_for_deployment_branch(
                repo_dir=repo_dir,
                npm_service_source_paths=("web/admin",),
            )

            self.assertEqual(lockfiles, ("web/admin/package-lock.json",))

    def test_project_deployment_setup_commits_coolify_runtime_env_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            repo_dir = Path(temp_dir)
            _run_git_for_test(["init"], cwd=repo_dir)
            _run_git_for_test(["config", "user.name", "Test Bot"], cwd=repo_dir)
            _run_git_for_test(["config", "user.email", "test@example.com"], cwd=repo_dir)
            _run_git_for_test(["checkout", "-b", "main"], cwd=repo_dir)
            (repo_dir / "README.md").write_text("source\n", encoding="utf-8")
            service_dir = repo_dir / "web" / "admin"
            service_dir.mkdir(parents=True)
            (service_dir / "package.json").write_text(
                (
                    '{"name":"admin","devDependencies":'
                    '{"vite":"^8.0.12","@vitejs/plugin-react":"^5.1.1"}}\n'
                ),
                encoding="utf-8",
            )
            (service_dir / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
            _run_git_for_test(["add", "README.md", "web/admin/package.json", "web/admin/package-lock.json"], cwd=repo_dir)
            _run_git_for_test(["commit", "-m", "source"], cwd=repo_dir)
            source_commit = _run_git_for_test(["rev-parse", "HEAD"], cwd=repo_dir).strip()
            _run_git_for_test(["remote", "add", "origin", str(repo_dir)], cwd=repo_dir)

            branch, _commit, compose_path = _ensure_deployment_compose_artifact(
                repo_dir=repo_dir,
                project_id="project-1",
                source_branch="main",
                source_commit_sha=source_commit,
                compose_raw="services:\n  web:\n    image: example/web\n",
                npm_service_source_paths=("web/admin",),
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
                _validate_npm_lockfiles_for_deployment_branch(
                    repo_dir=repo_dir,
                    npm_service_source_paths=("web/admin",),
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
                return {
                    "uuid": application_uuid,
                    "fqdn": "https://web.project-1.apps.example.com",
                }

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
                "deployment_branch": "main",
                "deployment_commit_sha": "abcdef1",
                "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
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
            "web.development.main.project-1.bsktpay-2.localhost",
        )
        self.assertEqual(result["route_bindings"][0]["proxy_port"], 8088)
        self.assertEqual(result["route_bindings"][0]["internal_url"], "http://host.docker.internal:8088")
        self.assertEqual(result["route_bindings"][0]["service_key"], "web")
        assert fake_client.application_payload is not None
        self.assertEqual(fake_client.application_payload["name"], "development")
        self.assertEqual(fake_client.application_payload["build_pack"], "dockercompose")
        self.assertEqual(fake_client.application_payload["git_repository"], "example/repo")
        self.assertEqual(
            fake_client.application_payload["docker_compose_location"],
            "/.master-builder/deployments/docker-compose.yml",
        )
        self.assertEqual(
            fake_client.application_payload["docker_compose_domains"],
            [{"name": "web", "domain": "http://web.development.main.project-1.bsktpay-2.localhost"}],
        )
        self.assertNotIn("docker_compose_raw", fake_client.application_payload)
        self.assertEqual(fake_client.started_application_uuid, "application-1")

    def test_internal_coolify_release_submits_nixpacks_app_with_required_ports(self) -> None:
        class FakeCoolifyClient:
            def __init__(self) -> None:
                self.application_payload: dict[str, object] | None = None
                self.started_application_uuid: str | None = None

            def create_private_github_app_application(self, *, payload: dict[str, object]) -> str:
                self.application_payload = payload
                return "application-1"

            def bulk_update_application_envs(self, *, application_uuid: str, payload: dict[str, object]) -> dict[str, object]:
                return {"application_uuid": application_uuid, **payload}

            def start_application(self, *, application_uuid: str) -> str:
                self.started_application_uuid = application_uuid
                return "deployment-1"

            def get_application(self, *, application_uuid: str) -> dict[str, object]:
                return {"uuid": application_uuid, "fqdn": "https://web.project-1.apps.example.com"}

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
            name="Web",
            slug="web",
            source_path=".",
            detection_confidence=1.0,
            detected_runtime="react_native_web",
            detected_language="typescript",
            analysis_source="test",
            build_strategy="nixpacks",
            exposed_port=19006,
            healthcheck=None,
            start_command="npm run web",
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
                "source_strategy": "nixpacks",
                "deployment_branch": "main",
                "deployment_commit_sha": "abcdef1",
                "install_command": "npm install --package-lock=false --legacy-peer-deps --production=false",
                "build_command": "npm run build:web",
                "domains": [],
                "services": [],
                "resources": [],
                "backup_policies": [],
            }
        )
        fake_client = FakeCoolifyClient()

        with (
            self.session_factory() as session,
            patch("orchestrator.api.admin.deployment_release_service._resolve_secret_value", return_value="token"),
            patch("orchestrator.api.admin.deployment_release_service.CoolifyApiClient", return_value=fake_client),
        ):
            result = submit_internal_coolify_release(
                session=session,
                tenant=tenant,
                project=project,
                project_app=project_app,
                tenant_plane=tenant_plane,
                project_deployment=project_deployment,
                payload=ProjectDeploymentReleaseCreate(git_ref="main", commit_sha="abcdef1", release_kind="run_preview"),
                existing_application_uuid=None,
                existing_service_uuid=None,
            )

        self.assertEqual(result["application_uuid"], "application-1")
        self.assertEqual(result["deployment_uuid"], "deployment-1")
        self.assertEqual(fake_client.started_application_uuid, "application-1")
        assert fake_client.application_payload is not None
        self.assertEqual(fake_client.application_payload["build_pack"], "nixpacks")
        self.assertEqual(fake_client.application_payload["ports_exposes"], "19006")
        self.assertEqual(fake_client.application_payload["base_directory"], "/")
        self.assertEqual(
            fake_client.application_payload["start_command"],
            "npx expo-cli start --web --non-interactive --host 0.0.0.0 --port 19006",
        )
        self.assertEqual(
            fake_client.application_payload["install_command"],
            "npm install --package-lock=false --legacy-peer-deps --production=false && "
            "npm install --no-save --legacy-peer-deps expo-cli@3.28.6",
        )
        self.assertEqual(fake_client.application_payload["build_command"], "npm run build:web")
        self.assertNotIn("dockerfile_location", fake_client.application_payload)
        self.assertEqual(result["route_bindings"][0]["service_key"], "web")
        self.assertEqual(result["route_bindings"][0]["port"], "19006")
        self.assertEqual(result["route_bindings"][0]["proxy_port"], "19006")

    def test_internal_coolify_nixpacks_release_requires_exposed_port(self) -> None:
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
            name="Web",
            slug="web",
            source_path=".",
            detection_confidence=1.0,
            detected_runtime="react_native_web",
            detected_language="typescript",
            analysis_source="test",
            build_strategy="nixpacks",
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
                "source_strategy": "nixpacks",
                "deployment_branch": "main",
                "deployment_commit_sha": "abcdef1",
                "domains": [],
                "services": [],
                "resources": [],
                "backup_policies": [],
            }
        )

        with (
            self.session_factory() as session,
            patch("orchestrator.api.admin.deployment_release_service._resolve_secret_value", return_value="token"),
        ):
            with self.assertRaises(HTTPException) as raised:
                submit_internal_coolify_release(
                    session=session,
                    tenant=tenant,
                    project=project,
                    project_app=project_app,
                    tenant_plane=tenant_plane,
                    project_deployment=project_deployment,
                    payload=ProjectDeploymentReleaseCreate(git_ref="main", commit_sha="abcdef1", release_kind="run_preview"),
                    existing_application_uuid=None,
                    existing_service_uuid=None,
                )

        self.assertEqual(raised.exception.status_code, status.HTTP_409_CONFLICT)
        self.assertIn("Project app must define exposed_port", str(raised.exception.detail))

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
                        "host": "web.development.main.project-1.bsktpay-2.localhost",
                        "proxy_port": 8088,
                        "internal_url": "http://host.docker.internal:8088",
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
            "http://web.development.main.project-1.bsktpay-2.localhost:8088",
        )
        self.assertEqual(service_urls[0].host, "web.development.main.project-1.bsktpay-2.localhost")
        self.assertEqual(service_urls[0].proxy_port, 8088)
        self.assertEqual(service_urls[0].internal_url, "http://host.docker.internal:8088")

    def test_route_verification_uses_internal_probe_url_with_public_host_header(self) -> None:
        service_url = ProjectDeploymentServiceUrlRead.model_validate(
            {
                "service_key": "web",
                "service_name": "Web",
                "service_kind": "website",
                "url": "http://web.development.main.project-1.bsktpay-2.localhost:8088",
                "url_kind": "generated",
                "host": "web.development.main.project-1.bsktpay-2.localhost",
                "proxy_port": 8088,
                "internal_url": "http://host.docker.internal:8088",
            }
        )

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        captured = {}

        def fake_urlopen(request, timeout):  # noqa: ANN001
            captured["url"] = request.full_url
            captured["host"] = request.get_header("Host")
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("orchestrator.api.admin.deployment_release_service.urlopen", side_effect=fake_urlopen):
            result = _fetch_route_activation(service_url)

        self.assertIsNone(result)
        self.assertEqual(captured["url"], "http://host.docker.internal:8088")
        self.assertEqual(captured["host"], "web.development.main.project-1.bsktpay-2.localhost")
        self.assertEqual(captured["timeout"], 5)

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
                        "host": "web.development.main.project-1.bsktpay-2.localhost",
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
                        "host": "api.development.main.project-1.bsktpay-2.localhost",
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

    def test_reconcile_run_preview_enqueues_local_route_sync_command_when_provider_route_is_inactive(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
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
                    "region": "local",
                    "base_domain": "192-168-0-118.sslip.io:8088",
                    "api_base_url": "http://host.docker.internal:8000/api/v1",
                    "secret_refs": {"coolify_api_token": "tenant/tenant-1/COOLIFY_API_TOKEN"},
                    "state": "active",
                    "managed_host_id": "host-1",
                },
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
            app = ProjectApp(
                app_id="app-1",
                tenant_id="tenant-1",
                project_id="project-1",
                name="App 1",
                slug="app-1",
                source_path=".",
                detection_confidence=1.0,
                detected_runtime="compose",
                detected_language=None,
                analysis_source="test",
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
            host = DeploymentHost(
                host_id="host-1",
                label="Local host",
                provider="internal_coolify",
                infrastructure_provider="hetzner",
                region="local",
                capability_keys_json=["local_preview_routes"],
                agent_version="test",
                metadata_json={},
                state="active",
                bootstrap_token_hash=None,
                access_token_hash="access-token-hash",
                registered_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
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
                    "application_uuid": "app-uuid-1",
                    "deployment_uuid": "deployment-1",
                    "route_bindings": [
                        {
                            "service_key": "admin-website",
                            "service_name": "Admin Website",
                            "service_kind": "website",
                            "scheme": "http",
                            "host": "admin.preview.192-168-0-118.sslip.io",
                            "proxy_port": 8088,
                            "port": "80",
                            "internal_url": "http://host.docker.internal:8088",
                            "url_kind": "generated",
                        }
                    ],
                },
                last_error=None,
                requested_at=now,
                started_at=now,
                completed_at=None,
                created_at=now,
                updated_at=now,
                release_kind="run_preview",
                source_run_id="run-1",
                pr_number=None,
                delivery_metadata={},
                destroyed_at=None,
            )
            session.add_all([tenant, project, app, host, release])
            session.commit()

            class _FakeClient:
                pass

            with (
                patch("orchestrator.core.deployment_runtime._coolify_client_for_tenant", return_value=_FakeClient()),
                patch(
                    "orchestrator.core.deployment_runtime._coolify_observation_for_release",
                    return_value=CoolifyDeploymentObservation(
                        deployment_uuid="deployment-1",
                        application_uuid="app-uuid-1",
                        status="finished",
                        application_status="running",
                        last_error=None,
                    ),
                ),
                patch(
                    "orchestrator.core.deployment_runtime.verify_release_route_bindings",
                    return_value=SimpleNamespace(ok=False, error="provider route not active"),
                ),
            ):
                updated = reconcile_deployment_release(session=session, release=release)

            self.assertTrue(updated)
            session.refresh(release)
            self.assertEqual(release.status, "route_activating")
            command = session.execute(select(DeploymentHostCommand)).scalar_one()
            self.assertEqual(command.kind, "sync_local_preview_routes")
            self.assertEqual(command.release_id, "release-1")
            self.assertEqual(command.status, "queued")
            self.assertEqual(command.payload_json["deployment_uuid"], "deployment-1")
            self.assertEqual(command.payload_json["action"], "upsert")

    def test_reconcile_run_preview_fails_when_local_route_sync_command_failed(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
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
                    "region": "local",
                    "base_domain": "192-168-0-118.sslip.io:8088",
                    "api_base_url": "http://host.docker.internal:8000/api/v1",
                    "secret_refs": {"coolify_api_token": "tenant/tenant-1/COOLIFY_API_TOKEN"},
                    "state": "active",
                    "managed_host_id": "host-1",
                },
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
            app = ProjectApp(
                app_id="app-1",
                tenant_id="tenant-1",
                project_id="project-1",
                name="App 1",
                slug="app-1",
                source_path=".",
                detection_confidence=1.0,
                detected_runtime="compose",
                detected_language=None,
                analysis_source="test",
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
            host = DeploymentHost(
                host_id="host-1",
                label="Local host",
                provider="internal_coolify",
                infrastructure_provider="hetzner",
                region="local",
                capability_keys_json=["local_preview_routes"],
                agent_version="test",
                metadata_json={},
                state="active",
                bootstrap_token_hash=None,
                access_token_hash="access-token-hash",
                registered_at=now,
                last_seen_at=now,
                created_at=now,
                updated_at=now,
            )
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
                    "application_uuid": "app-uuid-1",
                    "deployment_uuid": "deployment-1",
                    "route_bindings": [
                        {
                            "service_key": "admin-website",
                            "service_name": "Admin Website",
                            "service_kind": "website",
                            "scheme": "http",
                            "host": "admin.preview.192-168-0-118.sslip.io",
                            "proxy_port": 8088,
                            "port": "80",
                            "internal_url": "http://host.docker.internal:8088",
                            "url_kind": "generated",
                        }
                    ],
                },
                last_error=None,
                requested_at=now,
                started_at=now,
                completed_at=None,
                created_at=now,
                updated_at=now,
                release_kind="run_preview",
                source_run_id="run-1",
                pr_number=None,
                delivery_metadata={},
                destroyed_at=None,
            )
            command = DeploymentHostCommand(
                command_id="command-1",
                host_id="host-1",
                tenant_id="tenant-1",
                project_id="project-1",
                app_id="app-1",
                restore_run_id=None,
                release_id="release-1",
                kind="sync_local_preview_routes",
                status="failed",
                claim_id="claim-1",
                lease_expires_at=None,
                available_at=now,
                attempt_count=1,
                payload_json={"deployment_uuid": "deployment-1", "action": "upsert"},
                result_json={},
                last_error="proxy write failed",
                claimed_at=now,
                started_at=now,
                completed_at=now,
                created_at=now,
                updated_at=now,
            )
            session.add_all([tenant, project, app, host, release, command])
            session.commit()

            class _FakeClient:
                pass

            with (
                patch("orchestrator.core.deployment_runtime._coolify_client_for_tenant", return_value=_FakeClient()),
                patch(
                    "orchestrator.core.deployment_runtime._coolify_observation_for_release",
                    return_value=CoolifyDeploymentObservation(
                        deployment_uuid="deployment-1",
                        application_uuid="app-uuid-1",
                        status="finished",
                        application_status="running",
                        last_error=None,
                    ),
                ),
                patch(
                    "orchestrator.core.deployment_runtime.verify_release_route_bindings",
                    return_value=SimpleNamespace(ok=False, error="provider route not active"),
                ),
            ):
                updated = reconcile_deployment_release(session=session, release=release)

            self.assertTrue(updated)
            session.refresh(release)
            self.assertEqual(release.status, "failed")
            self.assertEqual(release.last_error, "Local preview route sync failed: proxy write failed")

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
                "deployment_branch": "main",
                "deployment_commit_sha": "abcdef1",
                "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
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
                "deployment_branch": "main",
                "deployment_commit_sha": "abcdef1",
                "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
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
            "http://customer-api.production.main.project-1.bsktpay-2.localhost",
        )
        self.assertEqual(ports["customer-api"], "8080")
        self.assertEqual(route_bindings[0]["host"], "customer-api.production.main.project-1.bsktpay-2.localhost")
        self.assertEqual(route_bindings[0]["proxy_port"], 8088)
        self.assertEqual(route_bindings[0]["internal_url"], "http://host.docker.internal:8088")

    def test_coolify_generated_routes_use_http_for_local_lan_proxy_domain(self) -> None:
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
            name="Align",
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
                "region": "local",
                "base_domain": "192-168-0-118.sslip.io:8088",
                "platform_subdomain": "builder",
                "api_base_url": "http://host.docker.internal:8000/api/v1",
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
                "deployment_branch": "main",
                "deployment_commit_sha": "abcdef1",
                "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
                "domains": [],
                "services": [
                    {
                        "key": "admin-website",
                        "kind": "website",
                        "name": "Admin Website",
                        "compose_service": "admin-website",
                        "container_port": 3000,
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
            git_ref="mb/deploy/bsktpay-2-default/feature-ap-293-67d699cd48e2",
            exposed_ports_by_service={"admin-website": ["3000"]},
            release_kind="run_preview",
        )

        self.assertEqual(
            routes["admin-website"],
            "http://admin-website-production-mb-deploy-bsktpay-2-default-6d39449663.align.192-168-0-118.sslip.io",
        )
        self.assertEqual(ports["admin-website"], "3000")
        self.assertEqual(route_bindings[0]["scheme"], "http")
        self.assertEqual(
            route_bindings[0]["host"],
            "admin-website-production-mb-deploy-bsktpay-2-default-6d39449663.align.192-168-0-118.sslip.io",
        )
        self.assertLessEqual(len(route_bindings[0]["host"].split(".")[0]), 63)
        self.assertEqual(route_bindings[0]["host"].split(".")[1], "align")
        self.assertEqual(route_bindings[0]["path"], "")
        self.assertEqual(route_bindings[0]["proxy_port"], 8088)
        self.assertEqual(
            route_bindings[0]["internal_url"],
            "http://host.docker.internal:8088",
        )

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
                "deployment_branch": "main",
                "deployment_commit_sha": "abcdef1",
                "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
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
        self.assertNotIn("docker_compose_domains", fake_client.application_patch_payload)
        self.assertNotIn("project_uuid", fake_client.application_patch_payload)
        self.assertNotIn("environment_name", fake_client.application_patch_payload)
        self.assertNotIn("server_uuid", fake_client.application_patch_payload)
        self.assertNotIn("destination_uuid", fake_client.application_patch_payload)
        self.assertNotIn("github_app_uuid", fake_client.application_patch_payload)

    def test_internal_coolify_release_recreates_missing_existing_docker_compose_application(self) -> None:
        class FakeCoolifyClient:
            def __init__(self) -> None:
                self.update_called = False
                self.created_payload: dict[str, object] | None = None
                self.started_application_uuid: str | None = None

            def get_application(self, *, application_uuid: str) -> dict[str, object]:
                raise CoolifyApiError(
                    "Coolify API request failed (404) for GET /applications/application-stale: "
                    '{"message":"Application not found"}'
                )

            def update_application(self, *, application_uuid: str, payload: dict[str, object]) -> dict[str, object]:
                self.update_called = True
                return {"application_uuid": application_uuid, **payload}

            def create_private_github_app_application(self, *, payload: dict[str, object]) -> str:
                self.created_payload = payload
                return "application-new"

            def bulk_update_application_envs(self, *, application_uuid: str, payload: dict[str, object]) -> dict[str, object]:
                return {"application_uuid": application_uuid, **payload}

            def start_application(self, *, application_uuid: str) -> str:
                self.started_application_uuid = application_uuid
                return "deployment-new"

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
                "deployment_branch": "main",
                "deployment_commit_sha": "abcdef1",
                "deployment_compose_path": ".master-builder/deployments/docker-compose.yml",
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
                existing_application_uuid="application-stale",
                existing_service_uuid=None,
            )

        self.assertEqual(result["application_uuid"], "application-new")
        self.assertEqual(result["deployment_uuid"], "deployment-new")
        self.assertEqual(fake_client.started_application_uuid, "application-new")
        self.assertFalse(fake_client.update_called)
        assert fake_client.created_payload is not None
        self.assertEqual(fake_client.created_payload["build_pack"], "dockercompose")

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

    def test_release_reconciliation_does_not_fail_in_progress_release_from_stale_application_status(self) -> None:
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

        stale_application_status = _resolve_release_observation_transition(
            current_status="deploying",
            observed_status="finished",
            observed_application_status="starting:unknown",
        )

        self.assertEqual(stale_application_status, "live")

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
