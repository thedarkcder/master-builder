from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from orchestrator.core.audit_events import prune_audit_events
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import AuditEvent, Tenant
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class AuditEventRetentionTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="audit-events")

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def test_prune_audit_events_respects_retention_and_legal_hold(self) -> None:
        now = datetime.now(timezone.utc)
        stale = now - timedelta(days=45)
        session_factory = create_session_factory(self.database_url)

        with session_factory() as session:
            session.add_all(
                [
                    Tenant(
                        tenant_id="tenant-a",
                        name="Tenant A",
                        is_enabled=True,
                        archived_at=None,
                        purge_after_at=None,
                        jira_config={},
                        github_config={},
                        repos_config={},
                        policy_config={
                            "observability": {
                                "audit_retention_days": 30,
                                "audit_export_enabled": True,
                                "legal_hold_enabled": False,
                                "legal_hold_reason": None,
                            }
                        },
                        discord_config=None,
                        experience_config={},
                        setup_state={},
                        created_at=now,
                        updated_at=now,
                    ),
                    Tenant(
                        tenant_id="tenant-b",
                        name="Tenant B",
                        is_enabled=True,
                        archived_at=None,
                        purge_after_at=None,
                        jira_config={},
                        github_config={},
                        repos_config={},
                        policy_config={
                            "observability": {
                                "audit_retention_days": 30,
                                "audit_export_enabled": True,
                                "legal_hold_enabled": True,
                                "legal_hold_reason": "Hold",
                            }
                        },
                        discord_config=None,
                        experience_config={},
                        setup_state={},
                        created_at=now,
                        updated_at=now,
                    ),
                ]
            )
            session.add_all(
                [
                    AuditEvent(
                        event_id="event-a-stale",
                        tenant_id="tenant-a",
                        project_id=None,
                        workflow_id=None,
                        run_id=None,
                        operation_id=None,
                        attempt_id=None,
                        issue_key=None,
                        actor_type=None,
                        actor_id=None,
                        source_component="test",
                        event_kind="execution_failed",
                        level="error",
                        correlation_id=None,
                        trace_id=None,
                        span_id=None,
                        message="stale a",
                        payload_json={},
                        recorded_at=stale,
                    ),
                    AuditEvent(
                        event_id="event-a-fresh",
                        tenant_id="tenant-a",
                        project_id=None,
                        workflow_id=None,
                        run_id=None,
                        operation_id=None,
                        attempt_id=None,
                        issue_key=None,
                        actor_type=None,
                        actor_id=None,
                        source_component="test",
                        event_kind="execution_failed",
                        level="error",
                        correlation_id=None,
                        trace_id=None,
                        span_id=None,
                        message="fresh a",
                        payload_json={},
                        recorded_at=now,
                    ),
                    AuditEvent(
                        event_id="event-b-stale",
                        tenant_id="tenant-b",
                        project_id=None,
                        workflow_id=None,
                        run_id=None,
                        operation_id=None,
                        attempt_id=None,
                        issue_key=None,
                        actor_type=None,
                        actor_id=None,
                        source_component="test",
                        event_kind="execution_failed",
                        level="error",
                        correlation_id=None,
                        trace_id=None,
                        span_id=None,
                        message="stale b",
                        payload_json={},
                        recorded_at=stale,
                    ),
                ]
            )
            session.commit()

        with session_factory() as session:
            deleted = prune_audit_events(session=session, now=now)
            session.commit()
            remaining_event_ids = set(
                session.execute(select(AuditEvent.event_id).order_by(AuditEvent.event_id.asc())).scalars().all()
            )

        self.assertEqual(deleted, 1)
        self.assertEqual(remaining_event_ids, {"event-a-fresh", "event-b-stale"})
