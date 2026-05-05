from __future__ import annotations

import json
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import patch

from sqlalchemy import select

from orchestrator.api.admin.runs.logging_stream_service import stream_run_events_ndjson
from orchestrator.core.observability.agent_observability import record_agent_lifecycle_event
from orchestrator.core.config import get_settings
from orchestrator.core.observability.logging_pane import emit_logging_pane_event
from orchestrator.core.observability.repository import (
    configure_product_event_repository_for_tests,
    reset_product_event_repository_for_tests,
)
from orchestrator.core.workflow.type_catalog import ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt
from orchestrator.storage.models import Run
from orchestrator.temporal.activities.run_execution import execute_claimed_run_activity
from orchestrator.temporal.payloads import DevelopmentTeamRunWorkflowInput
from tests.test_support.admin_api_harness import AdminApiTestHarness
from tests.test_support.jira_parent_workflow_boundary import JiraParentWorkflowBoundaryHarness
from tests.test_support.jira_webhook_api_harness import JiraWebhookTestsHarness
from tests.test_support.product_events import RecordingProductEventRepository
from tests.workflow_test_support import add_workflow_attempt


class WorkflowBoundaryPathTests(JiraWebhookTestsHarness):
    def test_jira_webhook_creates_workflow_attempt_telemetry_and_jira_projection(self) -> None:
        boundary = JiraParentWorkflowBoundaryHarness(
            tenant_id="tenant-webhook",
            issue_key="TP-997",
            issue_summary="Runtime architecture boundary",
            issue_description="Create durable workflow boundary coverage for parent planning.",
        )
        payload = self._jira_issue_payload(
            issue_key="TP-997",
            labels=["pm-parent"],
            status_name="Backlog",
        )
        payload["webhookEvent"] = "jira:issue_created"
        payload["issue"]["fields"]["summary"] = "Runtime architecture boundary"
        payload["issue"]["fields"]["description"] = "Create durable workflow boundary coverage for parent planning."

        with boundary.installed():
            response = self.client.post(
                "/jira/webhook/tenant-webhook",
                json=payload,
            )
            processed = self._process_one_webhook_job()

        self._assert_jira_issue_event_queued(response, issue_key="TP-997")
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")

        with self.session_factory() as session:
            workflow = session.get(WorkflowExecution, "parent_planning:TP-997")
            self.assertIsNotNone(workflow)
            assert workflow is not None
            self.assertEqual(workflow.status, "completed")
            operations = {
                operation.operation_type: operation
                for operation in session.execute(
                    select(WorkflowOperation).where(WorkflowOperation.workflow_id == workflow.workflow_id)
                ).scalars()
            }
            for operation_type in (
                "brief_normalization",
                "jira_parent_update",
                "backlog_planning",
                "jira_child_fanout",
            ):
                self.assertIn(operation_type, operations)
                attempts = session.execute(
                    select(WorkflowOperationAttempt).where(
                        WorkflowOperationAttempt.operation_id == operations[operation_type].operation_id
                    )
                ).scalars().all()
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0].status, "completed")

        self.assertEqual(boundary.jira.created_issue_keys, ["TP-998"])
        self.assertTrue(boundary.jira.updated_parent_labels)
        self.assertEqual(boundary.jira.updated_parent_labels[-1], ["pm-parent"])
        self.assertEqual([issue_key for issue_key, _comment in boundary.jira.comments], ["TP-997"])
        self.assertTrue(boundary.telemetry.contains_kind("runtime_log"))
        self.assertTrue(boundary.telemetry.contains_kind("stage_request"))
        self.assertTrue(boundary.telemetry.contains_kind("jira_child_upsert_request"))
        self.assertTrue(boundary.telemetry.all_operation_events_have_attempt_ids())


class RunTemporalStreamBoundaryTests(AdminApiTestHarness):
    def test_run_temporal_activity_records_attempt_and_streams_to_log_pane_contract(self) -> None:
        self._insert_jira_connection()
        tenant_response = self.client.post(
            "/api/admin/tenants",
            json=self._tenant_payload(),
            auth=("admin", "secret"),
        )
        self.assertEqual(tenant_response.status_code, 201)
        repository = RecordingProductEventRepository()
        now = datetime.now(timezone.utc)
        claim_id = "claim-run-temporal-boundary"
        workflow_id = "workflow-run-temporal-boundary"
        run_id = "run-temporal-boundary"
        session_factory = create_session_factory(self.database_url)

        with session_factory() as session:
            add_workflow_attempt(
                session,
                workflow_id=workflow_id,
                run_id=run_id,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-611",
                issue_summary="Temporal stream boundary",
                issue_description="Prove run execution streams to UI logs.",
                repo_url="https://github.com/example/repo",
                branch="feature/temporal-stream",
                workflow_type_key="issue_execution",
                workflow_status="running",
                run_status="dispatching",
                attempt_number=1,
                claim_id=claim_id,
                worker_service_instance_id="worker:test",
                dispatch_claimed_at=now,
                now=now,
            )
            session.commit()

        def _process_claimed_run(*, session, selection, **_kwargs):  # noqa: ANN001, ANN003
            run = selection.run
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow_id,
                    WorkflowOperation.operation_type == ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
                )
            ).scalar_one()
            attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == operation.operation_id,
                    WorkflowOperationAttempt.status == "running",
                )
            ).scalar_one()
            record_agent_lifecycle_event(
                session=session,
                event_type="TASK_STARTED",
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                run_id=run_id,
                issue_key="TP-611",
                agent_id="worker:test",
                recorded_at=now,
            )
            emit_logging_pane_event(
                session=session,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                workflow_id=workflow_id,
                operation_id=operation.operation_id,
                attempt_id=attempt.attempt_id,
                run_id=run_id,
                issue_key="TP-611",
                agent_id="worker:test",
                invocation_id="inv-run-temporal-boundary",
                channel="worker",
                command="workflow.dev",
                working_dir="/tmp/repo",
                stage="dev",
                attempt=1,
                stream="stdout",
                message="temporal activity emitted run log",
                recorded_at=now,
            )
            run.status = "succeeded"
            run.finished_at = now
            session.flush()
            return run

        with ExitStack() as stack:
            configure_product_event_repository_for_tests(repository)
            stack.callback(reset_product_event_repository_for_tests)
            stack.enter_context(
                patch("orchestrator.temporal.activities.run_execution.build_workflow_runner_for_session", return_value=object())
            )
            stack.enter_context(
                patch("orchestrator.temporal.activities.run_execution.process_claimed_run", side_effect=_process_claimed_run)
            )
            result = execute_claimed_run_activity(
                DevelopmentTeamRunWorkflowInput(
                    workflow_id=workflow_id,
                    run_id=run_id,
                    claim_id=claim_id,
                    tenant_id="tenant-a",
                    project_id="tenant-a-default",
                    issue_key="TP-611",
                    workflow_execution_timeout_seconds=86400,
                    workflow_run_timeout_seconds=43200,
                    activity_start_to_close_timeout_seconds=300,
                    human_input_resume_timeout_seconds=600,
                )
            )

            with session_factory() as session:
                operation = session.execute(
                    select(WorkflowOperation).where(
                        WorkflowOperation.workflow_id == workflow_id,
                        WorkflowOperation.operation_type == ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
                    )
                ).scalar_one()
                attempts = session.execute(
                    select(WorkflowOperationAttempt).where(
                        WorkflowOperationAttempt.operation_id == operation.operation_id
                    )
                ).scalars().all()
                stream = stream_run_events_ndjson(
                    session=session,
                    run_id=run_id,
                    run_model=Run,
                    settings=get_settings(),
                )
                rows = [json.loads(next(stream)), json.loads(next(stream))]

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].status, "completed")
        self.assertEqual({row.get("run_id") for row in rows}, {run_id})
        self.assertTrue(any(row.get("event_type") == "TASK_STARTED" for row in rows))
        self.assertTrue(any(row.get("event_kind") == "runtime_log" for row in rows))
        self.assertTrue(any(row.get("message") == "temporal activity emitted run log" for row in rows))
