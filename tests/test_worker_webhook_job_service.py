import os
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from sqlalchemy.exc import PendingRollbackError

from orchestrator.core.webhook_job_errors import RetryableWebhookJobError
from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_DISCORD_COMMAND,
    WEBHOOK_TRANSPORT_GITHUB,
    WEBHOOK_TRANSPORT_JIRA,
    WEBHOOK_TRANSPORT_PROJECT_AUTOMATION,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Project, Run, Tenant, WebhookJob
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

    def test_subject_batch_failure_marks_all_claimed_jobs_failed(self) -> None:
        first_request = self._request()
        second_request = replace(
            first_request,
            request_id="request-2",
            dedupe_key="delivery-2",
        )
        with self.session_factory() as session:
            first = enqueue_webhook_job(session, request=first_request).job
            second = enqueue_webhook_job(session, request=second_request).job
            session.commit()

        with self.session_factory() as session:
            with patch(
                "orchestrator.core.worker.webhook_job_service._process_jira_subject_jobs",
                side_effect=RuntimeError("atlassian auth failed"),
            ):
                processed = process_next_webhook_job(
                    session=session,
                    settings=self._settings(),
                    owner_id="worker-1",
                )

            self.assertIsNotNone(processed)
            first_persisted = session.get(WebhookJob, first.job_id)
            second_persisted = session.get(WebhookJob, second.job_id)
            self.assertIsNotNone(first_persisted)
            self.assertIsNotNone(second_persisted)
            assert first_persisted is not None
            assert second_persisted is not None
            self.assertEqual(first_persisted.status, "failed")
            self.assertEqual(second_persisted.status, "failed")
            self.assertEqual(first_persisted.last_error, "atlassian auth failed")
            self.assertEqual(second_persisted.last_error, "atlassian auth failed")
            self.assertIsNone(first_persisted.owner_id)
            self.assertIsNone(second_persisted.owner_id)

    def test_subject_batch_failure_stores_root_cause_for_wrapped_workflow_update(self) -> None:
        root = ValueError("Event attempt_id must belong to the supplied operation_id")
        terminal = RuntimeError("terminal_workflow_advance_error: Event attempt_id must belong to the supplied operation_id")
        terminal.__cause__ = root
        wrapper = RuntimeError("Workflow update failed")
        wrapper.__cause__ = terminal
        with self.session_factory() as session:
            enqueued = enqueue_webhook_job(session, request=self._request()).job
            session.commit()

        with self.session_factory() as session:
            with patch(
                "orchestrator.core.worker.webhook_job_service._process_jira_subject_jobs",
                side_effect=wrapper,
            ):
                processed = process_next_webhook_job(
                    session=session,
                    settings=self._settings(),
                    owner_id="worker-1",
                )

            self.assertIsNotNone(processed)
            persisted = session.get(WebhookJob, enqueued.job_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(persisted.status, "failed")
            self.assertEqual(persisted.last_error, "Event attempt_id must belong to the supplied operation_id")

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
        claim = SimpleNamespace(
            acquired=True,
            batch=SimpleNamespace(
                primary_job=broken_job,
                related_jobs=(),
                job_ids=("job-1",),
            ),
        )
        session = MagicMock()

        def _fail_processing(*, claimed_job, **_kwargs):  # noqa: ANN001
            claimed_job._broken = True
            raise RuntimeError("flush failed")

        with (
            patch("orchestrator.core.worker.webhook_job_service.claim_next_webhook_subject_batch", return_value=claim),
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

    def test_process_next_webhook_job_requeues_retryable_failures(self) -> None:
        with self.session_factory() as session:
            enqueued = enqueue_webhook_job(session, request=self._request()).job
            session.commit()

        with self.session_factory() as session:
            with patch(
                "orchestrator.core.worker.webhook_job_service._process_jira_subject_jobs",
                side_effect=RetryableWebhookJobError("runtime temporarily unavailable", retry_after_seconds=45),
            ):
                processed = process_next_webhook_job(
                    session=session,
                    settings=self._settings(),
                    owner_id="worker-1",
                )

            self.assertIsNotNone(processed)
            assert processed is not None
            persisted = session.get(WebhookJob, enqueued.job_id)
            self.assertIsNotNone(persisted)
            assert persisted is not None
            self.assertEqual(processed.status, "pending")
            self.assertEqual(persisted.status, "pending")
            self.assertEqual(persisted.attempt_count, 1)
            self.assertEqual(persisted.last_error, "runtime temporarily unavailable")
            self.assertIsNone(persisted.owner_id)
            self.assertGreater(persisted.available_at, persisted.updated_at)
