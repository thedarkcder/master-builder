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
from orchestrator.temporal.activities.run_execution import (
    _WorkflowOperationAttemptHeartbeatController,
    execute_claimed_run_activity,
    resume_human_input_activity,
)
from orchestrator.temporal.payloads import DevelopmentTeamRunWorkflowInput, HumanInputResumeInput
from tests.test_support.admin_api_harness import AdminApiTestHarness
from tests.test_support.jira_parent_workflow_boundary import JiraParentWorkflowBoundaryHarness
from tests.test_support.jira_webhook_api_harness import JiraWebhookTestsHarness
from tests.test_support.product_events import RecordingProductEventRepository
from tests.workflow_test_support import add_human_input_request, add_workflow_attempt


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
            self.assertIs(selection.claimed_run.run, selection.run)
            self.assertEqual(selection.claimed_run.claim_id, claim_id)
            self.assertEqual(selection.claimed_run.worker_service_instance_id, "worker:test")
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

    def test_run_temporal_activity_failure_terminalizes_dispatching_run(self) -> None:
        self._insert_jira_connection()
        tenant_response = self.client.post(
            "/api/admin/tenants",
            json=self._tenant_payload(),
            auth=("admin", "secret"),
        )
        self.assertEqual(tenant_response.status_code, 201)
        now = datetime.now(timezone.utc)
        claim_id = "claim-run-temporal-failure"
        workflow_id = "workflow-run-temporal-failure"
        run_id = "run-temporal-failure"
        session_factory = create_session_factory(self.database_url)

        with session_factory() as session:
            add_workflow_attempt(
                session,
                workflow_id=workflow_id,
                run_id=run_id,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-612",
                issue_summary="Temporal failure boundary",
                issue_description="Prove activity failure terminalizes run.",
                repo_url="https://github.com/example/repo",
                branch="feature/temporal-failure",
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

        def _process_claimed_run(**_kwargs):  # noqa: ANN003
            raise RuntimeError("dispatcher exploded")

        with (
            patch("orchestrator.temporal.activities.run_execution.build_workflow_runner_for_session", return_value=object()),
            patch("orchestrator.temporal.activities.run_execution.process_claimed_run", side_effect=_process_claimed_run),
        ):
            with self.assertRaisesRegex(RuntimeError, "dispatcher exploded"):
                execute_claimed_run_activity(
                    DevelopmentTeamRunWorkflowInput(
                        workflow_id=workflow_id,
                        run_id=run_id,
                        claim_id=claim_id,
                        tenant_id="tenant-a",
                        project_id="tenant-a-default",
                        issue_key="TP-612",
                        workflow_execution_timeout_seconds=86400,
                        workflow_run_timeout_seconds=43200,
                        activity_start_to_close_timeout_seconds=300,
                        human_input_resume_timeout_seconds=600,
                    )
                )

        with session_factory() as session:
            run = session.get(Run, run_id)
            assert run is not None
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow_id,
                    WorkflowOperation.operation_type == ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
                )
            ).scalar_one()

        self.assertEqual(run.status, "failed")
        self.assertIsNone(run.claim_id)
        self.assertIn("Temporal run activity failed: dispatcher exploded", run.last_error or "")
        self.assertEqual(operation.status, "failed")

    def test_run_temporal_activity_reclaims_queued_retry_and_does_not_reuse_old_work_unit(self) -> None:
        self._insert_jira_connection()
        tenant_response = self.client.post(
            "/api/admin/tenants",
            json=self._tenant_payload(),
            auth=("admin", "secret"),
        )
        self.assertEqual(tenant_response.status_code, 201)
        now = datetime.now(timezone.utc)
        claim_id = "claim-run-temporal-requeue"
        workflow_id = "workflow-run-temporal-requeue"
        run_id = "run-temporal-requeue"
        session_factory = create_session_factory(self.database_url)

        with session_factory() as session:
            add_workflow_attempt(
                session,
                workflow_id=workflow_id,
                run_id=run_id,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-615",
                issue_summary="Temporal requeue boundary",
                issue_description="Prove queued retry results are not reused as terminal workflow output.",
                repo_url="https://github.com/example/repo",
                branch="feature/temporal-requeue",
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

        process_calls: list[tuple[str, str, str]] = []

        def _process_claimed_run(*, session, selection, **_kwargs):  # noqa: ANN001, ANN003
            claimed = selection.claimed_run
            process_calls.append((str(claimed.run_id), str(claimed.claim_id), str(claimed.worker_service_instance_id)))
            run = selection.run
            if len(process_calls) == 1:
                self.assertEqual(claimed.claim_id, claim_id)
                self.assertEqual(claimed.worker_service_instance_id, "worker:test")
                run.status = "queued"
                run.claim_id = None
                run.worker_service_instance_id = None
                run.dispatch_claimed_at = None
                run.last_heartbeat_at = None
                session.flush()
                return run
            self.assertEqual(claimed.worker_service_instance_id, f"temporal:{workflow_id}")
            self.assertTrue(claimed.claim_id)
            self.assertNotEqual(claimed.claim_id, claim_id)
            run.status = "succeeded"
            run.finished_at = now
            session.flush()
            return run

        payload = DevelopmentTeamRunWorkflowInput(
            workflow_id=workflow_id,
            run_id=run_id,
            claim_id=claim_id,
            tenant_id="tenant-a",
            project_id="tenant-a-default",
            issue_key="TP-615",
            workflow_execution_timeout_seconds=86400,
            workflow_run_timeout_seconds=43200,
            activity_start_to_close_timeout_seconds=300,
            human_input_resume_timeout_seconds=600,
        )

        with (
            patch("orchestrator.temporal.activities.run_execution.build_workflow_runner_for_session", return_value=object()),
            patch("orchestrator.temporal.activities.run_execution.process_claimed_run", side_effect=_process_claimed_run),
        ):
            first_result = execute_claimed_run_activity(payload)
            second_result = execute_claimed_run_activity(payload)

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

        self.assertEqual(first_result.status, "queued")
        self.assertEqual(second_result.status, "succeeded")
        self.assertEqual(len(process_calls), 2)
        self.assertEqual(len(attempts), 2)
        self.assertTrue(all(attempt.status == "completed" for attempt in attempts))
        self.assertEqual(operation.status, "completed")

    def test_resume_temporal_activity_processes_existing_temporal_claimed_resume_run(self) -> None:
        self._insert_jira_connection()
        tenant_response = self.client.post(
            "/api/admin/tenants",
            json=self._tenant_payload(),
            auth=("admin", "secret"),
        )
        self.assertEqual(tenant_response.status_code, 201)
        now = datetime.now(timezone.utc)
        workflow_id = "workflow-resume-temporal-existing-claim"
        source_run_id = "source-run-existing-claim"
        resume_run_id = "resume-run-existing-claim"
        request_id = "input-existing-claim"
        claim_id = "claim-existing-resume"
        worker_owner = f"temporal:{workflow_id}"
        session_factory = create_session_factory(self.database_url)

        with session_factory() as session:
            add_workflow_attempt(
                session,
                workflow_id=workflow_id,
                run_id=source_run_id,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-613",
                issue_summary="Temporal resume boundary",
                workflow_type_key="issue_execution",
                workflow_status="failed",
                run_status="failed",
                attempt_number=1,
                entry_checkpoint_id="checkpoint-existing-claim",
                checkpoint_kind="stage",
                checkpoint_stage="pm",
                checkpoint_payload={},
                pre_check_outcome="ready_for_agent",
                now=now,
            )
            resume_run = Run(
                run_id=resume_run_id,
                workflow_id=workflow_id,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-613",
                issue_summary="Temporal resume boundary",
                attempt_number=2,
                parent_run_id=source_run_id,
                entry_mode="resume",
                entry_stage="pm",
                entry_checkpoint_id="checkpoint-existing-claim",
                dedupe_scope="issue_execution",
                status="dispatching",
                pre_check_outcome="ready_for_agent",
                claim_id=claim_id,
                dispatch_claimed_at=now,
                worker_service_instance_id=worker_owner,
                created_at=now,
            )
            session.add(resume_run)
            add_human_input_request(
                session,
                request_id=request_id,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                workflow_id=workflow_id,
                checkpoint_id="checkpoint-existing-claim",
                source_run_id=source_run_id,
                consumed_by_run_id=resume_run_id,
                issue_key="TP-613",
                source_stage="pm",
                request_type="install_request",
                status="consumed",
                answered_at=now,
                answer_source_ref="test",
                now=now,
            )
            session.commit()

        def _process_claimed_run(**kwargs):  # noqa: ANN003
            selection = kwargs["selection"]
            self.assertEqual(selection.claimed_run.run_id, resume_run_id)
            self.assertEqual(selection.claimed_run.claim_id, claim_id)
            self.assertEqual(selection.claimed_run.worker_service_instance_id, worker_owner)
            selection.claimed_run.run.status = "succeeded"
            selection.claimed_run.run.finished_at = now
            return selection.claimed_run.run

        with (
            patch("orchestrator.temporal.activities.run_execution.build_workflow_runner_for_session", return_value=object()),
            patch("orchestrator.temporal.activities.run_execution.process_claimed_run", side_effect=_process_claimed_run),
        ):
            result = resume_human_input_activity(HumanInputResumeInput(request_id=request_id))

        self.assertEqual(result.run_id, resume_run_id)
        self.assertEqual(result.status, "succeeded")

        with session_factory() as session:
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow_id,
                    WorkflowOperation.operation_type == "human_input_resume",
                )
            ).scalar_one()
            self.assertEqual(operation.run_id, resume_run_id)
            self.assertEqual(operation.status, "completed")

    def test_operation_attempt_heartbeat_controller_touches_active_attempt(self) -> None:
        self._insert_jira_connection()
        tenant_response = self.client.post(
            "/api/admin/tenants",
            json=self._tenant_payload(),
            auth=("admin", "secret"),
        )
        self.assertEqual(tenant_response.status_code, 201)
        now = datetime.now(timezone.utc)
        workflow_id = "workflow-operation-heartbeat"
        run_id = "run-operation-heartbeat"
        session_factory = create_session_factory(self.database_url)

        with session_factory() as session:
            add_workflow_attempt(
                session,
                workflow_id=workflow_id,
                run_id=run_id,
                tenant_id="tenant-a",
                project_id="tenant-a-default",
                issue_key="TP-614",
                issue_summary="Operation heartbeat",
                workflow_type_key="issue_execution",
                workflow_status="running",
                run_status="dispatching",
                attempt_number=1,
                claim_id="claim-operation-heartbeat",
                worker_service_instance_id="worker:test",
                dispatch_claimed_at=now,
                now=now,
            )
            operation = WorkflowOperation(
                operation_id="operation-heartbeat",
                workflow_id=workflow_id,
                run_id=run_id,
                operation_type="run_attempt_execution",
                idempotency_key="run-attempt:run-operation-heartbeat",
                status="running",
                target_system="workflow_engine",
                target_ref=run_id,
                summary="Execute run",
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            attempt = WorkflowOperationAttempt(
                attempt_id="attempt-heartbeat",
                operation_id=operation.operation_id,
                attempt_number=1,
                status="running",
                error_category=None,
                error_message=None,
                retryable=False,
                next_retry_at=None,
                status_detail=None,
                last_heartbeat_at=now,
                lease_expires_at=now,
                lease_owner="old-owner",
                created_at=now,
                started_at=now,
                finished_at=None,
            )
            session.add_all([operation, attempt])
            session.commit()

        controller = _WorkflowOperationAttemptHeartbeatController(
            database_url=self.database_url,
            attempt_id="attempt-heartbeat",
            lease_owner="new-owner",
            heartbeat_interval_seconds=5,
        )
        controller._run_once()

        with session_factory() as session:
            attempt = session.get(WorkflowOperationAttempt, "attempt-heartbeat")
            assert attempt is not None
            self.assertEqual(attempt.lease_owner, "new-owner")
            self.assertIsNotNone(attempt.lease_expires_at)
            self.assertNotEqual(attempt.lease_expires_at, now)
