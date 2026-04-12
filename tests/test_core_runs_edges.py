from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy.exc import IntegrityError

from orchestrator.core.runs import (
    RUN_STATUS_FAILED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_SUCCEEDED,
    RunStateTransitionError,
    _first_active_run_for_tenant,
    _coerce_positive_limit,
    cancel_run,
    enqueue_run,
    mark_run_terminal,
)


class CoreRunsEdgeTests(unittest.TestCase):
    def test_coerce_positive_limit_rejects_invalid_value(self) -> None:
        with self.assertRaises(RunStateTransitionError):
            _coerce_positive_limit("bad")  # type: ignore[arg-type]

    def test_enqueue_raises_when_duplicate_delivery_points_to_missing_run(self) -> None:
        session = MagicMock()
        delivery = SimpleNamespace(run_id="missing-run")

        def fake_get(model, key):  # noqa: ANN001
            if getattr(model, "__name__", "") == "WebhookDelivery":
                return delivery
            if getattr(model, "__name__", "") == "Run":
                return None
            return None

        session.get.side_effect = fake_get

        with self.assertRaises(RunStateTransitionError):
            enqueue_run(
                session,
                tenant_id="tenant-a",
                project_id=None,
                issue_key="TP-1",
                delivery_id="d-1",
            )

    def test_enqueue_raises_when_concurrency_limit_reached_without_active_run(self) -> None:
        session = MagicMock()
        with (
            patch("orchestrator.core.runs._active_workflow_for_issue", return_value=None),
            patch("orchestrator.core.runs._active_run_count_for_tenant", return_value=1),
            patch("orchestrator.core.runs._first_active_run_for_tenant", return_value=None),
        ):
            with self.assertRaises(RunStateTransitionError):
                enqueue_run(
                    session,
                    tenant_id="tenant-a",
                    project_id=None,
                    issue_key="TP-2",
                    max_concurrent_runs=1,
                )

    def test_enqueue_raises_unknown_integrity_conflict_after_retry(self) -> None:
        session = MagicMock()
        session.commit.side_effect = IntegrityError("stmt", {}, Exception("db"))
        with (
            patch("orchestrator.core.runs._active_workflow_for_issue", return_value=None),
            patch("orchestrator.core.runs._active_run_count_for_tenant", return_value=0),
            patch("orchestrator.core.runs.notify_run_enqueued"),
        ):
            with self.assertRaises(RunStateTransitionError):
                enqueue_run(
                    session,
                    tenant_id="tenant-a",
                    project_id=None,
                    issue_key="TP-3",
                )

    def test_enqueue_integrity_retry_returns_duplicate_delivery_when_delivery_found(self) -> None:
        session = MagicMock()
        session.commit.side_effect = IntegrityError("stmt", {}, Exception("db"))
        delivery = SimpleNamespace(run_id="run-1")
        deduped_run = SimpleNamespace(run_id="run-1")

        def fake_get(model, key):  # noqa: ANN001
            name = getattr(model, "__name__", "")
            if name == "WebhookDelivery":
                return delivery
            if name == "Run":
                return deduped_run
            return None

        session.get.side_effect = fake_get
        with patch("orchestrator.core.runs.notify_run_enqueued"):
            result = enqueue_run(
                session,
                tenant_id="tenant-a",
                project_id=None,
                issue_key="TP-3",
                delivery_id="d-1",
            )
        self.assertFalse(result.enqueued)
        self.assertEqual(result.reason, "duplicate_delivery")
        self.assertEqual(result.run.run_id, "run-1")

    def test_enqueue_integrity_retry_raises_unknown_conflict_without_active_workflow(self) -> None:
        session = MagicMock()
        session.commit.side_effect = IntegrityError("stmt", {}, Exception("db"))
        with (
            patch("orchestrator.core.runs._active_workflow_for_issue", return_value=None),
            patch("orchestrator.core.runs._active_run_count_for_tenant", return_value=0),
            patch("orchestrator.core.runs.notify_run_enqueued"),
        ):
            with self.assertRaises(RunStateTransitionError):
                enqueue_run(
                    session,
                    tenant_id="tenant-a",
                    project_id=None,
                    issue_key="TP-3",
                    max_concurrent_runs=1,
                )

    def test_enqueue_integrity_retry_returns_concurrency_limit_result(self) -> None:
        session = MagicMock()
        session.commit.side_effect = IntegrityError("stmt", {}, Exception("db"))
        limited_run = SimpleNamespace(run_id="run-limited")
        with (
            patch("orchestrator.core.runs._active_workflow_for_issue", return_value=None),
            patch("orchestrator.core.runs._active_run_count_for_tenant", side_effect=[0, 1]),
            patch("orchestrator.core.runs._first_active_run_for_tenant", return_value=limited_run),
            patch("orchestrator.core.runs.notify_run_enqueued"),
        ):
            result = enqueue_run(
                session,
                tenant_id="tenant-a",
                project_id=None,
                issue_key="TP-3",
                precheck_outcome="ready_for_agent",
                max_concurrent_runs=1,
            )
        self.assertFalse(result.enqueued)
        self.assertEqual(result.reason, "tenant_concurrency_limit_reached")
        self.assertEqual(result.run.run_id, "run-limited")

    def test_enqueue_persists_precheck_and_required_worker_capability_on_run_row(self) -> None:
        session = MagicMock()
        added_rows: list[object] = []
        session.add.side_effect = added_rows.append

        with (
            patch("orchestrator.core.runs._active_workflow_for_issue", return_value=None),
            patch("orchestrator.core.runs._active_run_count_for_tenant", return_value=0),
            patch("orchestrator.core.runs.notify_run_enqueued"),
        ):
            result = enqueue_run(
                session,
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="TP-8",
                precheck_outcome="ready_for_agent",
                required_worker_capability="macos",
            )

        run_rows = [row for row in added_rows if getattr(row, "__class__", type("", (), {})).__name__ == "Run"]
        self.assertEqual(len(run_rows), 1)
        run = run_rows[0]
        self.assertTrue(result.enqueued)
        self.assertEqual(run.pre_check_outcome, "ready_for_agent")
        self.assertEqual(run.required_worker_capability, "macos")

    def test_enqueue_rejects_missing_ready_precheck(self) -> None:
        session = MagicMock()

        with (
            patch("orchestrator.core.runs._active_workflow_for_issue", return_value=None),
            patch("orchestrator.core.runs._active_run_count_for_tenant", return_value=0),
            self.assertRaises(RunStateTransitionError),
        ):
            enqueue_run(
                session,
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="TP-8B",
                precheck_outcome=None,
            )

        session.add.assert_not_called()
        session.commit.assert_not_called()

    def test_mark_run_terminal_rejects_terminal_status_change(self) -> None:
        session = MagicMock()
        run = SimpleNamespace(
            run_id="run-1",
            tenant_id="tenant-a",
            issue_key="TP-4",
            status=RUN_STATUS_SUCCEEDED,
            started_at=None,
            finished_at=None,
            last_error=None,
        )
        session.get.return_value = run
        with self.assertRaises(RunStateTransitionError):
            mark_run_terminal(session, run_id="run-1", terminal_status=RUN_STATUS_FAILED)

    def test_cancel_run_rejects_terminal_run(self) -> None:
        session = MagicMock()
        run = SimpleNamespace(
            run_id="run-1",
            status=RUN_STATUS_SUCCEEDED,
            tenant_id="tenant-a",
            issue_key="TP-5",
            started_at=None,
            finished_at=None,
            last_error=None,
        )
        session.get.return_value = run
        with self.assertRaises(RunStateTransitionError):
            cancel_run(session, run_id="run-1", cancelled_by="user")

    def test_cancel_run_sets_terminal_fields_for_active_run(self) -> None:
        session = MagicMock()
        run = SimpleNamespace(
            run_id="run-1",
            workflow_id="workflow-1",
            status=RUN_STATUS_RUNNING,
            tenant_id="tenant-a",
            issue_key="TP-6",
            started_at=None,
            finished_at=None,
            last_error=None,
        )
        workflow = SimpleNamespace(
            workflow_id="workflow-1",
            status=RUN_STATUS_RUNNING,
            last_error=None,
            finished_at=None,
            updated_at=None,
        )

        def fake_get(model, key):  # noqa: ANN001
            name = getattr(model, "__name__", "")
            if name == "Run":
                return run
            if name == "WorkflowExecution":
                return workflow
            return None

        session.get.side_effect = fake_get
        cancelled = cancel_run(session, run_id="run-1", cancelled_by="user")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertIn("Cancelled by user", str(cancelled.last_error))
        self.assertIsNotNone(cancelled.started_at)
        self.assertIsNotNone(cancelled.finished_at)

    def test_first_active_run_for_tenant_returns_first_row_without_uniqueness_assumption(self) -> None:
        session = MagicMock()
        first_run = SimpleNamespace(run_id="run-oldest")
        scalars_result = MagicMock()
        scalars_result.first.return_value = first_run
        execute_result = MagicMock()
        execute_result.scalars.return_value = scalars_result
        session.execute.return_value = execute_result

        selected = _first_active_run_for_tenant(session, tenant_id="tenant-a")

        self.assertIs(selected, first_run)
        execute_result.scalars.assert_called_once()
        scalars_result.first.assert_called_once()
