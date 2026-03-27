import os
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
from tempfile import TemporaryDirectory

from sqlalchemy.exc import PendingRollbackError

from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_DISCORD_COMMAND,
    WEBHOOK_TRANSPORT_GITHUB,
    WEBHOOK_TRANSPORT_JIRA,
    WebhookJobEnqueueRequest,
    enqueue_webhook_job,
)
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Run, Tenant, WebhookJob


class WorkerWebhookJobServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/worker_webhook_job_service.db"

        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(database_url=self.database_url)

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
                Run(
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
                    last_error=None,
                    plan={"pre_check": {"outcome": "ready_for_agent"}},
                    created_at=now,
                    started_at=None,
                    last_heartbeat_at=None,
                    worker_service_instance_id=None,
                    finished_at=None,
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        reset_db_engine_cache()

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

    def test_blocking_reconciliation_cancels_stale_queued_run(self) -> None:
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
                    settings=SimpleNamespace(),
                    owner_id="worker-1",
                )

            self.assertIsNotNone(processed)
            job = session.get(WebhookJob, processed.job_id)
            run = session.get(Run, "run-1")
            self.assertEqual(job.status, "done")
            self.assertEqual(run.status, "cancelled")
            self.assertEqual(run.last_error, "Cancelled by jira_webhook:gtd_required")

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
                processed = process_next_webhook_job(
                    session=session,
                    settings=SimpleNamespace(),
                    owner_id="worker-1",
                )

            self.assertIsNotNone(processed)
            execute_command.assert_called_once()
            self.assertTrue(execute_command.call_args.kwargs["defer_seed_issues"])

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
            patch("orchestrator.core.worker.webhook_job_service._process_github_job", side_effect=_fail_processing),
            patch(
                "orchestrator.core.worker.webhook_job_service.mark_webhook_job_ids_failed",
                return_value=("failed-job",),
            ) as mark_failed,
        ):
            processed = process_next_webhook_job(
                session=session,
                settings=SimpleNamespace(),
                owner_id="worker-1",
            )

        self.assertEqual(processed, "failed-job")
        session.rollback.assert_called_once()
        mark_failed.assert_called_once()
