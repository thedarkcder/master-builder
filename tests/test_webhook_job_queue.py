import os
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory

from orchestrator.core.webhook_job_queue import (
    WEBHOOK_TRANSPORT_JIRA,
    WebhookJobEnqueueRequest,
    _acquire_subject_claim,
    claim_next_webhook_job,
    enqueue_webhook_job,
)
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import Tenant, WebhookJob, WebhookSubjectClaim


class WebhookJobQueueTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/webhook_job_queue.db"

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
            session.commit()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        reset_db_engine_cache()

    @staticmethod
    def _request(*, issue_key: str, delivery_id: str, request_id: str) -> WebhookJobEnqueueRequest:
        return WebhookJobEnqueueRequest(
            transport=WEBHOOK_TRANSPORT_JIRA,
            request_id=request_id,
            tenant_id="tenant-1",
            project_id=None,
            subject_key=f"jira:tenant-1:{issue_key}",
            dedupe_key=delivery_id,
            event_type="jira:issue_updated",
            payload_json={"webhookEvent": "jira:issue_updated"},
            context_json={"snapshot": {"issue_key": issue_key}},
        )

    def test_claim_next_job_skips_subject_with_live_processing_lease(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            enqueue_webhook_job(
                session,
                request=self._request(issue_key="TP-1", delivery_id="delivery-1", request_id="request-1"),
                now=now,
            )
            enqueue_webhook_job(
                session,
                request=self._request(issue_key="TP-2", delivery_id="delivery-2", request_id="request-2"),
                now=now + timedelta(seconds=1),
            )
            session.add(
                WebhookSubjectClaim(
                    subject_key="jira:tenant-1:TP-1",
                    owner_id="other-worker",
                    lease_expires_at=now + timedelta(minutes=5),
                    updated_at=now,
                )
            )
            session.commit()

            claim = claim_next_webhook_job(
                session,
                owner_id="worker-1",
                now=now + timedelta(seconds=2),
            )

            self.assertTrue(claim.acquired)
            self.assertIsNotNone(claim.job)
            self.assertEqual(claim.job.subject_key, "jira:tenant-1:TP-2")
            self.assertEqual(claim.job.status, "processing")
            claimed_job = session.get(WebhookJob, claim.job.job_id)
            self.assertIsNotNone(claimed_job)
            self.assertEqual(claimed_job.owner_id, "worker-1")

    def test_acquire_subject_claim_does_not_steal_live_lease_from_other_owner(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add(
                WebhookSubjectClaim(
                    subject_key="jira:tenant-1:TP-3",
                    owner_id="other-worker",
                    lease_expires_at=now + timedelta(minutes=5),
                    updated_at=now,
                )
            )
            session.commit()

            acquired = _acquire_subject_claim(
                session,
                subject_key="jira:tenant-1:TP-3",
                owner_id="worker-1",
                now=now + timedelta(seconds=1),
            )

            self.assertFalse(acquired)
            claim = session.get(WebhookSubjectClaim, {"subject_key": "jira:tenant-1:TP-3"})
            self.assertIsNotNone(claim)
            self.assertEqual(claim.owner_id, "other-worker")
