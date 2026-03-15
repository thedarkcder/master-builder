from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch

from orchestrator.core.worker.process_service import _emit_detailed_jira_feedback
from orchestrator.core.worker.process_service import _emit_orchestrated_trace_logs
from orchestrator.core.worker.process_service import _extract_capability_requeue_target
from orchestrator.core.worker.process_service import process_next_queued_run
from orchestrator.core.workflow.runner import PmPlan, WorkflowDiagnostics, WorkflowResult


class WorkerProcessServiceTests(unittest.TestCase):
    def test_emit_orchestrated_trace_logs_persists_stage_rows(self) -> None:
        recorded_rows: list[dict[str, object]] = []

        def _record_run_log_event(**kwargs: object) -> None:
            recorded_rows.append(dict(kwargs))

        workflow_result = WorkflowResult(
            succeeded=False,
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

        with patch("orchestrator.core.worker.process_service.record_run_log_event", side_effect=_record_run_log_event):
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
            succeeded=False,
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

    def test_extract_capability_requeue_target_from_plan(self) -> None:
        workflow_result = WorkflowResult(
            succeeded=False,
            plan=PmPlan(
                plan_steps=["Plan"],
                acceptance_criteria=["AC"],
                risks=[],
                execution_worker_capability="macos",
            ),
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=1,
            diagnostics=WorkflowDiagnostics(
                stage="pm",
                message="Execution capability mismatch: PM selected macos but current worker is linux.",
                attempts=1,
                history=[],
            ),
        )
        self.assertEqual(_extract_capability_requeue_target(workflow_result), "macos")

    def test_extract_capability_requeue_target_returns_none_for_non_mismatch(self) -> None:
        workflow_result = WorkflowResult(
            succeeded=False,
            plan=None,
            pr_url=None,
            summary=[],
            test_guidance=[],
            attempts=1,
            diagnostics=WorkflowDiagnostics(
                stage="test",
                message="Max workflow attempts reached after test failures",
                attempts=1,
                history=[],
            ),
        )
        self.assertIsNone(_extract_capability_requeue_target(workflow_result))

    def test_process_next_queued_run_retries_start_claim_conflicts(self) -> None:
        run = SimpleNamespace(run_id="run-1", tenant_id="tenant-1", issue_key="GP-122", project_id="project-1")
        tenant = SimpleNamespace(tenant_id="tenant-1")
        selection = SimpleNamespace(
            run=run,
            tenant=tenant,
            terminal_run=None,
            effective_policy={"max_concurrent_runs": 1},
        )
        selection_fn = MagicMock(return_value=selection)
        start_run_fn = MagicMock(side_effect=[None, run])
        decision_gate_run = SimpleNamespace(run_id="decision-gate-result")

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
            select_next_queued_run_fn=selection_fn,
            apply_decision_gate_fn=lambda **_: (
                decision_gate_run,
                {"send_result": SimpleNamespace(sent=True), "stage_update": {"stage": "decision_gate_required"}},
            ),
            send_discord_message_fn=MagicMock(),
            send_jira_message_fn=MagicMock(),
            ask_reply_components_fn=MagicMock(),
            resolve_project_for_run_fn=MagicMock(),
            fail_missing_project_mapping_fn=MagicMock(),
            block_archived_project_fn=MagicMock(),
            ensure_project_repository_checkout_fn=MagicMock(),
            fail_project_repository_checkout_fn=MagicMock(),
            cleanup_run_workspaces_fn=MagicMock(),
            start_run_fn=start_run_fn,
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
            requeue_workflow_result_for_capability_fn=MagicMock(),
            requeue_workflow_result_for_stale_snapshot_fn=MagicMock(),
            check_run_snapshot_freshness_fn=MagicMock(),
            transition_issue_status_fn=MagicMock(),
            emit_agent_event_fn=MagicMock(),
            resolve_agent_id_fn=lambda: "worker-linux-local",
            run_status_queued="queued",
            run_status_running="running",
            run_status_failed="failed",
            run_status_blocked="blocked",
            run_status_cancelled="cancelled",
        )

        self.assertIs(result, decision_gate_run)
        self.assertEqual(start_run_fn.call_count, 2)
        start_kwargs = start_run_fn.call_args.kwargs
        self.assertEqual(start_kwargs["expected_status"], "queued")
        self.assertEqual(start_kwargs["max_concurrent_runs"], 1)


if __name__ == "__main__":
    unittest.main()
