import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from sqlalchemy.exc import PendingRollbackError
from sqlalchemy import select

from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_COOLIFY_DEPLOYMENT,
    WEBHOOK_TRANSPORT_DISCORD_COMMAND,
    WEBHOOK_TRANSPORT_GITHUB,
    WEBHOOK_TRANSPORT_JIRA,
    WEBHOOK_TRANSPORT_PROJECT_AUTOMATION,
    WEBHOOK_TRANSPORT_PROJECT_APP_ANALYSIS,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.core.project_app_planner import (
    ProjectAppAnalysisResult,
    ProjectAppAnalysisRunMetadata,
    ProjectAppNormalizedCandidate,
)
from orchestrator.core.project_app_artifact_pr_runtime import ProjectAppArtifactPRResult
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import (
    Project,
    ProjectApp,
    ProjectAppAnalysisRun,
    ProjectDeploymentRelease,
    Run,
    Tenant,
    WebhookJob,
)
from orchestrator.core.communications import DiscordChannelMessageWithAttachmentAction
from orchestrator.core.communications import IngressResult
from tests.workflow_test_support import add_run_with_workflow, make_run


class WorkerWebhookJobServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/worker_webhook_job_service.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)

        now = datetime.now(timezone.utc)
        ready_snapshot = ExecutionSnapshot.empty()
        ready_snapshot.context.execution_context["pre_check_outcome"] = "ready_for_agent"
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
                    discord_config={"channel_id": "channel-automation"},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            add_run_with_workflow(
                session,
                make_run(
                    run_id="run-1",
                    tenant_id="tenant-1",
                    project_id=None,
                    issue_key="TP-186",
                    issue_summary="Queued stale run",
                    issue_description="desc",
                    repo_url="https://github.com/example/repo",
                    branch=None,
                    pr_url=None,
                    dedupe_scope="issue_execution",
                    status="queued",
                    plan=ready_snapshot.dump(),
                    created_at=now,
                    started_at=None,
                    last_heartbeat_at=None,
                    worker_service_instance_id=None,
                    finished_at=None,
                ),
            )
            session.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        reset_db_engine_cache()

    @staticmethod
    def _settings() -> SimpleNamespace:
        return SimpleNamespace(secrets_encryption_key="test-key")

    @staticmethod
    def _request() -> WebhookJobEnqueueRequest:
        return WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_JIRA,
            request_id="request-1",
            tenant_id="tenant-1",
            project_id=None,
            subject_key="jira:tenant-1:TP-186",
            dedupe_key="delivery-1",
            event_type="jira:issue_updated",
            payload_json={"webhookEvent": "jira:issue_updated"},
            context_json={
                "snapshot": {
                    "request_id": "request-1",
                    "tenant_id": "tenant-1",
                    "payload": {"webhookEvent": "jira:issue_updated"},
                    "webhook_event": "jira:issue_updated",
                    "issue_key": "TP-186",
                    "issue_labels": ["agent:ready"],
                    "issue_status": "To Do",
                    "issue_status_category_key": "indeterminate",
                    "issue_summary": "TP-186",
                    "issue_description": "desc",
                    "comment_command": None,
                    "comment_command_argument": None,
                    "comment_command_error": None,
                    "delivery_id": "delivery-1",
                    "project_id": None,
                }
            },
        )

    @staticmethod
    def _project_automation_request(
        *,
        request_id: str,
        execution_id: str,
        automation_id: str = "automation-1",
    ) -> WebhookJobEnqueueRequest:
        return WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_PROJECT_AUTOMATION,
            request_id=request_id,
            tenant_id="tenant-1",
            project_id="project-1",
            subject_key="project_automation:tenant-1:project-1:daily-update",
            dedupe_key=request_id,
            event_type="standup_voice_brief",
            payload_json={"execution_id": execution_id, "automation_id": automation_id},
            context_json={},
        )

    @staticmethod
    def _github_request(*, request_id: str, dedupe_key: str, subject_key: str) -> WebhookJobEnqueueRequest:
        return WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_GITHUB,
            request_id=request_id,
            tenant_id="tenant-1",
            project_id="project-1",
            subject_key=subject_key,
            dedupe_key=dedupe_key,
            event_type="check_run",
            payload_json={"action": "completed"},
            context_json={
                "tenant_id": "tenant-1",
                "project_id": "project-1",
                "pr_number": 26,
                "review_summary_present": False,
                "delivery_id": dedupe_key,
                "github_event": "check_run",
                "normalized_action": "completed",
                "installation_id": 12345,
                "repo_full_name": "example/repo",
            },
        )

    @staticmethod
    def _github_pull_request_closed_merged_request(
        *,
        request_id: str,
        dedupe_key: str,
        subject_key: str,
        pr_number: int,
        head_ref: str,
    ) -> WebhookJobEnqueueRequest:
        return WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_GITHUB,
            request_id=request_id,
            tenant_id="tenant-1",
            project_id="project-1",
            subject_key=subject_key,
            dedupe_key=dedupe_key,
            event_type="pull_request",
            payload_json={
                "action": "closed",
                "pull_request": {
                    "number": pr_number,
                    "merged": True,
                    "head": {"ref": head_ref},
                },
            },
            context_json={
                "tenant_id": "tenant-1",
                "project_id": "project-1",
                "pr_number": pr_number,
                "review_summary_present": False,
                "delivery_id": dedupe_key,
                "github_event": "pull_request",
                "normalized_action": "closed",
                "installation_id": 12345,
                "repo_full_name": "example/repo",
            },
        )

    @staticmethod
    def _coolify_request(
        *,
        request_id: str,
        token: str,
        deployment_uuid: str = "deployment-1",
        application_uuid: str = "application-1",
        status: str = "success",
    ) -> WebhookJobEnqueueRequest:
        return WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_COOLIFY_DEPLOYMENT,
            request_id=request_id,
            tenant_id="tenant-1",
            project_id="project-1",
            subject_key="coolify_deployment:tenant-1:project-1",
            dedupe_key=request_id,
            event_type=f"deployment_{status}",
            payload_json={
                "status": status,
                "deployment_uuid": deployment_uuid,
                "application_uuid": application_uuid,
                "event_type": f"deployment_{status}",
            },
            context_json={"webhook_token": token},
        )

    @staticmethod
    def _project_app_analysis_request(
        *,
        request_id: str,
        analysis_run_id: str,
        checkout_path: str,
        analysis_source: str = "manual_analyze",
        planner_version: str | None = "planner-v1",
    ) -> WebhookJobEnqueueRequest:
        return WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_PROJECT_APP_ANALYSIS,
            request_id=request_id,
            tenant_id="tenant-1",
            project_id="project-1",
            subject_key=f"project_app_analysis:tenant-1:project-1:{analysis_run_id}",
            dedupe_key=analysis_run_id,
            event_type="project_app_analysis",
            payload_json={
                "analysis_run_id": analysis_run_id,
                "checkout_path": checkout_path,
                "analysis_source": analysis_source,
                "planner_version": planner_version,
            },
            context_json={
                "analysis_run_id": analysis_run_id,
                "checkout_path": checkout_path,
                "analysis_source": analysis_source,
            },
        )

    def test_blocking_reconciliation_does_not_cancel_existing_run(self) -> None:
        with self.session_factory() as session:
            enqueue_webhook_job(session, request=self._request())
            session.commit()

        with self.session_factory() as session:
            with (
                patch(
                    "orchestrator.core.worker.webhook_job_service._refresh_jira_context_from_live_issue",
                    side_effect=lambda **kwargs: None,
                ),
                patch(
                    "orchestrator.core.worker.webhook_job_service._process_jira_webhook_context",
                    return_value=SimpleNamespace(
                        content={
                            "issue_key": "TP-186",
                            "enqueued": False,
                            "reason": "gtd_required",
                        },
                        actions=(),
                    ),
                ),
            ):
                processed = process_next_webhook_job(
                    session=session,
                    settings=self._settings(),
                    owner_id="worker-1",
                )

            self.assertIsNotNone(processed)
            job = session.get(WebhookJob, processed.job_id)
            run = session.get(Run, "run-1")
            self.assertEqual(job.status, "done")
            self.assertEqual(run.status, "queued")
            self.assertIsNone(run.last_error)

    def test_discord_command_jobs_derive_seed_deferral_in_worker_service(self) -> None:
        request = WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_DISCORD_COMMAND,
            request_id="request-2",
            tenant_id="tenant-1",
            project_id=None,
            subject_key="discord_channel:tenant-1:channel-1",
            dedupe_key="discord-request-2",
            event_type="command_webhook",
            payload_json={
                "user_id": "user-1",
                "command": "!issues seed build stories",
                "channel_id": "channel-1",
            },
            context_json={},
        )
        with self.session_factory() as session:
            enqueue_webhook_job(session, request=request)
            session.commit()

        with self.session_factory() as session:
            with patch(
                "orchestrator.core.worker.webhook_job_service.execute_tenant_discord_ingress_command"
            ) as execute_command:
                processed = process_next_webhook_job(session=session, settings=self._settings(), owner_id="worker-1")

            self.assertIsNotNone(processed)
            execute_command.assert_called_once()
            self.assertTrue(execute_command.call_args.kwargs["defer_seed_issues"])

    def test_project_automation_jobs_dispatch_and_mark_success(self) -> None:
        with self.session_factory() as session:
            enqueue_webhook_job(session, request=self._project_automation_request(request_id="request-automation-1", execution_id="automation-exec-1"))
            session.commit()

        with self.session_factory() as session:
            with (
                patch(
                    "orchestrator.core.worker.webhook_job_service.prepare_project_automation_execution",
                    return_value=SimpleNamespace(
                        execution_id="automation-exec-1",
                        action=DiscordChannelMessageWithAttachmentAction(
                            channel_id="channel-automation",
                            content="summary",
                            filename="voice.wav",
                            file_bytes=b"voice",
                            content_type="audio/wav",
                        ),
                        already_succeeded=False,
                        window_end_at=datetime.now(timezone.utc),
                    ),
                ),
                patch(
                    "orchestrator.core.worker.webhook_job_service.execute_side_effect_action",
                    return_value={"message_id": "discord-msg-1", "channel_id": "channel-automation"},
                ) as execute_side_effect,
                patch("orchestrator.core.worker.webhook_job_service.mark_project_automation_execution_success") as mark_success,
            ):
                processed = process_next_webhook_job(session=session, settings=self._settings(), owner_id="worker-1")

            self.assertIsNotNone(processed)
            execute_side_effect.assert_called_once()
            mark_success.assert_called_once()
            self.assertEqual(mark_success.call_args.kwargs["discord_message_id"], "discord-msg-1")
            self.assertEqual(session.get(WebhookJob, processed.job_id).status, "done")

    def test_project_automation_jobs_mark_failure_when_send_fails(self) -> None:
        with self.session_factory() as session:
            enqueue_webhook_job(session, request=self._project_automation_request(request_id="request-automation-2", execution_id="automation-exec-2"))
            session.commit()

        with self.session_factory() as session:
            with (
                patch(
                    "orchestrator.core.worker.webhook_job_service.prepare_project_automation_execution",
                    return_value=SimpleNamespace(
                        execution_id="automation-exec-2",
                        action=DiscordChannelMessageWithAttachmentAction(
                            channel_id="channel-automation",
                            content="summary",
                            filename="voice.wav",
                            file_bytes=b"voice",
                            content_type="audio/wav",
                        ),
                        already_succeeded=False,
                        window_end_at=datetime.now(timezone.utc),
                    ),
                ),
                patch(
                    "orchestrator.core.worker.webhook_job_service.execute_side_effect_ingress_result",
                    side_effect=RuntimeError("send failed"),
                ),
                patch("orchestrator.core.worker.webhook_job_service.mark_project_automation_execution_failure") as mark_failed,
            ):
                processed = process_next_webhook_job(session=session, settings=self._settings(), owner_id="worker-1")

            self.assertIsNotNone(processed)
            mark_failed.assert_called_once()
            self.assertEqual(session.get(WebhookJob, processed.job_id).status, "failed")

    def test_project_app_analysis_job_persists_run_and_app_upsert(self) -> None:
        now = datetime.now(timezone.utc)
        checkout_path = self.temp_dir.name
        with self.session_factory() as session:
            session.add(
                ProjectAppAnalysisRun(
                    run_id="analysis-run-1",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    status="queued",
                    planner_version="planner-v1",
                    request_payload={
                        "analysis_source": "manual_analyze",
                        "checkout_path": checkout_path,
                        "planner_version": "planner-v1",
                    },
                    result_payload={},
                    error=None,
                    created_at=now,
                    started_at=None,
                    completed_at=None,
                    updated_at=now,
                )
            )
            session.add(
                ProjectApp(
                    app_id="app-1",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    name="Old app",
                    slug="old-app",
                    source_path=".",
                    detection_confidence=0.1,
                    detected_runtime="node",
                    detected_language="javascript",
                    analysis_source="legacy",
                    build_strategy="nixpacks",
                    exposed_port=3000,
                    healthcheck=None,
                    start_command=None,
                    env_schema_json={},
                    secret_schema_json={},
                    deployment_config={},
                    status="draft",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()
            enqueue_webhook_job(
                session,
                request=self._project_app_analysis_request(
                    request_id="request-analysis-1",
                    analysis_run_id="analysis-run-1",
                    checkout_path=checkout_path,
                ),
            )
            session.commit()

        normalized_app = ProjectAppNormalizedCandidate(
            name="api-service",
            source_path=".",
            build_strategy="dockerfile",
            detected_runtime="python",
            detected_language="python",
            detection_confidence=0.9,
            exposed_port=8000,
            healthcheck="/healthz",
            start_command="python app.py",
            env_schema_json={"APP_ENV": {"required": True}},
            secret_schema_json={"API_TOKEN": {"required": True}},
            deployment_config={},
            analysis_source="manual_analyze",
            needs_generated_files=True,
            resources_json=(),
            env_json={"APP_ENV": {"required": True}},
            secret_json={"API_TOKEN": {"required": True}},
            slug="api-service",
        )
        analysis_result = ProjectAppAnalysisResult(
            apps=(normalized_app,),
            metadata=ProjectAppAnalysisRunMetadata(
                tenant_id="tenant-1",
                project_id="project-1",
                checkout_path=checkout_path,
                analysis_source="manual_analyze",
                planner_version="planner-v1",
                pre_scan_count=1,
                runtime_count=1,
                normalized_count=1,
                raw_planner_result_json={
                    "apps": [
                        {
                            "name": "api-service",
                            "source_path": ".",
                            "build_strategy": "dockerfile",
                            "port": 8000,
                            "healthcheck": "/healthz",
                            "resources": [],
                            "env": {"APP_ENV": {"required": True}},
                            "secrets": {"API_TOKEN": {"required": True}},
                            "needs_generated_files": True,
                        }
                    ]
                },
            ),
        )

        with self.session_factory() as session:
            with (
                patch(
                    "orchestrator.core.worker.webhook_job_service.run_project_app_analysis",
                    return_value=analysis_result,
                ),
                patch(
                    "orchestrator.core.worker.webhook_job_service.create_project_app_artifact_pr",
                    return_value=ProjectAppArtifactPRResult(
                        pr_number=77,
                        pr_url="https://github.com/example/repo/pull/77",
                        head_branch="project-app-artifacts/analysis-run-1",
                        base_branch="main",
                        generated_files=("Dockerfile",),
                    ),
                ),
            ):
                processed = process_next_webhook_job(session=session, settings=self._settings(), owner_id="worker-1")

            assert processed is not None
            job = session.get(WebhookJob, processed.job_id)
            run = session.get(ProjectAppAnalysisRun, "analysis-run-1")
            app_row = session.execute(
                select(ProjectApp).where(
                    ProjectApp.tenant_id == "tenant-1",
                    ProjectApp.project_id == "project-1",
                    ProjectApp.source_path == ".",
                )
            ).scalars().one()

            assert job.status == "done"
            assert run.status == "completed"
            assert run.result_payload["normalized_apps"][0]["name"] == "api-service"
            assert run.result_payload["artifact_pr"]["pr_number"] == 77
            assert run.result_payload["artifact_pr"]["generated_files"] == ["Dockerfile"]
            assert app_row.app_id == "app-1"
            assert app_row.name == "api-service"
            assert app_row.status == "needs_pr_merge"

    def test_project_app_analysis_job_routes_generated_files_candidates_into_artifact_pr_flow(self) -> None:
        now = datetime.now(timezone.utc)
        checkout_path = self.temp_dir.name
        with self.session_factory() as session:
            session.add(
                ProjectAppAnalysisRun(
                    run_id="analysis-run-artifact-pr-1",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    status="queued",
                    planner_version="planner-v1",
                    request_payload={
                        "analysis_source": "manual_analyze",
                        "checkout_path": checkout_path,
                        "planner_version": "planner-v1",
                    },
                    result_payload={},
                    error=None,
                    created_at=now,
                    started_at=None,
                    completed_at=None,
                    updated_at=now,
                )
            )
            session.commit()
            enqueue_webhook_job(
                session,
                request=self._project_app_analysis_request(
                    request_id="request-analysis-artifact-pr-1",
                    analysis_run_id="analysis-run-artifact-pr-1",
                    checkout_path=checkout_path,
                ),
            )
            session.commit()

        normalized_app = ProjectAppNormalizedCandidate(
            name="api-service",
            source_path=".",
            build_strategy="dockerfile",
            detected_runtime="python",
            detected_language="python",
            detection_confidence=0.9,
            exposed_port=8000,
            healthcheck="/healthz",
            start_command="python app.py",
            env_schema_json={"APP_ENV": {"required": True}},
            secret_schema_json={"API_TOKEN": {"required": True}},
            deployment_config={},
            analysis_source="manual_analyze",
            needs_generated_files=True,
            resources_json=(),
            env_json={"APP_ENV": {"required": True}},
            secret_json={"API_TOKEN": {"required": True}},
            slug="api-service",
        )
        analysis_result = ProjectAppAnalysisResult(
            apps=(normalized_app,),
            metadata=ProjectAppAnalysisRunMetadata(
                tenant_id="tenant-1",
                project_id="project-1",
                checkout_path=checkout_path,
                analysis_source="manual_analyze",
                planner_version="planner-v1",
                pre_scan_count=1,
                runtime_count=1,
                normalized_count=1,
                raw_planner_result_json={
                    "apps": [
                        {
                            "name": "api-service",
                            "source_path": ".",
                            "build_strategy": "dockerfile",
                            "port": 8000,
                            "healthcheck": "/healthz",
                            "resources": [],
                            "env": {"APP_ENV": {"required": True}},
                            "secrets": {"API_TOKEN": {"required": True}},
                            "needs_generated_files": True,
                        }
                    ]
                },
            ),
        )
        artifact_pr_result = ProjectAppArtifactPRResult(
            pr_number=17,
            pr_url="https://github.com/example/repo/pull/17",
            head_branch="artifact-pr/analysis-run-artifact-pr-1",
            base_branch="main",
            generated_files=("Dockerfile", "docker-compose.yml"),
        )

        with self.session_factory() as session:
            with (
                patch(
                    "orchestrator.core.worker.webhook_job_service.run_project_app_analysis",
                    return_value=analysis_result,
                ),
                patch(
                    "orchestrator.core.worker.webhook_job_service.create_project_app_artifact_pr",
                    return_value=artifact_pr_result,
                ) as artifact_pr_mock,
            ):
                processed = process_next_webhook_job(session=session, settings=self._settings(), owner_id="worker-1")

            assert processed is not None
            job = session.get(WebhookJob, processed.job_id)
            run = session.get(ProjectAppAnalysisRun, "analysis-run-artifact-pr-1")

            assert job.status == "done"
            assert run.status == "completed"
            assert run.result_payload["artifact_pr"] == artifact_pr_result.to_result_json()
            artifact_pr_mock.assert_called_once()

    def test_project_app_analysis_job_marks_run_failed_on_planner_error(self) -> None:
        now = datetime.now(timezone.utc)
        checkout_path = self.temp_dir.name
        with self.session_factory() as session:
            session.add(
                ProjectAppAnalysisRun(
                    run_id="analysis-run-2",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    status="queued",
                    planner_version="planner-v1",
                    request_payload={
                        "analysis_source": "manual_analyze",
                        "checkout_path": checkout_path,
                        "planner_version": "planner-v1",
                    },
                    result_payload={},
                    error=None,
                    created_at=now,
                    started_at=None,
                    completed_at=None,
                    updated_at=now,
                )
            )
            session.commit()
            enqueue_webhook_job(
                session,
                request=self._project_app_analysis_request(
                    request_id="request-analysis-2",
                    analysis_run_id="analysis-run-2",
                    checkout_path=checkout_path,
                ),
            )
            session.commit()

        with self.session_factory() as session:
            with patch(
                "orchestrator.core.worker.webhook_job_service.run_project_app_analysis",
                side_effect=RuntimeError("planner exploded"),
            ):
                processed = process_next_webhook_job(session=session, settings=self._settings(), owner_id="worker-1")

            assert processed is not None
            job = session.get(WebhookJob, processed.job_id)
            run = session.get(ProjectAppAnalysisRun, "analysis-run-2")
            assert job.status == "failed"
            assert run.status == "failed"
            assert "planner exploded" in (run.error or "")

    def test_coolify_deployment_webhook_transitions_release_on_valid_token(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-1")
            project = session.get(Project, "project-1")
            assert tenant is not None
            assert project is not None
            tenant.deployment_plane_config = {
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "state": "active",
                "base_domain": "apps.example.com",
                "platform_subdomain": "builder",
                "secret_refs": {
                    "coolify_webhook_token": "platform/COOLIFY_WEBHOOK_TOKEN",
                },
            }
            project.deployment_config = {
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "dockerfile",
                "domains": [{"key": "primary", "host": "app.example.com"}],
                "resources": [],
                "backup_policies": [],
            }
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-1",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    provider="internal_coolify",
                    status="queued",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="main",
                    commit_sha="abcdef",
                    requested_by_user_id="user-1",
                    deployment_snapshot={},
                    provider_context={
                        "application_uuid": "application-1",
                        "deployment_uuid": "deployment-1",
                    },
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        with self.session_factory() as session:
            enqueue_webhook_job(
                session,
                request=self._coolify_request(
                    request_id="coolify-request-1",
                    token="token-123",
                ),
            )
            session.commit()

        with self.session_factory() as session:
            with patch(
                "orchestrator.core.deployment_runtime.resolve_platform_secret_ref",
                return_value="token-123",
            ):
                processed = process_next_webhook_job(session=session, settings=self._settings(), owner_id="worker-1")

            self.assertIsNotNone(processed)
            job = session.get(WebhookJob, processed.job_id)
            release = session.get(ProjectDeploymentRelease, "release-1")
            self.assertEqual(job.status, "done")
            self.assertEqual(release.status, "provisioning")
            self.assertEqual(release.provider_context["deployment_uuid"], "deployment-1")

    def test_coolify_deployment_webhook_invalid_token_fails_job(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-1")
            project = session.get(Project, "project-1")
            assert tenant is not None
            assert project is not None
            tenant.deployment_plane_config = {
                "provider": "internal_coolify",
                "infrastructure_provider": "hetzner",
                "state": "active",
                "base_domain": "apps.example.com",
                "platform_subdomain": "builder",
                "secret_refs": {
                    "coolify_webhook_token": "platform/COOLIFY_WEBHOOK_TOKEN",
                },
            }
            project.deployment_config = {
                "enabled": True,
                "environment_name": "production",
                "source_strategy": "dockerfile",
                "domains": [{"key": "primary", "host": "app.example.com"}],
                "resources": [],
                "backup_policies": [],
            }
            session.add(
                ProjectDeploymentRelease(
                    release_id="release-2",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    provider="internal_coolify",
                    status="queued",
                    environment_name="production",
                    source_strategy="dockerfile",
                    git_ref="main",
                    commit_sha="abcdef",
                    requested_by_user_id="user-1",
                    deployment_snapshot={},
                    provider_context={"deployment_uuid": "deployment-2"},
                    last_error=None,
                    requested_at=now,
                    started_at=now,
                    completed_at=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

        with self.session_factory() as session:
            enqueue_webhook_job(
                session,
                request=self._coolify_request(
                    request_id="coolify-request-2",
                    token="wrong-token",
                    deployment_uuid="deployment-2",
                ),
            )
            session.commit()

        with self.session_factory() as session:
            with patch(
                "orchestrator.core.deployment_runtime.resolve_platform_secret_ref",
                return_value="expected-token",
            ):
                processed = process_next_webhook_job(session=session, settings=self._settings(), owner_id="worker-1")

            self.assertIsNotNone(processed)
            job = session.get(WebhookJob, processed.job_id)
            release = session.get(ProjectDeploymentRelease, "release-2")
            self.assertEqual(job.status, "failed")
            self.assertEqual(release.status, "queued")

    def test_github_subject_jobs_are_coalesced_and_marked_done_together(self) -> None:
        with self.session_factory() as session:
            first = enqueue_webhook_job(
                session,
                request=self._github_request(
                    request_id="request-github-1",
                    dedupe_key="delivery-github-1",
                    subject_key="github_pr:tenant-1:example/repo:26",
                ),
            ).job
            second = enqueue_webhook_job(
                session,
                request=self._github_request(
                    request_id="request-github-2",
                    dedupe_key="delivery-github-2",
                    subject_key="github_pr:tenant-1:example/repo:26",
                ),
            ).job
            session.commit()

        with self.session_factory() as session:
            with (
                patch(
                    "orchestrator.core.worker.webhook_job_service.build_github_review_runtime",
                    return_value=(SimpleNamespace(), SimpleNamespace()),
                ),
                patch(
                    "orchestrator.core.worker.webhook_job_service.build_github_webhook_ingress_result",
                    return_value=IngressResult(actions=()),
                ) as build_result,
                patch(
                    "orchestrator.core.worker.webhook_job_service.build_http_transport_action_executors",
                    return_value=(),
                ),
                patch(
                    "orchestrator.core.worker.webhook_job_service.execute_side_effect_ingress_result",
                ),
            ):
                processed = process_next_webhook_job(
                    session=session,
                    settings=self._settings(),
                    owner_id="worker-1",
                )

            self.assertIsNotNone(processed)
            assert processed is not None
            self.assertEqual(session.get(WebhookJob, first.job_id).status, "done")
            self.assertEqual(session.get(WebhookJob, second.job_id).status, "done")
            self.assertEqual(session.get(WebhookJob, first.job_id).attempt_count, 1)
            self.assertEqual(session.get(WebhookJob, second.job_id).attempt_count, 1)
            build_result.assert_called_once()

    def test_github_pull_request_merged_marks_matching_apps_ready(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                ProjectApp(
                    app_id="app-merge-1",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    name="API",
                    slug="api",
                    source_path=".",
                    detection_confidence=0.9,
                    detected_runtime="python",
                    detected_language="python",
                    analysis_source="manual_analyze",
                    build_strategy="dockerfile",
                    exposed_port=8000,
                    healthcheck=None,
                    start_command="python app.py",
                    env_schema_json={},
                    secret_schema_json={},
                    deployment_config={},
                    status="needs_pr_merge",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ProjectAppAnalysisRun(
                    run_id="analysis-run-merge-1",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    status="completed",
                    planner_version="planner-v1",
                    request_payload={},
                    result_payload={
                        "artifact_pr": {
                            "pr_number": 42,
                            "head_branch": "project-app-artifacts/analysis-run-merge-1",
                        },
                        "normalized_apps": [
                            {
                                "name": "api",
                                "source_path": ".",
                                "needs_generated_files": True,
                            }
                        ],
                    },
                    error=None,
                    created_at=now,
                    started_at=now,
                    completed_at=now,
                    updated_at=now,
                )
            )
            enqueue_webhook_job(
                session,
                request=self._github_pull_request_closed_merged_request(
                    request_id="request-github-merge-1",
                    dedupe_key="delivery-github-merge-1",
                    subject_key="github_pr:tenant-1:example/repo:42",
                    pr_number=42,
                    head_ref="project-app-artifacts/analysis-run-merge-1",
                ),
            )
            session.commit()

        with self.session_factory() as session:
            with patch("orchestrator.core.worker.webhook_job_service.build_github_review_runtime") as build_runtime:
                processed = process_next_webhook_job(
                    session=session,
                    settings=self._settings(),
                    owner_id="worker-1",
                )

            assert processed is not None
            app = session.get(ProjectApp, "app-merge-1")
            job = session.get(WebhookJob, processed.job_id)
            assert app is not None
            assert job is not None
            assert app.status == "ready"
            assert job.status == "done"
            build_runtime.assert_not_called()

    def test_github_pull_request_merged_ignores_stale_analysis_runs(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                ProjectApp(
                    app_id="app-merge-2",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    name="API",
                    slug="api-stale",
                    source_path=".",
                    detection_confidence=0.9,
                    detected_runtime="python",
                    detected_language="python",
                    analysis_source="manual_analyze",
                    build_strategy="dockerfile",
                    exposed_port=8000,
                    healthcheck=None,
                    start_command="python app.py",
                    env_schema_json={},
                    secret_schema_json={},
                    deployment_config={},
                    status="needs_pr_merge",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                ProjectAppAnalysisRun(
                    run_id="analysis-run-merge-old",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    status="completed",
                    planner_version="planner-v1",
                    request_payload={},
                    result_payload={
                        "artifact_pr": {
                            "pr_number": 42,
                            "head_branch": "project-app-artifacts/analysis-run-merge-old",
                        },
                        "normalized_apps": [
                            {
                                "name": "api",
                                "source_path": ".",
                                "needs_generated_files": True,
                            }
                        ],
                    },
                    error=None,
                    created_at=now - timedelta(minutes=20),
                    started_at=now - timedelta(minutes=20),
                    completed_at=now - timedelta(minutes=20),
                    updated_at=now - timedelta(minutes=20),
                )
            )
            session.add(
                ProjectAppAnalysisRun(
                    run_id="analysis-run-merge-new",
                    tenant_id="tenant-1",
                    project_id="project-1",
                    status="completed",
                    planner_version="planner-v1",
                    request_payload={},
                    result_payload={
                        "artifact_pr": {
                            "pr_number": 43,
                            "head_branch": "project-app-artifacts/analysis-run-merge-new",
                        },
                        "normalized_apps": [
                            {
                                "name": "api",
                                "source_path": ".",
                                "needs_generated_files": True,
                            }
                        ],
                    },
                    error=None,
                    created_at=now - timedelta(minutes=5),
                    started_at=now - timedelta(minutes=5),
                    completed_at=now - timedelta(minutes=5),
                    updated_at=now - timedelta(minutes=5),
                )
            )
            enqueue_webhook_job(
                session,
                request=self._github_pull_request_closed_merged_request(
                    request_id="request-github-merge-2",
                    dedupe_key="delivery-github-merge-2",
                    subject_key="github_pr:tenant-1:example/repo:42",
                    pr_number=42,
                    head_ref="project-app-artifacts/analysis-run-merge-old",
                ),
            )
            session.commit()

        with self.session_factory() as session:
            processed = process_next_webhook_job(
                session=session,
                settings=self._settings(),
                owner_id="worker-1",
            )

            assert processed is not None
            app = session.get(ProjectApp, "app-merge-2")
            assert app is not None
            assert app.status == "needs_pr_merge"

    def test_process_next_webhook_job_snapshots_job_context_before_rollback(self) -> None:
        class _BrokenJob:
            def __init__(self) -> None:
                self._broken = False
                self.tenant_id = "tenant-1"
                self.subject_key = "github:tenant-1:pr-1"
                self.job_id = "job-1"

            @property
            def transport(self) -> str:
                if self._broken:
                    raise PendingRollbackError("session rolled back")
                return WEBHOOK_TRANSPORT_GITHUB

        broken_job = _BrokenJob()
        claim = SimpleNamespace(acquired=True, job=broken_job)
        session = MagicMock()

        def _fail_processing(*, claimed_job, **_kwargs):  # noqa: ANN001
            claimed_job._broken = True
            raise RuntimeError("flush failed")

        with (
            patch("orchestrator.core.worker.webhook_job_service.claim_next_webhook_job", return_value=claim),
            patch("orchestrator.core.worker.webhook_job_service._process_github_subject_jobs", side_effect=_fail_processing),
            patch(
                "orchestrator.core.worker.webhook_job_service.mark_webhook_job_ids_failed",
                return_value=("failed-job",),
            ) as mark_failed,
        ):
            processed = process_next_webhook_job(
                session=session,
                settings=self._settings(),
                owner_id="worker-1",
            )

        self.assertEqual(processed, "failed-job")
        session.rollback.assert_called_once()
        mark_failed.assert_called_once()
