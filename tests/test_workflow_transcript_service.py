from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.api.admin.workflow_transcript_service import build_workflow_step_transcript
from orchestrator.api.admin.schema_mappers import workflow_observability_event_to_schema
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import (
    AuditEvent,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
)
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class WorkflowTranscriptServiceTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="workflow-transcript")

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def test_build_workflow_step_transcript_groups_attempts_and_sections(self) -> None:
        now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-215",
                execution_id="wfexec-mab-215",
                workflow_type_key="parent_planning",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="MAB-215",
                issue_summary="Identity and authorization v1 contract",
                issue_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="temporal",
                dedupe_scope="parent_planning",
                status="failed",
                last_error="Answer the product clarification on Jira issue MAB-215, then retry engineering child fanout.",
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            operation = WorkflowOperation(
                operation_id="operation-jira-child-fanout",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="jira_child_fanout",
                idempotency_key="jira-child-fanout:MAB-215",
                status="failed",
                target_system="jira",
                target_ref="MAB-215",
                summary="Engineering child fanout completed from the confirmed parent brief.",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            attempt = WorkflowOperationAttempt(
                attempt_id="attempt-7",
                operation_id=operation.operation_id,
                attempt_number=7,
                status="failed",
                error_category="missing_input",
                error_message="Answer the product clarification on Jira issue MAB-215, then retry engineering child fanout.\n\nQuestions to answer:\n- Which entitlement checks must be enforced?",
                status_detail=None,
                retryable=True,
                next_retry_at=None,
                created_at=now,
                started_at=now,
                finished_at=now,
            )
            session.add(workflow)
            session.add(operation)
            session.add(attempt)
            session.add_all(
                [
                    AuditEvent(
                        event_id="event-request",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        workflow_id=workflow.workflow_id,
                        run_id=None,
                        operation_id=operation.operation_id,
                        attempt_id=attempt.attempt_id,
                        issue_key="MAB-215",
                        actor_type=None,
                        actor_id=None,
                        source_component="runtime_invocation",
                        event_kind="stage_request",
                        level="info",
                        correlation_id=None,
                        trace_id=None,
                        span_id=None,
                        message="Submitted runtime request.",
                        payload_json={
                            "attempt": 7,
                            "system_prompt": "You are the planner.",
                            "user_prompt": "Create or refresh engineering child tickets.",
                        },
                        recorded_at=now,
                    ),
                    AuditEvent(
                        event_id="event-tool",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        workflow_id=workflow.workflow_id,
                        run_id=None,
                        operation_id=operation.operation_id,
                        attempt_id=attempt.attempt_id,
                        issue_key="MAB-215",
                        actor_type=None,
                        actor_id=None,
                        source_component="runtime_invocation",
                        event_kind="tool_request",
                        level="info",
                        correlation_id=None,
                        trace_id=None,
                        span_id=None,
                        message="Requested tool jira.search.",
                        payload_json={
                            "attempt": 7,
                            "tool_name": "jira.search",
                            "tool_args": {"query": "project = MAB"},
                        },
                        recorded_at=now,
                    ),
                    AuditEvent(
                        event_id="event-external",
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        workflow_id=workflow.workflow_id,
                        run_id=None,
                        operation_id=operation.operation_id,
                        attempt_id=attempt.attempt_id,
                        issue_key="MAB-215",
                        actor_type=None,
                        actor_id=None,
                        source_component="jira_seed",
                        event_kind="jira_child_upsert_request",
                        level="info",
                        correlation_id=None,
                        trace_id=None,
                        span_id=None,
                        message="Submitting child Jira issue upsert for Create tenant assurance boundary.",
                        payload_json={
                            "attempt": 7,
                            "summary": "Create tenant assurance boundary",
                            "description": "Detailed ticket body",
                        },
                        recorded_at=now,
                    ),
                ]
            )
            session.commit()

            transcript = build_workflow_step_transcript(
                session=session,
                workflow=workflow,
                operation=operation,
                attempts=[attempt],
                telemetry_events=[],
                audit_events=[
                    workflow_observability_event_to_schema(row)
                    for row in session.query(AuditEvent).order_by(AuditEvent.recorded_at.asc()).all()
                ],
                source="audit",
            )

        assert transcript.operation_id == "operation-jira-child-fanout"
        assert transcript.current_status == "failed"
        assert transcript.source == "audit"
        assert len(transcript.attempts) == 1
        rendered_attempt = transcript.attempts[0]
        assert rendered_attempt.attempt_id == "attempt-7"
        assert rendered_attempt.attempt_number == 7
        assert rendered_attempt.failure_message.startswith("Answer the product clarification")
        assert rendered_attempt.recommended_next_action == "Answer the product clarification on Jira issue MAB-215, then retry engineering child fanout."
        assert [section.kind for section in rendered_attempt.sections] == [
            "prompts",
            "tool_calls",
            "external_requests",
            "outcome",
        ]
        assert rendered_attempt.sections[0].entries[0].payload["user_prompt"] == "Create or refresh engineering child tickets."
        assert rendered_attempt.sections[1].entries[0].payload["tool_name"] == "jira.search"
        assert rendered_attempt.sections[2].entries[0].payload["summary"] == "Create tenant assurance boundary"
        assert rendered_attempt.sections[3].entries[0].message.startswith("Answer the product clarification")

    def test_build_workflow_step_transcript_does_not_fallback_to_attempt_state_for_telemetry(self) -> None:
        now = datetime(2026, 4, 21, 12, 0, 0, tzinfo=timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            workflow = WorkflowExecution(
                workflow_id="parent_planning:MAB-215",
                execution_id="wfexec-mab-215",
                workflow_type_key="parent_planning",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="MAB-215",
                issue_summary="Identity and authorization v1 contract",
                issue_description="Parent planning",
                repo_url=None,
                branch=None,
                pr_url=None,
                orchestration_backend="temporal",
                dedupe_scope="parent_planning",
                status="failed",
                last_error="Answer the product clarification on Jira issue MAB-215, then retry engineering child fanout.",
                active_run_id=None,
                latest_checkpoint_id=None,
                source_workflow_id=None,
                source_run_id=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            operation = WorkflowOperation(
                operation_id="operation-jira-child-fanout",
                workflow_id=workflow.workflow_id,
                run_id=None,
                operation_type="jira_child_fanout",
                idempotency_key="jira-child-fanout:MAB-215",
                status="failed",
                target_system="jira",
                target_ref="MAB-215",
                summary="Engineering child fanout completed from the confirmed parent brief.",
                created_at=now,
                started_at=now,
                finished_at=now,
                updated_at=now,
            )
            attempt = WorkflowOperationAttempt(
                attempt_id="attempt-7",
                operation_id=operation.operation_id,
                attempt_number=7,
                status="failed",
                error_category="missing_input",
                error_message="Answer the product clarification on Jira issue MAB-215, then retry engineering child fanout.",
                status_detail=None,
                retryable=True,
                next_retry_at=None,
                created_at=now,
                started_at=now,
                finished_at=now,
            )
            session.add_all([workflow, operation, attempt])
            session.commit()

            transcript = build_workflow_step_transcript(
                session=session,
                workflow=workflow,
                operation=operation,
                attempts=[attempt],
                telemetry_events=[],
                audit_events=[],
                source="telemetry",
            )

        assert transcript.source == "telemetry"
        assert transcript.attempts == []
