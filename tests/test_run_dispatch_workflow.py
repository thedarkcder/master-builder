from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.core.workflow.runner import WorkflowResult
from orchestrator.core.worker.run_dispatch_gateways import RunDispatchIdentityGateway
from orchestrator.core.worker.run_dispatch_gateways import RunDispatchStatusConfig
from orchestrator.core.worker.run_dispatch_gateways import RunExecutionGateway
from orchestrator.core.worker.run_dispatch_gateways import RunProjectGateway
from orchestrator.core.worker.run_dispatch_gateways import RunStageUpdateGateway
from orchestrator.core.worker.run_dispatch_workflow import (
    RunDispatchWorkflow,
    RunDispatchWorkflowDeps,
)


class _FakeHeartbeatController:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _deps(**overrides) -> RunDispatchWorkflowDeps:
    identity = overrides.pop(
        "identity",
        RunDispatchIdentityGateway(
            logger=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            build_run_heartbeat_controller_fn=lambda **_: _FakeHeartbeatController(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            resolve_worker_service_instance_id_fn=lambda: "node-a:1234",
        ),
    )
    project = overrides.pop(
        "project",
        RunProjectGateway(
            resolve_project_for_run_fn=MagicMock(),
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            bind_run_project_fn=MagicMock(),
            tenant_jira_issue_url_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
        ),
    )
    stage_updates = overrides.pop(
        "stage_updates",
        RunStageUpdateGateway(
            send_discord_message_fn=MagicMock(return_value=SimpleNamespace(sent=True, reason=None)),
            send_jira_message_fn=MagicMock(),
            lock_acquired_update_fn=MagicMock(
                side_effect=lambda **kwargs: {
                    "stage": "lock_acquired",
                    "tenant_id": kwargs["tenant_id"],
                    "issue_key": kwargs["issue_key"],
                    "run_id": kwargs["run_id"],
                    "discord_message": "",
                    "jira_message": "",
                }
            ),
            repo_setup_ready_update_fn=None,
            plan_posted_update_fn=MagicMock(),
            pr_opened_update_fn=MagicMock(),
            run_failed_update_fn=MagicMock(),
            run_requeued_repo_setup_update_fn=MagicMock(),
            run_requeued_capability_update_fn=MagicMock(),
            run_requeued_stale_snapshot_update_fn=MagicMock(),
        ),
    )
    execution = overrides.pop(
        "execution",
        RunExecutionGateway(
            promote_run_to_running_fn=MagicMock(),
            workflow_request_for_run_fn=MagicMock(),
            fail_guardrail_violation_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_setup_fn=MagicMock(),
            finalize_cancelled_run_fn=MagicMock(),
            finalize_workflow_result_fn=MagicMock(),
            persist_stage_checkpoint_fn=MagicMock(),
            requeue_run_for_repo_setup_fn=MagicMock(),
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
        ),
    )
    statuses = overrides.pop("statuses", RunDispatchStatusConfig())
    assert not overrides
    return RunDispatchWorkflowDeps(
        identity=identity,
        project=project,
        stage_updates=stage_updates,
        execution=execution,
        statuses=statuses,
    )


class RunDispatchWorkflowTests(unittest.TestCase):
    def test_execute_fails_when_dispatching_run_cannot_promote(self) -> None:
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
        deps = _deps(
            project=RunProjectGateway(
                resolve_project_for_run_fn=MagicMock(return_value=project),
                fail_missing_project_mapping_fn=MagicMock(),
                block_archived_project_fn=MagicMock(),
                bind_run_project_fn=MagicMock(return_value=run),
                tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.example/browse/GP-122"),
                transition_issue_status_fn=MagicMock(),
            ),
            execution=RunExecutionGateway(
                promote_run_to_running_fn=MagicMock(return_value=None),
                workflow_request_for_run_fn=MagicMock(),
                fail_guardrail_violation_fn=fail_guardrail_violation_fn,
                ensure_project_repository_checkout_fn=MagicMock(),
                fail_project_repository_checkout_fn=MagicMock(),
                fail_project_repository_setup_fn=MagicMock(),
                finalize_cancelled_run_fn=MagicMock(),
                finalize_workflow_result_fn=MagicMock(),
                persist_stage_checkpoint_fn=MagicMock(),
                requeue_run_for_repo_setup_fn=MagicMock(),
                requeue_workflow_result_for_capability_fn=MagicMock(),
                requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
                check_run_snapshot_freshness_fn=MagicMock(),
            ),
        )

        result = RunDispatchWorkflow(
            session=session,
            runner=MagicMock(),
            settings=SimpleNamespace(
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
            ),
            deps=deps,
        ).execute(
            selection=SimpleNamespace(
                claimed_run=SimpleNamespace(
                    run=run,
                    tenant=tenant,
                    project=project,
                    effective_policy={"allow_jira_transitions": True},
                    run_id=run.run_id,
                    claim_id=run.claim_id,
                    worker_service_instance_id=run.worker_service_instance_id,
                ),
                terminal_run=None,
            ),
        )

        self.assertEqual(result.status, "failed")
        fail_guardrail_violation_fn.assert_called_once_with(
            session,
            run=run,
            error="Claimed run could not transition from dispatching to running",
            expected_worker_service_instance_id="node-a:1234",
            expected_claim_id="claim-1",
        )

    def test_execute_promotes_before_request_build(self) -> None:
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

        deps = _deps(
            project=RunProjectGateway(
                resolve_project_for_run_fn=lambda *_args, **_kwargs: (order.append("resolve_project") or project),
                fail_missing_project_mapping_fn=MagicMock(),
                block_archived_project_fn=MagicMock(),
                bind_run_project_fn=MagicMock(return_value=run),
                tenant_jira_issue_url_fn=MagicMock(return_value="https://jira.example/browse/GP-122"),
                transition_issue_status_fn=MagicMock(),
            ),
            execution=RunExecutionGateway(
                promote_run_to_running_fn=_promote,
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
                ensure_project_repository_checkout_fn=MagicMock(),
                fail_project_repository_checkout_fn=MagicMock(),
                fail_project_repository_setup_fn=MagicMock(),
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
                requeue_run_for_repo_setup_fn=MagicMock(),
                requeue_workflow_result_for_capability_fn=MagicMock(),
                requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
                check_run_snapshot_freshness_fn=MagicMock(),
            ),
        )

        result = RunDispatchWorkflow(
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
            settings=SimpleNamespace(
                worker_workspace_key="worker-a",
                project_repo_checkout_base_dir="/tmp/workdirs",
                admin_ui_base_url="http://localhost:4100",
                worker_run_heartbeat_interval_seconds=30,
            ),
            deps=deps,
        ).execute(
            selection=SimpleNamespace(
                claimed_run=SimpleNamespace(
                    run=run,
                    tenant=tenant,
                    project=None,
                    effective_policy={"allow_jira_transitions": False},
                    worker_service_instance_id=run.worker_service_instance_id,
                    claim_id=run.claim_id,
                ),
                terminal_run=None,
            ),
        )

        self.assertEqual(getattr(result, "status", None), "succeeded")
        self.assertEqual(order[:2], ["promote", "resolve_project"])
        self.assertIn("build_request", order)
