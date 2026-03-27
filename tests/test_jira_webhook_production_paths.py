from __future__ import annotations

from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from orchestrator.api.main import create_app
from orchestrator.core.config import get_settings
from orchestrator.core.followup_context_service import upsert_followup_context
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.storage.models import DecisionCase, DecisionCycle, DecisionEvidence, FollowupContext, Run, Tenant
from tests.production_path_support import (
    clear_runtime_environment,
    configure_runtime_environment,
    load_json_fixture,
    seed_core_runtime_state,
    session_factory_for,
)

pytestmark = pytest.mark.production_path


class JiraWebhookProductionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="jira_webhook_production.db",
            include_admin=True,
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(self.session_factory)
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def _process_one_webhook_job(self):
        with self.session_factory() as session:
            return process_next_webhook_job(
                session=session,
                settings=get_settings(),
                owner_id="worker:test",
            )

    def test_disabled_tenant_short_circuits_on_real_route(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "example")
            assert tenant is not None
            tenant.is_enabled = False
            session.commit()

        response = self.client.post(
            "/jira/webhook/example",
            json=load_json_fixture("jira", "webhooks", "issue_updated.json"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reason"], "tenant_disabled")
        self.assertFalse(response.json()["enqueued"])

    def test_ready_label_override_enqueues_through_real_route(self) -> None:
        payload = load_json_fixture("jira", "webhooks", "issue_updated.json")
        payload["issue"]["fields"]["labels"] = ["ready_for_agent"]

        fake_oauth = SimpleNamespace(
            access_token="access-token",
            connection=SimpleNamespace(cloud_id="cloud-1"),
            client=SimpleNamespace(
                get_issue_detail=lambda **_kwargs: SimpleNamespace(
                    summary="Webhook ticket",
                    description="desc",
                    status="To Do",
                    status_category_key="new",
                    labels=["ready_for_agent"],
                )
            ),
        )

        with patch("orchestrator.core.worker.webhook_job_service.tenant_jira_oauth_context", return_value=fake_oauth):
            response = self.client.post("/jira/webhook/example", json=payload)
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertFalse(body["enqueued"])
        self.assertTrue(body["queued"])
        self.assertEqual(body["reason"], "queued_for_reconciliation")
        self.assertEqual(body["issue_key"], "TP-42")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

        with self.session_factory() as session:
            tenant_runs = session.execute(
                select(Run).where(Run.tenant_id == "example", Run.issue_key == "TP-42")
            ).scalars().all()
            self.assertTrue(tenant_runs)

    def test_comment_reply_records_decision_evidence_through_real_route(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            session.add_all(
                [
                    DecisionCase(
                        case_id="case-1",
                        tenant_id="example",
                        project_id="example-default",
                        issue_key="TP-42",
                        state="blocked",
                        blocked_reason="decision_gate_required",
                        classification="decision_gate",
                        issue_fingerprint="fingerprint-1",
                        active_cycle_id="cycle-1",
                        last_source="jira_webhook",
                        last_event_type="jira_webhook",
                        last_event_at=now,
                        required_worker_capability="linux",
                        required_worker_label="worker:linux",
                        ready_label="agent:ready",
                        ready_label_present=False,
                        metadata_json={"source": "jira"},
                        created_at=now,
                        updated_at=now,
                    ),
                    DecisionCycle(
                        cycle_id="cycle-1",
                        case_id="case-1",
                        tenant_id="example",
                        project_id="example-default",
                        issue_key="TP-42",
                        status="open",
                        reason="Need cross-account policy",
                        classification="decision_gate",
                        question_set_json=[
                            {
                                "id": "q1",
                                "kind": "decision_gate",
                                "text": "What should happen when a linked device_id is used by another user?",
                            }
                        ],
                        unresolved_question_ids_json=["q1"],
                        metadata_json={"asked_via": "jira"},
                        opened_at=now,
                        closed_at=None,
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            upsert_followup_context(
                session=session,
                tenant_id="example",
                project_id="example-default",
                context_type="decision_gate",
                channel_id="discord-channel-1",
                thread_channel_id="thread-1",
                root_message_id="message-1",
                issue_key="TP-42",
                metadata={"source": "discord"},
            )
            session.commit()

        runtime = unittest.mock.MagicMock()
        runtime.run_json.return_value = {
            "answers": [
                {
                    "question_id": "q1",
                    "status": "accepted",
                    "answer": "Reject relink; the device_id stays bound to the original user.",
                    "notes": "",
                }
            ]
        }
        with (
            patch("orchestrator.core.decision_reply_service.build_codex_runtime", return_value=runtime),
            patch("orchestrator.api.webhooks.jira_webhook_comment_flow.post_jira_comment", return_value=(True, None)),
        ):
            response = self.client.post(
                "/jira/webhook/example",
                json=load_json_fixture("jira", "webhooks", "comment_created.json"),
                headers={"X-Atlassian-Webhook-Identifier": "delivery-1"},
            )
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertEqual(body["reason"], "queued_for_reconciliation")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

        with self.session_factory() as session:
            evidences = session.execute(
                select(DecisionEvidence)
                .where(
                    DecisionEvidence.tenant_id == "example",
                    DecisionEvidence.issue_key == "TP-42",
                )
            ).scalars().all()
            case = session.get(DecisionCase, "case-1")
            followup_context = session.execute(
                select(FollowupContext).where(
                    FollowupContext.tenant_id == "example",
                    FollowupContext.issue_key == "TP-42",
                )
            ).scalars().one()

        self.assertEqual(len(evidences), 1)
        self.assertIn("Reject relink", evidences[0].raw_text)
        assert case is not None
        self.assertIsNone(case.active_cycle_id)
        self.assertTrue(case.decision_gate_closed_permanently)
        self.assertEqual(followup_context.status, "closed")
        self.assertIsNotNone(followup_context.closed_at)
