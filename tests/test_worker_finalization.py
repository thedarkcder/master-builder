from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from orchestrator.core.worker.finalization import CompletionTailExecutor
from orchestrator.core.worker.finalization import WorkflowFinalizer
from orchestrator.core.workflow.runner import WorkflowDiagnostics
from orchestrator.core.workflow.runner import WorkflowResult


class WorkflowFinalizationTests(unittest.TestCase):
    def test_workflow_finalizer_returns_terminal_plan_for_success(self) -> None:
        session = MagicMock()
        logger = MagicMock()
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-185",
            project_id="project-1",
            status="running",
            plan={"x": 1},
            last_error=None,
        )
        finalized_run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-185",
            project_id="project-1",
            status="succeeded",
            plan={"x": 1},
            last_error=None,
        )
        workflow_result = WorkflowResult(
            outcome="success",
            plan=None,
            pr_url="https://github.com/org/repo/pull/1",
            summary=["done"],
            test_guidance=[],
            attempts=1,
        )

        finalizer = WorkflowFinalizer(
            session=session,
            logger=logger,
            finalize_workflow_result_fn=MagicMock(return_value=finalized_run),
            run_status_failed="failed",
            project_id="project-1",
            agent_id="worker-linux",
        )

        with patch("orchestrator.core.worker.finalization.emit_logging_pane_event"):
            plan = finalizer.finalize(
                run=run,
                workflow_result=workflow_result,
                stage_updates=[],
                execution_context={"execution_branch": "run/gp-185"},
                expected_worker_service_instance_id="worker-1",
            )

        self.assertIs(plan.run, finalized_run)
        self.assertEqual(plan.persisted_status, "succeeded")
        self.assertEqual(plan.event_types, ("TASK_COMPLETED",))
        self.assertEqual(
            plan.tail_steps,
            (
                "orchestration_trace",
                "jira_feedback",
                "manual_pr_reporting",
                "workspace_cleanup",
            ),
        )

    def test_workflow_finalizer_keeps_failed_run_workspace_for_recovery(self) -> None:
        session = MagicMock()
        logger = MagicMock()
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-185",
            project_id="project-1",
            status="running",
            plan={"x": 1},
            last_error=None,
        )
        finalized_run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-185",
            project_id="project-1",
            status="failed",
            plan={"x": 1},
            last_error="dev failed",
        )
        workflow_result = WorkflowResult(
            outcome="failed",
            plan=None,
            pr_url=None,
            summary=["failed"],
            test_guidance=[],
            attempts=1,
            diagnostics=WorkflowDiagnostics(
                stage="dev", message="dev failed", attempts=1, history=[]
            ),
        )

        finalizer = WorkflowFinalizer(
            session=session,
            logger=logger,
            finalize_workflow_result_fn=MagicMock(return_value=finalized_run),
            run_status_failed="failed",
            project_id="project-1",
            agent_id="worker-linux",
        )

        with patch("orchestrator.core.worker.finalization.emit_logging_pane_event"):
            plan = finalizer.finalize(
                run=run,
                workflow_result=workflow_result,
                stage_updates=[],
                execution_context={"execution_branch": "run/gp-185"},
                expected_worker_service_instance_id="worker-1",
            )

        self.assertIs(plan.run, finalized_run)
        self.assertEqual(plan.persisted_status, "failed")
        self.assertEqual(plan.event_types, ("RUN_FAILED", "TASK_FAILED"))
        self.assertEqual(
            plan.tail_steps,
            ("orchestration_trace", "jira_feedback", "manual_pr_reporting"),
        )

    def test_workflow_finalizer_falls_back_to_failed_plan_when_finalize_raises(
        self,
    ) -> None:
        session = MagicMock()
        logger = MagicMock()
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-185",
            project_id="project-1",
            status="running",
            plan={},
            last_error=None,
        )
        marked_run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-185",
            project_id="project-1",
            status="failed",
            plan={},
            last_error="boom",
        )
        workflow_result = WorkflowResult(
            outcome="failed",
            plan=None,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=1,
            diagnostics=WorkflowDiagnostics(
                stage="dev", message="boom", attempts=1, history=[]
            ),
        )

        finalizer = WorkflowFinalizer(
            session=session,
            logger=logger,
            finalize_workflow_result_fn=MagicMock(side_effect=RuntimeError("boom")),
            mark_run_terminal_fn=MagicMock(return_value=marked_run),
            run_status_failed="failed",
            project_id="project-1",
            agent_id="worker-linux",
        )

        with patch("orchestrator.core.worker.finalization.emit_logging_pane_event"):
            plan = finalizer.finalize(
                run=run,
                workflow_result=workflow_result,
                stage_updates=[],
                execution_context=None,
                expected_worker_service_instance_id="worker-1",
            )

        self.assertIs(plan.run, marked_run)
        self.assertEqual(plan.persisted_status, "failed")
        self.assertEqual(plan.event_types, ("RUN_FAILED", "TASK_FAILED"))
        self.assertEqual(plan.tail_steps, ())
        session.rollback.assert_called_once()

    def test_completion_tail_executor_uses_finalized_run_and_keeps_tail_failures_non_authoritative(
        self,
    ) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
        project = SimpleNamespace(project_id="project-1")
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-185",
            project_id="project-1",
            status="succeeded",
            pr_url="https://github.com/org/repo/pull/1",
            plan={},
            last_error=None,
        )
        workflow_result = WorkflowResult(
            outcome="success",
            plan=None,
            pr_url=run.pr_url,
            summary=["done"],
            test_guidance=[],
            attempts=1,
            review_summary=["Looks good"],
        )
        plan = SimpleNamespace(
            run=run,
            workflow_result=workflow_result,
            tail_steps=(
                "orchestration_trace",
                "jira_feedback",
                "manual_pr_reporting",
                "workspace_cleanup",
            ),
        )

        send_jira_message = MagicMock(side_effect=RuntimeError("jira down"))
        cleanup = MagicMock()

        executor = CompletionTailExecutor(
            session=session,
            tenant=tenant,
            project=project,
            settings=SimpleNamespace(),
            logger=MagicMock(),
            send_jira_message_fn=send_jira_message,
            cleanup_run_workspaces_fn=cleanup,
            base_dir="/tmp/workdirs",
            jira_issue_url="https://jira.test/GP-185",
            agent_id="worker-linux",
            workspace_key="worker-a",
        )

        recorded_rows: list[dict[str, object]] = []

        def _emit_logging_pane_event(**kwargs: object) -> None:
            recorded_rows.append(dict(kwargs))

        with (
            patch(
                "orchestrator.core.worker.finalization.emit_logging_pane_event",
                side_effect=_emit_logging_pane_event,
            ),
            patch(
                "orchestrator.core.worker.finalization.publish_manual_pr_remediation_completion"
            ) as publish_completion,
        ):
            executor.execute(plan)

        publish_completion.assert_called_once()
        self.assertIs(publish_completion.call_args.kwargs["run"], run)
        self.assertEqual(
            publish_completion.call_args.kwargs["terminal_status"], "succeeded"
        )
        cleanup.assert_called_once()
        failure_messages = [
            json.loads(str(row["message"]))
            for row in recorded_rows
            if row.get("stage") == "telemetry"
        ]
        self.assertTrue(
            any(
                message.get("event_kind") == "completion_step_failed"
                and message.get("step") == "jira_feedback"
                for message in failure_messages
            )
        )
        self.assertEqual(run.status, "succeeded")

    def test_completion_tail_executor_runs_steps_in_defined_order(self) -> None:
        session = MagicMock()
        tenant = SimpleNamespace(tenant_id="tenant-1")
        project = SimpleNamespace(project_id="project-1")
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-185",
            project_id="project-1",
            status="succeeded",
            pr_url=None,
            plan={},
            last_error=None,
        )
        workflow_result = WorkflowResult(
            outcome="success",
            plan=None,
            pr_url=None,
            summary=["done"],
            test_guidance=[],
            attempts=1,
        )
        plan = SimpleNamespace(
            run=run,
            workflow_result=workflow_result,
            tail_steps=(
                "orchestration_trace",
                "jira_feedback",
                "manual_pr_reporting",
                "workspace_cleanup",
            ),
        )
        order: list[str] = []
        cleanup = MagicMock(side_effect=lambda **_: order.append("workspace_cleanup"))

        executor = CompletionTailExecutor(
            session=session,
            tenant=tenant,
            project=project,
            settings=SimpleNamespace(),
            logger=MagicMock(),
            send_jira_message_fn=MagicMock(),
            cleanup_run_workspaces_fn=cleanup,
            base_dir="/tmp/workdirs",
            jira_issue_url="https://jira.test/GP-185",
            agent_id="worker-linux",
            workspace_key="worker-a",
        )

        with (
            patch("orchestrator.core.worker.finalization.emit_logging_pane_event"),
            patch(
                "orchestrator.core.worker.finalization._emit_orchestrated_trace_logs",
                side_effect=lambda **_: order.append("orchestration_trace"),
            ),
            patch(
                "orchestrator.core.worker.finalization._emit_detailed_jira_feedback",
                side_effect=lambda **_: order.append("jira_feedback"),
            ),
            patch(
                "orchestrator.core.worker.finalization.publish_manual_pr_remediation_completion",
                side_effect=lambda **_: order.append("manual_pr_reporting"),
            ),
        ):
            executor.execute(plan)

        self.assertEqual(
            order,
            [
                "orchestration_trace",
                "jira_feedback",
                "manual_pr_reporting",
                "workspace_cleanup",
            ],
        )


if __name__ == "__main__":
    unittest.main()
