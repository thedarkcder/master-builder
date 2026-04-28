from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from orchestrator.core.audit_events import record_audit_event
from orchestrator.core.observability_stream import record_observability_stream_event
from orchestrator.core.product_events import list_product_events, reset_event_store_for_tests
from orchestrator.core.runtime_invocation import AgentInvocationContext, _emit_invocation_event
from orchestrator.core.workflow_execution_projection import (
    WorkflowExecutionReference,
    WorkflowSourceReference,
    ensure_workflow_execution,
)
from orchestrator.storage.db import create_session_factory
from orchestrator.core.workflow_type_catalog import get_workflow_type
from orchestrator.storage.models import Run
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class OperationObservabilityContractTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="operation-observability-contracts")

    def tearDown(self) -> None:
        reset_event_store_for_tests()
        self._cleanup_test_database()

    def test_operation_scoped_audit_event_requires_attempt_id(self) -> None:
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            with pytest.raises(ValueError, match="operation_id and attempt_id"):
                record_audit_event(
                    session,
                    tenant_id="tenant-a",
                    project_id=None,
                    workflow_id="workflow-1",
                    run_id=None,
                    operation_id="operation-1",
                    attempt_id=None,
                    issue_key="MAB-215",
                    actor_type="agent",
                    actor_id="system",
                    source_component="test",
                    event_kind="attempt_started",
                    level="info",
                    message="attempt started",
                    payload={},
                )

    def test_operation_scoped_stream_event_requires_attempt_id(self) -> None:
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            with pytest.raises(ValueError, match="operation_id and attempt_id"):
                record_observability_stream_event(
                    session,
                    tenant_id="tenant-a",
                    project_id=None,
                    workflow_id="workflow-1",
                    run_id=None,
                    operation_id="operation-1",
                    attempt_id=None,
                    issue_key="MAB-215",
                    event_kind="runtime_log",
                    level="info",
                    source_component="test",
                    message="runtime line",
                    payload={},
                )

    def test_run_scoped_runtime_invocation_event_is_persisted_without_operation(self) -> None:
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow_type = get_workflow_type(session, workflow_type_key="parent_planning")
            assert workflow_type is not None
            projection = ensure_workflow_execution(
                session=session,
                workflow_type=workflow_type,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                execution=WorkflowExecutionReference(
                    key="MAB-900",
                    source=WorkflowSourceReference(source_system="jira", source_ref="MAB-900"),
                ),
                display_name="Run scoped telemetry",
                description="Runtime invocation without workflow operation",
            )
            session.add(
                Run(
                    run_id="run-scoped-1",
                    workflow_id=projection.workflow.workflow_id,
                    tenant_id="tenant-a",
                    project_id="tenant-a-default",
                    issue_key="MAB-900",
                    issue_summary=None,
                    issue_description=None,
                    repo_url=None,
                    branch=None,
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
                    plan=None,
                    created_at=datetime.now(timezone.utc),
                    dispatch_claimed_at=None,
                    started_at=None,
                    last_heartbeat_at=None,
                    worker_service_instance_id=None,
                    finished_at=None,
                )
            )
            session.commit()

        executed_sql: list[str] = []

        class _FakeStore:
            def execute(self, sql: str) -> str:
                executed_sql.append(sql)
                return ""

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                "orchestrator.core.runtime_invocation.get_settings",
                lambda: SimpleNamespace(database_url=self.database_url, agent_id="agent-test"),
            )
            monkeypatch.setattr("orchestrator.core.product_events.event_store", lambda: _FakeStore())
            _emit_invocation_event(
                context=AgentInvocationContext(
                    channel="worker",
                    tenant_id="tenant-a",
                    project_id="tenant-a-default",
                    command="workflow",
                    stage="pm",
                    working_dir="/workspace",
                    run_id="run-scoped-1",
                    invocation_id="inv-run-scoped",
                ),
                event_kind="stage_request",
                payload={"message": "Submitted runtime request.", "system_prompt": "system", "user_prompt": "user"},
            )

        assert executed_sql
        persisted_sql = "\n".join(executed_sql)
        assert "execution_log_events" in persisted_sql
        assert "run-scoped-1" in persisted_sql
        assert "stage_request" in persisted_sql
        assert "inv-run-scoped" in persisted_sql

    def test_clickhouse_naive_timestamp_is_returned_as_utc_aware_iso(self) -> None:
        class _FakeStore:
            def query_events(self, _sql: str):
                from orchestrator.core.product_events import _event_from_json

                return [
                    _event_from_json(
                        {
                            "event_sequence": 1,
                            "event_id": "event-1",
                            "event_class": "execution_log",
                            "tenant_id": "tenant-a",
                            "project_id": None,
                            "workflow_id": "workflow-1",
                            "run_id": None,
                            "operation_id": "operation-1",
                            "attempt_id": "attempt-1",
                            "issue_key": "MAB-215",
                            "event_kind": "workflow_operation_attempt_started",
                            "level": "info",
                            "source_component": "workflow_operation",
                            "message": "Started attempt.",
                            "payload_json": "{}",
                            "recorded_at": "2026-04-24 16:33:24.887931",
                        }
                    )
                ]

        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr("orchestrator.core.product_events.event_store", lambda: _FakeStore())
            events = list_product_events(
                event_class="execution_log",
                filters={"attempt_id": "attempt-1"},
                limit=1,
            )

        assert events[0].recorded_at.tzinfo is timezone.utc
        assert events[0].recorded_at.isoformat() == "2026-04-24T16:33:24.887931+00:00"
