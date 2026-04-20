from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from orchestrator.core.workflow_operation_logging import emit_workflow_operation_log
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt
from tests.test_support.db_harness import SqliteTemplateDbTestCase


class WorkflowOperationLoggingTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="workflow-operation-logging")

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def test_emit_workflow_operation_log_includes_execution_and_attempt_context(self) -> None:
        now = datetime.now(timezone.utc)
        session_factory = create_session_factory(self.database_url)
        with session_factory() as session:
            session.add(
                WorkflowExecution(
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
                    status="running",
                    last_error=None,
                    active_run_id=None,
                    latest_checkpoint_id=None,
                    source_workflow_id=None,
                    source_run_id=None,
                    created_at=now,
                    started_at=now,
                    finished_at=None,
                    updated_at=now,
                )
            )
            operation = WorkflowOperation(
                operation_id="operation-jira-child-fanout",
                workflow_id="parent_planning:MAB-215",
                run_id=None,
                operation_type="jira_child_fanout",
                idempotency_key="jira-child-fanout:MAB-215",
                status="running",
                target_system="jira",
                target_ref="MAB-215",
                summary=None,
                created_at=now,
                started_at=now,
                finished_at=None,
                updated_at=now,
            )
            attempt = WorkflowOperationAttempt(
                attempt_id="attempt-3",
                operation_id="operation-jira-child-fanout",
                attempt_number=3,
                status="running",
                error_category=None,
                error_message=None,
                status_detail=None,
                retryable=False,
                next_retry_at=None,
                created_at=now,
                started_at=now,
                finished_at=None,
            )
            session.add(operation)
            session.add(attempt)
            session.commit()

            captured: dict[str, object] = {}

            def _fake_log(level, message, *, extra):  # noqa: ANN001
                captured["level"] = level
                captured["message"] = message
                captured["extra"] = extra

            with patch(
                "orchestrator.core.workflow_operation_logging._OPERATION_LOGGER.log",
                _fake_log,
            ):
                emit_workflow_operation_log(
                    session,
                    operation=operation,
                    attempt=attempt,
                    event_type="workflow_operation_attempt_failed",
                    message='Jira API request failed (400): {"errorMessages":["CONTENT_LIMIT_EXCEEDED"],"errors":{}}',
                    metadata={"status": "failed", "error_category": "content_limit"},
                )

        extra = captured["extra"]
        assert captured["message"].startswith("Jira API request failed")
        assert extra["event_type"] == "workflow_operation_attempt_failed"
        assert extra["tenant_id"] == "tenant-a"
        assert extra["project_id"] == "tenant-a-default"
        assert extra["metadata"]["workflow_id"] == "parent_planning:MAB-215"
        assert extra["metadata"]["execution_id"] == "wfexec-mab-215"
        assert extra["metadata"]["operation_id"] == "operation-jira-child-fanout"
        assert extra["metadata"]["operation_type"] == "jira_child_fanout"
        assert extra["metadata"]["attempt_id"] == "attempt-3"
        assert extra["metadata"]["attempt_number"] == 3
        assert extra["metadata"]["error_category"] == "content_limit"
