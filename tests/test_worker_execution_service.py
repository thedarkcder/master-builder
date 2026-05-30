from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock
from unittest.mock import patch


class WorkerExecutionServiceTests(unittest.TestCase):
    def test_claimed_run_path_delegates_to_process_service_with_claimed_selection(self) -> None:
        from orchestrator.core.worker import execution_service as execution_service_module

        claimed_run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-1",
            issue_key="GP-122",
            workflow_id="workflow-1",
            status="dispatching",
            worker_service_instance_id="worker-linux-local:runs",
            claim_id="claim-1",
        )
        tenant = SimpleNamespace(
            tenant_id="tenant-1",
            policy_config={"max_dev_test_review_loops": 10, "allow_pr_creation": True},
        )
        project = SimpleNamespace(
            project_id="project-1",
            policy_overrides={"max_dev_test_review_loops": 3},
        )
        workflow = SimpleNamespace(workflow_id="workflow-1", orchestration_backend="legacy")
        session = MagicMock()
        session.get.side_effect = [claimed_run, tenant, workflow]

        with (
            patch.object(
                execution_service_module,
                "get_settings",
                return_value=SimpleNamespace(agent_id="worker-linux-local"),
            ),
            patch.object(
                execution_service_module,
                "resolve_run_execution_policy_context",
                return_value=SimpleNamespace(
                    project=project,
                    effective_policy={"max_dev_test_review_loops": 3, "allow_pr_creation": True},
                ),
            ),
            patch.object(
                execution_service_module,
                "_process_claimed_run_impl",
                return_value=claimed_run,
            ) as process_claimed_mock,
        ):
            result = execution_service_module.process_claimed_run_with_dependencies(
                session=session,
                runner=MagicMock(),
                run_id="run-1",
                claim_id="claim-1",
            )

        self.assertIs(result, claimed_run)
        self.assertEqual(
            process_claimed_mock.call_args.kwargs["selection"].claimed_run.run.run_id,
            "run-1",
        )
        self.assertEqual(
            process_claimed_mock.call_args.kwargs["selection"].claimed_run.claim_id,
            "claim-1",
        )
        self.assertIs(
            process_claimed_mock.call_args.kwargs["selection"].claimed_run.project,
            project,
        )
        self.assertEqual(
            process_claimed_mock.call_args.kwargs["selection"].claimed_run.effective_policy[
                "max_dev_test_review_loops"
            ],
            3,
        )
