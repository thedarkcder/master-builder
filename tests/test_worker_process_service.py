from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from orchestrator.core.worker.process_service import process_next_queued_run
from orchestrator.core.worker.finalization import _emit_detailed_jira_feedback
from orchestrator.core.worker.finalization import _emit_orchestrated_trace_logs
from orchestrator.core.worker.finalization import _run_completion_step
from orchestrator.core.worker.stage_notifier import RunStageNotifier
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import (
    PmPlan,
    WorkflowDiagnostics,
    WorkflowResult,
    WorkflowStageCheckpoint,
)


class _FakeHeartbeatController:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class WorkerProcessServiceTests(unittest.TestCase):
    def test_process_next_queued_run_fails_when_dispatching_run_cannot_promote(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="dispatching",
            created_at=now - timedelta(seconds=15),
            started_at=None,
            last_heartbeat_at=None,
            worker_service_instance_id="node-a:1234",
            claim_id="claim-1",
            plan=None,
            last_error=None,
            finished_at=None,
            pr_url=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        session = MagicMock()
        fail_guardrail_violation_fn = MagicMock(
            return_value=SimpleNamespace(run_id="run-1", status="failed", last_error="failed")
        )

        result = process_next_queued_run(
            session=session,
            runner=MagicMock(),
            logger=MagicMock(),
            settings_fn=lambda: SimpleNamespace(
                worker_capabilities="linux",
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
            ),
            claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                run=run,
                tenant=tenant,
                project=project,
                effective_policy={"allow_jira_transitions": True},
                terminal_run=None,
            ),
            send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
            send_jira_message_fn=MagicMock(),
            resolve_project_for_run_fn=MagicMock(return_value=project),
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            build_run_heartbeat_controller_fn=lambda **_: _FakeHeartbeatController(),
            promote_run_to_running_fn=MagicMock(return_value=None),
            bind_run_project_fn=MagicMock(return_value=run),
            workflow_request_for_run_fn=MagicMock(
                return_value=SimpleNamespace(
                    start_point_ref=None,
                    start_point_sha=None,
                    execution_repo_dir="/tmp/workdirs/repo",
                    workspace_key="worker-a",
                    execution_branch="feature/test",
                    integration_branch="feature/test",
                    base_branch="main",
                )
            ),
            fail_guardrail_violation_fn=fail_guardrail_violation_fn,
            tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.example/browse/GP-122"),
            lock_acquired_update_fn=MagicMock(
                return_value={"stage": "lock_acquired", "discord_message": None, "jira_message": None}
            ),
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=MagicMock(),
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=MagicMock(),
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertEqual(result.status, "failed")
        fail_guardrail_violation_fn.assert_called_once_with(
            session,
            run=run,
            error="Claimed run could not transition from dispatching to running",
        )

    def test_process_next_queued_run_promotes_before_setup_validation(self) -> None:
        now = datetime.now(timezone.utc)
        order: list[str] = []
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="dispatching",
            created_at=now - timedelta(seconds=15),
            started_at=None,
            last_heartbeat_at=None,
            worker_service_instance_id="node-a:1234",
            claim_id="claim-1",
            plan=None,
            last_error=None,
            finished_at=None,
            pr_url=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        session = MagicMock()

        def _promote(_session, *, run, expected_worker_service_instance_id, expected_claim_id):  # noqa: ANN001
            _ = expected_worker_service_instance_id
            _ = expected_claim_id
            order.append("promote")
            run.status = "running"
            run.started_at = now
            run.last_heartbeat_at = now
            return run

        def _resolve_project(_session, *, run):  # noqa: ANN001
            order.append("resolve_project")
            return project

        result = process_next_queued_run(
            session=session,
            runner=SimpleNamespace(
                run=MagicMock(
                    return_value=WorkflowResult(
                        outcome="success",
                        plan=None,
                        pr_url=None,
                        summary=[],
                        test_guidance=[],
                        attempts=1,
                    )
                )
            ),
            logger=MagicMock(),
            settings_fn=lambda: SimpleNamespace(
                worker_capabilities="linux",
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
            ),
            claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                run=run,
                tenant=tenant,
                project=None,
                effective_policy={"allow_jira_transitions": False},
                terminal_run=None,
            ),
            send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
            send_jira_message_fn=MagicMock(),
            resolve_project_for_run_fn=_resolve_project,
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            build_run_heartbeat_controller_fn=lambda **_: _FakeHeartbeatController(),
            promote_run_to_running_fn=_promote,
            bind_run_project_fn=MagicMock(return_value=run),
            workflow_request_for_run_fn=MagicMock(
                side_effect=lambda *_args, **_kwargs: (
                    order.append("build_request")
                    or SimpleNamespace(
                        start_point_ref=None,
                        start_point_sha=None,
                        execution_repo_dir="/tmp/workdirs/repo",
                        workspace_key="worker-a",
                        execution_branch="feature/test",
                        integration_branch="feature/test",
                        base_branch="main",
                    )
                )
            ),
            fail_guardrail_violation_fn=MagicMock(),
            tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.example/browse/GP-122"),
            lock_acquired_update_fn=MagicMock(
                return_value={"stage": "lock_acquired", "discord_message": None, "jira_message": None}
            ),
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=MagicMock(),
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=MagicMock(
                return_value=SimpleNamespace(
                    run_id=run.run_id,
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    status="succeeded",
                    last_error=None,
                    plan=None,
                )
            ),
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(order[:3], ["promote", "resolve_project", "build_request"])

    def test_process_next_queued_run_fails_when_promotion_returns_non_running_owned_run(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="dispatching",
            created_at=now - timedelta(seconds=15),
            started_at=None,
            last_heartbeat_at=None,
            worker_service_instance_id="node-a:1234",
            claim_id="claim-1",
            plan=None,
            last_error=None,
            finished_at=None,
            pr_url=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        session = MagicMock()
        fail_guardrail_violation_fn = MagicMock(
            return_value=SimpleNamespace(run_id="run-1", status="failed", last_error="failed")
        )
        runner = MagicMock()

        def _promote(_session, *, run, expected_worker_service_instance_id, expected_claim_id):  # noqa: ANN001
            self.assertEqual(expected_worker_service_instance_id, "node-a:1234")
            self.assertEqual(expected_claim_id, "claim-1")
            return run

        result = process_next_queued_run(
            session=session,
            runner=runner,
            logger=MagicMock(),
            settings_fn=lambda: SimpleNamespace(
                worker_capabilities="linux",
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
            ),
            claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                run=run,
                tenant=tenant,
                project=project,
                effective_policy={"allow_jira_transitions": True},
                terminal_run=None,
            ),
            send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
            send_jira_message_fn=MagicMock(),
            resolve_project_for_run_fn=MagicMock(return_value=project),
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            build_run_heartbeat_controller_fn=lambda **_: _FakeHeartbeatController(),
            promote_run_to_running_fn=_promote,
            bind_run_project_fn=MagicMock(return_value=run),
            workflow_request_for_run_fn=MagicMock(),
            fail_guardrail_violation_fn=fail_guardrail_violation_fn,
            tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.example/browse/GP-122"),
            lock_acquired_update_fn=MagicMock(),
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=MagicMock(),
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=MagicMock(),
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertEqual(result.status, "failed")
        fail_guardrail_violation_fn.assert_called_once_with(
            session,
            run=run,
            error="Claimed run could not transition from dispatching to running",
        )
        runner.run.assert_not_called()

    def test_process_next_queued_run_returns_ownership_lost_when_promotion_loses_claim(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="dispatching",
            created_at=now - timedelta(seconds=15),
            started_at=None,
            last_heartbeat_at=None,
            worker_service_instance_id="node-a:1234",
            claim_id="claim-1",
            plan=None,
            last_error=None,
            finished_at=None,
            pr_url=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        session = MagicMock()
        ownership_lost_run = SimpleNamespace(
            **{
                **run.__dict__,
                "status": "dispatching",
                "worker_service_instance_id": "node-b:9999",
                "claim_id": "claim-2",
            }
        )
        runner = MagicMock()
        heartbeat = _FakeHeartbeatController()

        result = process_next_queued_run(
            session=session,
            runner=runner,
            logger=MagicMock(),
            settings_fn=lambda: SimpleNamespace(
                worker_capabilities="linux",
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
            ),
            claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                run=run,
                tenant=tenant,
                project=project,
                effective_policy={"allow_jira_transitions": True},
                terminal_run=None,
            ),
            send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
            send_jira_message_fn=MagicMock(),
            resolve_project_for_run_fn=MagicMock(return_value=project),
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            build_run_heartbeat_controller_fn=lambda **_: heartbeat,
            promote_run_to_running_fn=MagicMock(return_value=ownership_lost_run),
            bind_run_project_fn=MagicMock(return_value=run),
            workflow_request_for_run_fn=MagicMock(),
            fail_guardrail_violation_fn=MagicMock(),
            tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.example/browse/GP-122"),
            lock_acquired_update_fn=MagicMock(),
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=MagicMock(),
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=MagicMock(),
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertIs(result, ownership_lost_run)
        self.assertFalse(heartbeat.started)
        runner.run.assert_not_called()

    def test_process_next_queued_run_emits_started_side_effects_after_running_promotion(self) -> None:
        now = datetime.now(timezone.utc)
        order: list[str] = []
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="dispatching",
            created_at=now - timedelta(seconds=15),
            started_at=None,
            last_heartbeat_at=None,
            worker_service_instance_id="node-a:1234",
            claim_id="claim-1",
            plan=None,
            last_error=None,
            finished_at=None,
            pr_url=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        heartbeat = _FakeHeartbeatController()
        session = MagicMock()

        def _refresh(target, **_kwargs) -> None:  # noqa: ANN001
            _ = target

        session.refresh.side_effect = _refresh

        def _promote(_session, *, run, expected_worker_service_instance_id, expected_claim_id):  # noqa: ANN001
            self.assertEqual(expected_worker_service_instance_id, "node-a:1234")
            self.assertEqual(expected_claim_id, "claim-1")
            order.append("promote")
            run.status = "running"
            run.started_at = now
            run.last_heartbeat_at = now
            return run

        def _transition_issue_status(**_kwargs):  # noqa: ANN001
            order.append("jira")

        def _emit_agent_event(**kwargs):  # noqa: ANN001
            if kwargs.get("event_type") == "TASK_STARTED":
                order.append("task_started")

        finalize_workflow_result_fn = MagicMock(
            return_value=SimpleNamespace(
                run_id=run.run_id,
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                status="succeeded",
                last_error=None,
                plan=None,
            )
        )

        with patch("orchestrator.core.worker.process_service.record_run_log_event") as record_run_log_event_mock:
            result = process_next_queued_run(
                session=session,
                runner=SimpleNamespace(
                    run=MagicMock(
                        return_value=WorkflowResult(
                            outcome="success",
                            plan=None,
                            pr_url=None,
                            summary=[],
                            test_guidance=[],
                            attempts=1,
                        )
                    )
                ),
                logger=MagicMock(),
                settings_fn=lambda: SimpleNamespace(
                    worker_capabilities="linux",
                    worker_workspace_key="worker-a",
                    project_repo_checkout_base_dir="/tmp/workdirs",
                    admin_ui_base_url="http://localhost:4100",
                    worker_run_heartbeat_interval_seconds=30,
                ),
                claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                    run=run,
                    tenant=tenant,
                    project=project,
                    effective_policy={"allow_jira_transitions": True},
                    terminal_run=None,
                ),
                send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
                send_jira_message_fn=MagicMock(),
                resolve_project_for_run_fn=MagicMock(return_value=project),
                fail_missing_project_mapping_fn=MagicMock(),
                block_archived_project_fn=MagicMock(),
                ensure_project_repository_checkout_fn=MagicMock(),
                fail_project_repository_checkout_fn=MagicMock(),
                cleanup_run_workspaces_fn=MagicMock(),
                build_run_heartbeat_controller_fn=lambda **_: heartbeat,
                promote_run_to_running_fn=_promote,
                bind_run_project_fn=MagicMock(return_value=run),
                workflow_request_for_run_fn=MagicMock(
                    return_value=SimpleNamespace(
                        start_point_ref=None,
                        start_point_sha=None,
                        execution_repo_dir="/tmp/workdirs/repo",
                        workspace_key="worker-a",
                        execution_branch="feature/test",
                        integration_branch="feature/test",
                        base_branch="main",
                    )
                ),
                fail_guardrail_violation_fn=MagicMock(),
                tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.example/browse/GP-122"),
                lock_acquired_update_fn=MagicMock(
                    return_value={"stage": "lock_acquired", "discord_message": None, "jira_message": None}
                ),
                plan_posted_update_fn=MagicMock(
                    return_value={"stage": "plan_posted", "discord_message": None, "jira_message": None}
                ),
                pr_opened_update_fn=MagicMock(
                    return_value={"stage": "pr_opened", "discord_message": None, "jira_message": None}
                ),
                run_failed_update_fn=MagicMock(
                    return_value={"stage": "run_failed", "discord_message": None, "jira_message": None}
                ),
                run_requeued_capability_update_fn=MagicMock(
                    return_value={"stage": "run_requeued_capability", "discord_message": None, "jira_message": None}
                ),
                run_requeued_stale_snapshot_update_fn=MagicMock(
                    return_value={
                        "stage": "run_requeued_stale_snapshot",
                        "discord_message": None,
                        "jira_message": None,
                    }
                ),
                finalize_cancelled_run_fn=MagicMock(),
                finalize_workflow_result_fn=finalize_workflow_result_fn,
                persist_stage_checkpoint_fn=MagicMock(),
                requeue_workflow_result_for_capability_fn=MagicMock(),
                requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
                check_run_snapshot_freshness_fn=MagicMock(return_value=SimpleNamespace(stale=False, message=None)),
                transition_issue_status_fn=_transition_issue_status,
                emit_agent_event_fn=_emit_agent_event,
                resolve_agent_id_fn=lambda: "worker-linux-local",
                resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
                run_status_queued="queued",
                run_status_running="running",
                run_status_failed="failed",
                run_status_blocked="blocked",
                run_status_cancelled="cancelled",
            )

        self.assertEqual(result.status, "succeeded")
        self.assertEqual(order, ["promote", "jira", "task_started"])
        self.assertTrue(heartbeat.started)
        queue_wait_calls = [
            call
            for call in record_run_log_event_mock.call_args_list
            if call.kwargs.get("command") == "workflow.queue_wait"
        ]
        self.assertEqual(len(queue_wait_calls), 1)
        queue_wait_payload = json.loads(queue_wait_calls[0].kwargs["message"])
        self.assertEqual(queue_wait_payload["event_kind"], "queue_wait")
        self.assertEqual(queue_wait_payload["started_at"], now.isoformat())

    def test_process_next_queued_run_rejects_invalid_worker_capability_settings(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid worker capability token\\(s\\)"):
            process_next_queued_run(
                session=MagicMock(),
                runner=MagicMock(),
                logger=MagicMock(),
                settings_fn=lambda: SimpleNamespace(
                    worker_capabilities="linux,darwin",
                    worker_workspace_key="worker-a",
                    project_repo_checkout_base_dir="/tmp/workdirs",
                    admin_ui_base_url="http://localhost:4100",
                ),
                claim_next_queued_run_fn=MagicMock(),
                send_discord_message_fn=MagicMock(),
                send_jira_message_fn=MagicMock(),
                resolve_project_for_run_fn=MagicMock(),
                fail_missing_project_mapping_fn=MagicMock(),
                block_archived_project_fn=MagicMock(),
                ensure_project_repository_checkout_fn=MagicMock(),
                fail_project_repository_checkout_fn=MagicMock(),
                cleanup_run_workspaces_fn=MagicMock(),
                build_run_heartbeat_controller_fn=lambda **_: _FakeHeartbeatController(),
                bind_run_project_fn=MagicMock(),
                workflow_request_for_run_fn=MagicMock(),
                fail_guardrail_violation_fn=MagicMock(),
                tenant_jira_issue_url_fn=MagicMock(),
                lock_acquired_update_fn=MagicMock(),
                plan_posted_update_fn=MagicMock(),
                pr_opened_update_fn=MagicMock(),
                run_failed_update_fn=MagicMock(),
                run_requeued_capability_update_fn=MagicMock(),
                run_requeued_stale_snapshot_update_fn=MagicMock(),
                finalize_cancelled_run_fn=MagicMock(),
                finalize_workflow_result_fn=MagicMock(),
                persist_stage_checkpoint_fn=MagicMock(),
                requeue_workflow_result_for_capability_fn=MagicMock(),
                requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
                check_run_snapshot_freshness_fn=MagicMock(),
                transition_issue_status_fn=MagicMock(),
                emit_agent_event_fn=MagicMock(),
                resolve_agent_id_fn=lambda: "worker-linux-local",
                resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
                run_status_queued="queued",
                run_status_running="running",
                run_status_failed="failed",
                run_status_blocked="blocked",
                run_status_cancelled="cancelled",
            )

    def test_stage_notifier_refreshes_plan_before_appending_live_updates(self) -> None:
        run = SimpleNamespace(
            plan={},
            tenant_id="tenant-1",
            project_id="project-1",
            run_id="run-1",
            issue_key="GP-122",
        )
        session = MagicMock()
        refresh_calls: list[object] = []

        def refresh(target, attribute_names=None):  # noqa: ANN001
            self.assertIs(target, run)
            refresh_calls.append(attribute_names)
            if attribute_names == ["plan"]:
                run.plan = ExecutionSnapshot.empty(trigger_context={"source": "manual"}).dump()
                return
            self.assertIsNone(attribute_names)

        session.refresh.side_effect = refresh
        notifier = RunStageNotifier(
            session=session,
            tenant=SimpleNamespace(),
            run=run,
            settings=SimpleNamespace(),
            project=None,
            send_discord_message=lambda **_: SimpleNamespace(sent=True, reason=None),
            send_jira_message=lambda **_: None,
        )

        notifier.append(
            {
                "stage": "plan_posted",
                "discord_message": "Plan posted",
                "jira_message": "Plan posted",
            }
        )

        self.assertEqual(run.plan["context"]["trigger_context"], {"source": "manual"})
        self.assertEqual(len(run.plan["events"]["live_stage_updates"]), 1)
        self.assertEqual(run.plan["events"]["live_stage_updates"][0]["stage"], "plan_posted")
        session.commit.assert_called_once()
        self.assertEqual(refresh_calls, [["plan"], None])

    def test_emit_orchestrated_trace_logs_persists_stage_rows(self) -> None:
        recorded_rows: list[dict[str, object]] = []

        def _record_run_log_event(**kwargs: object) -> None:
            recorded_rows.append(dict(kwargs))

        workflow_result = WorkflowResult(
            outcome="blocked",
            plan=None,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=1,
            orchestration_stage_trace=[
                {"stage": "pm", "status": "completed", "summary": "PM finished", "attempt": 1},
                {"stage": "dev", "status": "blocked", "summary": "Dev blocked", "attempt": 2},
            ],
            orchestration_workstream_trace=[],
        )

        with patch("orchestrator.core.worker.finalization.record_run_log_event", side_effect=_record_run_log_event):
            _emit_orchestrated_trace_logs(
                session=MagicMock(),
                run=SimpleNamespace(
                    tenant_id="tenant-1",
                    project_id="project-1",
                    run_id="run-1",
                    issue_key="GP-122",
                ),
                workflow_result=workflow_result,
                agent_id="worker-linux",
            )

        stage_rows = [
            row
            for row in recorded_rows
            if row.get("stage") in {"pm", "dev", "test", "review"}
        ]
        self.assertEqual({str(row.get("stage")) for row in stage_rows}, {"pm", "dev"})
        self.assertTrue(all(str(row.get("stream")) == "system" for row in stage_rows))
        self.assertTrue(all(str(row.get("command", "")).startswith("workflow.") for row in stage_rows))
        parsed_messages = [json.loads(str(row.get("message") or "{}")) for row in stage_rows]
        self.assertTrue(all(msg.get("event_kind") == "orchestrated_stage_event" for msg in parsed_messages))

    def test_emit_detailed_jira_feedback_sends_dev_and_review_comments(self) -> None:
        send_jira = MagicMock()
        workflow_result = WorkflowResult(
            outcome="blocked",
            plan=None,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=1,
            dev_rationale=["Implemented onboarding flow", "Added splash navigation guard"],
            review_summary=["Needs state-machine transition fix"],
            review_feedback="Please remove timer-based progression.",
        )
        _emit_detailed_jira_feedback(
            session=MagicMock(),
            tenant=SimpleNamespace(),
            run=SimpleNamespace(run_id="run-1", issue_key="GP-1"),
            settings=SimpleNamespace(),
            workflow_result=workflow_result,
            send_jira_message_fn=send_jira,
        )

        stages = [call.kwargs["stage"] for call in send_jira.call_args_list]
        self.assertEqual(stages, ["dev_rationale", "review_summary", "review_feedback"])

    def test_run_completion_step_rolls_back_before_logging_failure(self) -> None:
        session = MagicMock()
        logger = MagicMock()
        failures: list[dict[str, str]] = []
        recorded_rows: list[dict[str, object]] = []

        def _record_run_log_event(**kwargs: object) -> None:
            recorded_rows.append(dict(kwargs))

        with patch("orchestrator.core.worker.finalization.record_run_log_event", side_effect=_record_run_log_event):
            _run_completion_step(
                session=session,
                run=SimpleNamespace(
                    tenant_id="tenant-1",
                    project_id="project-1",
                    run_id="run-1",
                    issue_key="GP-122",
                ),
                agent_id="worker-linux",
                logger=logger,
                step="jira_feedback",
                failures=failures,
                fn=MagicMock(side_effect=RuntimeError("jira publish failed")),
            )

        session.rollback.assert_called_once()
        self.assertEqual(failures, [{"step": "jira_feedback", "error_class": "RuntimeError", "error_message": "jira publish failed"}])
        recorded_messages = [json.loads(str(row["message"])) for row in recorded_rows if row.get("stage") == "telemetry"]
        self.assertTrue(
            any(
                message.get("event_kind") == "completion_step_failed"
                and message.get("step") == "jira_feedback"
                for message in recorded_messages
            )
        )

    def test_process_next_queued_run_uses_claimed_run_from_claim_service(self) -> None:
        run = SimpleNamespace(run_id="run-1", tenant_id="tenant-1", issue_key="GP-122", project_id="project-1")
        tenant = SimpleNamespace(tenant_id="tenant-1")
        selection = SimpleNamespace(
            run=run,
            tenant=tenant,
            terminal_run=None,
        )
        claim_next_queued_run_fn = MagicMock(return_value=selection)
        failed_run = SimpleNamespace(run_id="failed-run")
        fail_missing_project_mapping_fn = MagicMock(return_value=failed_run)

        result = process_next_queued_run(
            session=MagicMock(),
            runner=MagicMock(),
            logger=MagicMock(),
            settings_fn=lambda: SimpleNamespace(
                worker_capabilities="linux",
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
            ),
            claim_next_queued_run_fn=claim_next_queued_run_fn,
            send_discord_message_fn=MagicMock(),
            send_jira_message_fn=MagicMock(),
            resolve_project_for_run_fn=MagicMock(return_value=None),
            fail_missing_project_mapping_fn=fail_missing_project_mapping_fn,
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            build_run_heartbeat_controller_fn=lambda **_: _FakeHeartbeatController(),
            bind_run_project_fn=MagicMock(),
            workflow_request_for_run_fn=MagicMock(),
            fail_guardrail_violation_fn=MagicMock(),
            tenant_jira_issue_url_fn=MagicMock(),
            lock_acquired_update_fn=MagicMock(),
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=MagicMock(),
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=MagicMock(),
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertIs(result, failed_run)
        claim_next_queued_run_fn.assert_called_once()
        fail_missing_project_mapping_fn.assert_called_once()
        claim_kwargs = claim_next_queued_run_fn.call_args.kwargs
        self.assertEqual(claim_kwargs["worker_service_instance_id"], "node-a:1234")

    def test_process_next_queued_run_returns_run_when_ownership_is_lost(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            tenant="tenant-1",
            status="failed",
            worker_service_instance_id="node-b:9999",
            created_at=now,
            started_at=now,
            plan=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        heartbeat = _FakeHeartbeatController()
        session = MagicMock()

        def _refresh(target, **_kwargs) -> None:  # noqa: ANN001
            _ = target

        session.refresh.side_effect = _refresh

        result = process_next_queued_run(
            session=session,
            runner=SimpleNamespace(
                run=MagicMock(
                    return_value=WorkflowResult(
                        outcome="success",
                        plan=None,
                        pr_url=None,
                        summary=[],
                        test_guidance=[],
                        attempts=1,
                    )
                )
            ),
            logger=MagicMock(),
            settings_fn=lambda: SimpleNamespace(
                worker_capabilities="linux",
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
                tenant_id="tenant-1",
            ),
            claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                run=run,
                tenant=tenant,
                terminal_run=None,
            ),
            send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
            send_jira_message_fn=MagicMock(),
            resolve_project_for_run_fn=MagicMock(return_value=project),
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            build_run_heartbeat_controller_fn=lambda **_: heartbeat,
            bind_run_project_fn=MagicMock(),
            workflow_request_for_run_fn=MagicMock(return_value=SimpleNamespace(start_point_ref=None, start_point_sha=None)),
            fail_guardrail_violation_fn=MagicMock(),
            tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.test/GP-122"),
            lock_acquired_update_fn=MagicMock(
                return_value={
                    "stage": "lock_acquired",
                    "discord_message": "locked",
                    "jira_message": "locked",
                }
            ),
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=MagicMock(),
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=MagicMock(),
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertIs(result, run)
        self.assertTrue(heartbeat.started)
        self.assertTrue(heartbeat.stopped)

    def test_process_next_queued_run_publishes_manual_remediation_completion_before_finalize_with_terminal_status(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="running",
            worker_service_instance_id="node-a:1234",
            created_at=now,
            started_at=now,
            plan=ExecutionSnapshot.empty(
                trigger_context={
                    "manual_fix_request": {"instruction_text": "fix this"},
                    "requested_comment": {"type": "review_comment", "id": 77},
                }
            ).dump(),
            pr_url=None,
            last_error=None,
        )
        finalized_run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="succeeded",
            pr_url="https://github.com/org/repo/pull/10",
            plan=run.plan,
            last_error=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        heartbeat = _FakeHeartbeatController()
        session = MagicMock()
        session.refresh.side_effect = lambda _target, **_kwargs: None
        finalize_run = MagicMock(return_value=finalized_run)

        with patch("orchestrator.core.worker.finalization.publish_manual_pr_remediation_completion") as publish_completion:
            result = process_next_queued_run(
                session=session,
                runner=SimpleNamespace(
                    run=MagicMock(
                        return_value=WorkflowResult(
                            outcome="success",
                            plan=None,
                            pr_url="https://github.com/org/repo/pull/10",
                            summary=["Done"],
                            test_guidance=[],
                            attempts=1,
                        )
                    )
                ),
                logger=MagicMock(),
                settings_fn=lambda: SimpleNamespace(
                    worker_capabilities="linux",
                    worker_workspace_key="worker-a",
                    project_repo_checkout_base_dir="/tmp/workdirs",
                    admin_ui_base_url="http://localhost:4100",
                    worker_run_heartbeat_interval_seconds=30,
                    tenant_id="tenant-1",
                ),
                claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                    run=run,
                    tenant=tenant,
                    terminal_run=None,
                ),
                send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
                send_jira_message_fn=MagicMock(),
                resolve_project_for_run_fn=MagicMock(return_value=project),
                fail_missing_project_mapping_fn=MagicMock(),
                block_archived_project_fn=MagicMock(),
                ensure_project_repository_checkout_fn=MagicMock(),
                fail_project_repository_checkout_fn=MagicMock(),
                cleanup_run_workspaces_fn=MagicMock(),
                build_run_heartbeat_controller_fn=lambda **_: heartbeat,
                bind_run_project_fn=MagicMock(),
                workflow_request_for_run_fn=MagicMock(return_value=SimpleNamespace(start_point_ref=None, start_point_sha=None)),
                fail_guardrail_violation_fn=MagicMock(),
                tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.test/GP-122"),
                lock_acquired_update_fn=MagicMock(return_value={"stage": "lock_acquired", "discord_message": "locked", "jira_message": ""}),
                plan_posted_update_fn=MagicMock(),
                pr_opened_update_fn=MagicMock(),
                run_failed_update_fn=MagicMock(),
                run_requeued_capability_update_fn=MagicMock(),
                run_requeued_stale_snapshot_update_fn=MagicMock(),
                finalize_cancelled_run_fn=MagicMock(),
                finalize_workflow_result_fn=finalize_run,
                persist_stage_checkpoint_fn=MagicMock(),
                requeue_workflow_result_for_capability_fn=MagicMock(),
                requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
                check_run_snapshot_freshness_fn=MagicMock(),
                transition_issue_status_fn=MagicMock(),
                emit_agent_event_fn=MagicMock(),
                resolve_agent_id_fn=lambda: "worker-linux-local",
                resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
                run_status_queued="queued",
                run_status_running="running",
                run_status_failed="failed",
                run_status_blocked="blocked",
                run_status_cancelled="cancelled",
            )

        self.assertIs(result, finalized_run)
        publish_completion.assert_called_once()
        self.assertIs(publish_completion.call_args.kwargs["run"], finalized_run)
        self.assertEqual(publish_completion.call_args.kwargs["terminal_status"], "succeeded")
        self.assertTrue(heartbeat.started)
        self.assertTrue(heartbeat.stopped)

    def test_process_next_queued_run_keeps_heartbeat_running_until_finalize(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="running",
            worker_service_instance_id="node-a:1234",
            claim_id="claim-1",
            created_at=now,
            started_at=now,
            plan={},
            pr_url=None,
            last_error=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        heartbeat = _FakeHeartbeatController()
        session = MagicMock()
        session.refresh.side_effect = lambda _target, **_kwargs: None

        def _finalize(*_args, **_kwargs):  # noqa: ANN001
            self.assertFalse(heartbeat.stopped)
            return run

        result = process_next_queued_run(
            session=session,
            runner=SimpleNamespace(
                run=MagicMock(
                        return_value=WorkflowResult(
                        outcome="success",
                        plan=None,
                        pr_url=None,
                        summary=["Done"],
                        test_guidance=[],
                        attempts=1,
                    )
                )
            ),
            logger=MagicMock(),
            settings_fn=lambda: SimpleNamespace(
                worker_capabilities="linux",
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
                tenant_id="tenant-1",
            ),
            claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                run=run,
                tenant=tenant,
                terminal_run=None,
            ),
            send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
            send_jira_message_fn=MagicMock(),
            resolve_project_for_run_fn=MagicMock(return_value=project),
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            build_run_heartbeat_controller_fn=lambda **_: heartbeat,
            bind_run_project_fn=MagicMock(),
            workflow_request_for_run_fn=MagicMock(return_value=SimpleNamespace(start_point_ref=None, start_point_sha=None)),
            fail_guardrail_violation_fn=MagicMock(),
            tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.test/GP-122"),
            lock_acquired_update_fn=MagicMock(return_value={"stage": "lock_acquired", "discord_message": "locked", "jira_message": ""}),
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=MagicMock(),
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=_finalize,
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertIs(result, run)
        self.assertTrue(heartbeat.started)
        self.assertTrue(heartbeat.stopped)

    def test_process_next_queued_run_stops_on_waiting_for_input_without_failure_update(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-125",
            project_id="project-1",
            status="running",
            worker_service_instance_id="node-a:1234",
            created_at=now,
            started_at=now,
            plan={},
            pr_url=None,
            last_error=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        heartbeat = _FakeHeartbeatController()
        session = MagicMock()
        send_jira_message = MagicMock()
        cleanup = MagicMock()

        def _refresh(target, **_kwargs):  # noqa: ANN001
            if target is run:
                run.status = "waiting_for_input"

        session.refresh.side_effect = _refresh
        run_failed_update = MagicMock()
        finalize_run = MagicMock()

        result = process_next_queued_run(
            session=session,
            runner=SimpleNamespace(
                run=MagicMock(
                    return_value=WorkflowResult(
                        outcome="waiting_for_input",
                        plan=None,
                        pr_url=None,
                        summary=[],
                        test_guidance=[],
                        attempts=1,
                        blocker_message="Decision Gate clarification requested",
                        diagnostics=WorkflowDiagnostics(
                            stage="pm",
                            message="Decision Gate clarification requested",
                            attempts=1,
                            history=[],
                        ),
                    )
                )
            ),
            logger=MagicMock(),
            settings_fn=lambda: SimpleNamespace(
                worker_capabilities="linux",
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
                tenant_id="tenant-1",
            ),
            claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                run=run,
                tenant=tenant,
                terminal_run=None,
            ),
            send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
            send_jira_message_fn=send_jira_message,
            resolve_project_for_run_fn=MagicMock(return_value=project),
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=cleanup,
            build_run_heartbeat_controller_fn=lambda **_: heartbeat,
            bind_run_project_fn=MagicMock(),
            workflow_request_for_run_fn=MagicMock(return_value=SimpleNamespace(start_point_ref=None, start_point_sha=None)),
            fail_guardrail_violation_fn=MagicMock(),
            tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.test/GP-125"),
            lock_acquired_update_fn=MagicMock(return_value={"stage": "lock_acquired", "discord_message": "locked", "jira_message": ""}),
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=run_failed_update,
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=finalize_run,
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertIs(result, run)
        run_failed_update.assert_not_called()
        finalize_run.assert_not_called()
        self.assertEqual(send_jira_message.call_count, 1)
        self.assertEqual(send_jira_message.call_args.kwargs["stage"], "lock_acquired")
        cleanup.assert_not_called()
        self.assertTrue(heartbeat.started)
        self.assertTrue(heartbeat.stopped)

    def test_process_next_queued_run_persists_stage_checkpoint_before_finalize(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="running",
            worker_service_instance_id="node-a:1234",
            claim_id="claim-1",
            created_at=now,
            started_at=now,
            plan={},
            pr_url=None,
            last_error=None,
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        heartbeat = _FakeHeartbeatController()
        session = MagicMock()
        session.refresh.side_effect = lambda _target, **_kwargs: None

        def _persist_stage_checkpoint(
            _session,
            *,
            run,
            checkpoint,
            execution_context,
            expected_worker_service_instance_id,
            expected_claim_id,
        ):  # noqa: ANN001
            self.assertEqual(expected_worker_service_instance_id, "node-a:1234")
            self.assertEqual(expected_claim_id, "claim-1")
            self.assertEqual(
                execution_context,
                {
                    "execution_repo_dir": "/tmp/workdirs/route25-default/runs/run-1/repo",
                    "workspace_key": "worker-a",
                    "execution_branch": "run/gp-122/run-1",
                    "integration_branch": "feature/GP-122",
                    "base_branch": "staging",
                    "start_point_ref": "origin/feature/GP-122",
                    "start_point_sha": "abc123",
                },
            )
            snapshot = ExecutionSnapshot.empty()
            snapshot.apply_stage_checkpoint(checkpoint)
            snapshot.apply_execution_context(dict(execution_context))
            run.plan = snapshot.dump()
            return run

        def _runner_run(_request, *, test_feedback_hook=None, stage_checkpoint_hook=None):  # noqa: ANN001,ARG001
            self.assertIsNotNone(stage_checkpoint_hook)
            stage_checkpoint_hook(
                WorkflowStageCheckpoint(
                    stage="pm",
                    attempt=1,
                    status="completed",
                    summary="PM complete",
                    plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac"], risks=[]),
                )
            )
            return WorkflowResult(
                outcome="success",
                plan=PmPlan(plan_steps=["plan"], acceptance_criteria=["ac"], risks=[]),
                pr_url=None,
                summary=["Done"],
                test_guidance=[],
                attempts=1,
            )

        def _finalize(*_args, run, **_kwargs):  # noqa: ANN001
            self.assertEqual(run.plan["stages"]["pm"]["artifact"]["plan_steps"], ["plan"])
            self.assertEqual(run.plan["stages"]["pm"]["status"], "completed")
            return run

        result = process_next_queued_run(
            session=session,
            runner=SimpleNamespace(run=_runner_run),
            logger=MagicMock(),
            settings_fn=lambda: SimpleNamespace(
                worker_capabilities="linux",
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
                tenant_id="tenant-1",
            ),
            claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                run=run,
                tenant=tenant,
                terminal_run=None,
            ),
            send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
            send_jira_message_fn=MagicMock(),
            resolve_project_for_run_fn=MagicMock(return_value=project),
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            build_run_heartbeat_controller_fn=lambda **_: heartbeat,
            bind_run_project_fn=MagicMock(),
            workflow_request_for_run_fn=MagicMock(
                return_value=SimpleNamespace(
                    start_point_ref="origin/feature/GP-122",
                    start_point_sha="abc123",
                    execution_repo_dir="/tmp/workdirs/route25-default/runs/run-1/repo",
                    workspace_key="worker-a",
                    execution_branch="run/gp-122/run-1",
                    integration_branch="feature/GP-122",
                    base_branch="staging",
                )
            ),
            fail_guardrail_violation_fn=MagicMock(),
            tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.test/GP-122"),
            lock_acquired_update_fn=MagicMock(return_value={"stage": "lock_acquired", "discord_message": "locked", "jira_message": ""}),
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=MagicMock(),
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=_finalize,
            persist_stage_checkpoint_fn=_persist_stage_checkpoint,
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(return_value=SimpleNamespace(stale=False, message=None)),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertIs(result, run)

    def test_process_next_queued_run_records_mandatory_completion_failure_and_finalizes_failed_run(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="running",
            worker_service_instance_id="node-a:1234",
            created_at=now,
            started_at=now,
            plan={},
            pr_url=None,
            last_error=None,
        )
        finalized_run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="failed",
            pr_url=None,
            plan=run.plan,
            last_error="Completion step failed",
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        heartbeat = _FakeHeartbeatController()
        session = MagicMock()
        session.refresh.side_effect = lambda _target, **_kwargs: None
        finalize_run = MagicMock(return_value=finalized_run)
        cleanup = MagicMock()
        emit_agent_event = MagicMock()
        recorded_rows: list[dict[str, object]] = []

        def _send_jira_message(**kwargs):  # noqa: ANN001
            if kwargs.get("stage") == "review_summary":
                raise RuntimeError("jira is unavailable")

        def _record_run_log_event(**kwargs: object) -> None:
            recorded_rows.append(dict(kwargs))

        with patch("orchestrator.core.worker.finalization.record_run_log_event", side_effect=_record_run_log_event):
            result = process_next_queued_run(
                session=session,
                runner=SimpleNamespace(
                    run=MagicMock(
                        return_value=WorkflowResult(
                            outcome="success",
                            plan=None,
                            pr_url=None,
                            summary=["Done"],
                            test_guidance=[],
                            attempts=1,
                            review_summary=["Approved with notes"],
                        )
                    )
                ),
                logger=MagicMock(),
                settings_fn=lambda: SimpleNamespace(
                    worker_capabilities="linux",
                    worker_workspace_key="worker-a",
                    project_repo_checkout_base_dir="/tmp/workdirs",
                    admin_ui_base_url="http://localhost:4100",
                    worker_run_heartbeat_interval_seconds=30,
                    tenant_id="tenant-1",
                ),
                claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                    run=run,
                    tenant=tenant,
                    terminal_run=None,
                ),
                send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
                send_jira_message_fn=_send_jira_message,
                resolve_project_for_run_fn=MagicMock(return_value=project),
                fail_missing_project_mapping_fn=MagicMock(),
                block_archived_project_fn=MagicMock(),
                ensure_project_repository_checkout_fn=MagicMock(),
                fail_project_repository_checkout_fn=MagicMock(),
                cleanup_run_workspaces_fn=cleanup,
                build_run_heartbeat_controller_fn=lambda **_: heartbeat,
                bind_run_project_fn=MagicMock(),
                workflow_request_for_run_fn=MagicMock(return_value=SimpleNamespace(start_point_ref=None, start_point_sha=None)),
                fail_guardrail_violation_fn=MagicMock(),
                tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.test/GP-122"),
                lock_acquired_update_fn=MagicMock(return_value={"stage": "lock_acquired", "discord_message": "locked", "jira_message": ""}),
                plan_posted_update_fn=MagicMock(),
                pr_opened_update_fn=MagicMock(),
                run_failed_update_fn=MagicMock(),
                run_requeued_capability_update_fn=MagicMock(),
                run_requeued_stale_snapshot_update_fn=MagicMock(),
                finalize_cancelled_run_fn=MagicMock(),
                finalize_workflow_result_fn=finalize_run,
                persist_stage_checkpoint_fn=MagicMock(),
                requeue_workflow_result_for_capability_fn=MagicMock(),
                requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
                check_run_snapshot_freshness_fn=MagicMock(),
                transition_issue_status_fn=MagicMock(),
                emit_agent_event_fn=emit_agent_event,
                resolve_agent_id_fn=lambda: "worker-linux-local",
                resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
                run_status_queued="queued",
                run_status_running="running",
                run_status_failed="failed",
                run_status_blocked="blocked",
                run_status_cancelled="cancelled",
            )

        self.assertIs(result, finalized_run)
        cleanup.assert_called_once()
        finalize_run.assert_called_once()
        finalized_workflow_result = finalize_run.call_args.kwargs["workflow_result"]
        self.assertEqual(finalized_workflow_result.outcome, "success")
        event_types = [call.kwargs["event_type"] for call in emit_agent_event.call_args_list]
        self.assertNotIn("TASK_COMPLETED", event_types)
        self.assertIn("RUN_FAILED", event_types)
        self.assertIn("TASK_FAILED", event_types)
        recorded_messages = [json.loads(str(row["message"])) for row in recorded_rows if row.get("stage") == "telemetry"]
        self.assertTrue(
            any(
                message.get("event_kind") == "completion_step_failed"
                and message.get("step") == "jira_feedback"
                for message in recorded_messages
            )
        )

    def test_process_next_queued_run_fails_when_manual_pr_reporting_fails(self) -> None:
        now = datetime.now(timezone.utc)
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="running",
            worker_service_instance_id="node-a:1234",
            created_at=now,
            started_at=now,
            plan={},
            pr_url=None,
            last_error=None,
        )
        finalized_run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            project_id="project-1",
            status="failed",
            pr_url=None,
            plan=run.plan,
            last_error="Completion step failed",
        )
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={})
        project = SimpleNamespace(project_id="project-1", policy_overrides={}, is_archived=False)
        heartbeat = _FakeHeartbeatController()
        session = MagicMock()
        session.refresh.side_effect = lambda _target, **_kwargs: None
        finalize_run = MagicMock(return_value=finalized_run)
        recorded_rows: list[dict[str, object]] = []

        def _record_run_log_event(**kwargs: object) -> None:
            recorded_rows.append(dict(kwargs))

        with (
            patch("orchestrator.core.worker.finalization.record_run_log_event", side_effect=_record_run_log_event),
            patch(
                "orchestrator.core.worker.finalization.publish_manual_pr_remediation_completion",
                side_effect=RuntimeError("github publish failed"),
            ),
        ):
            result = process_next_queued_run(
                session=session,
                runner=SimpleNamespace(
                    run=MagicMock(
                        return_value=WorkflowResult(
                            outcome="success",
                            plan=None,
                            pr_url=None,
                            summary=["Done"],
                            test_guidance=[],
                            attempts=1,
                        )
                    )
                ),
                logger=MagicMock(),
                settings_fn=lambda: SimpleNamespace(
                    worker_capabilities="linux",
                    worker_workspace_key="worker-a",
                    project_repo_checkout_base_dir="/tmp/workdirs",
                    admin_ui_base_url="http://localhost:4100",
                    worker_run_heartbeat_interval_seconds=30,
                    tenant_id="tenant-1",
                ),
                claim_next_queued_run_fn=lambda *_args, **_kwargs: SimpleNamespace(
                    run=run,
                    tenant=tenant,
                    terminal_run=None,
                ),
                send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
                send_jira_message_fn=MagicMock(),
                resolve_project_for_run_fn=MagicMock(return_value=project),
                fail_missing_project_mapping_fn=MagicMock(),
                block_archived_project_fn=MagicMock(),
                ensure_project_repository_checkout_fn=MagicMock(),
                fail_project_repository_checkout_fn=MagicMock(),
                cleanup_run_workspaces_fn=MagicMock(),
                build_run_heartbeat_controller_fn=lambda **_: heartbeat,
                bind_run_project_fn=MagicMock(),
                workflow_request_for_run_fn=MagicMock(return_value=SimpleNamespace(start_point_ref=None, start_point_sha=None)),
                fail_guardrail_violation_fn=MagicMock(),
                tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.test/GP-122"),
                lock_acquired_update_fn=MagicMock(return_value={"stage": "lock_acquired", "discord_message": "locked", "jira_message": ""}),
                plan_posted_update_fn=MagicMock(),
                pr_opened_update_fn=MagicMock(),
                run_failed_update_fn=MagicMock(),
                run_requeued_capability_update_fn=MagicMock(),
                run_requeued_stale_snapshot_update_fn=MagicMock(),
                finalize_cancelled_run_fn=MagicMock(),
                finalize_workflow_result_fn=finalize_run,
                persist_stage_checkpoint_fn=MagicMock(),
                requeue_workflow_result_for_capability_fn=MagicMock(),
                requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
                check_run_snapshot_freshness_fn=MagicMock(),
                transition_issue_status_fn=MagicMock(),
                emit_agent_event_fn=MagicMock(),
                resolve_agent_id_fn=lambda: "worker-linux-local",
                resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
                run_status_queued="queued",
                run_status_running="running",
                run_status_failed="failed",
                run_status_blocked="blocked",
                run_status_cancelled="cancelled",
            )

        self.assertIs(result, finalized_run)
        finalized_workflow_result = finalize_run.call_args.kwargs["workflow_result"]
        self.assertEqual(finalized_workflow_result.outcome, "success")
        recorded_messages = [json.loads(str(row["message"])) for row in recorded_rows if row.get("stage") == "telemetry"]
        self.assertTrue(
            any(
                message.get("event_kind") == "completion_step_failed"
                and message.get("step") == "manual_pr_reporting"
                for message in recorded_messages
            )
        )


if __name__ == "__main__":
    unittest.main()
