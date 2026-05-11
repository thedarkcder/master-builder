from __future__ import annotations

from datetime import timedelta

from orchestrator.core.workflow.engine import WorkflowEngineState
from typing import Any

from orchestrator.temporal.payloads import ProjectDeploymentSetupActivityResult, ProjectDeploymentSetupWorkflowInput

try:  # pragma: no cover - exercised when temporal backend is enabled
    from temporalio import workflow
    from temporalio.common import RetryPolicy
except ImportError as exc:  # pragma: no cover - exercised when temporal backend is enabled
    raise RuntimeError("Temporal backend requires temporalio to be installed") from exc


@workflow.defn(name="ProjectDeploymentSetupWorkflow")
class ProjectDeploymentSetupWorkflow:
    def __init__(self) -> None:
        self._workflow_id: str = ""
        self._tenant_id: str = ""
        self._project_id: str = ""
        self._status: str = "queued"
        self._last_error: str | None = None
        self._activity_timeout_seconds: int = 7200

    @staticmethod
    def _activity_result_value(result: ProjectDeploymentSetupActivityResult | dict[str, Any], key: str) -> Any:
        if isinstance(result, dict):
            return result.get(key)
        return getattr(result, key, None)

    def _apply_result(self, result: ProjectDeploymentSetupActivityResult | dict[str, Any]) -> None:
        self._workflow_id = str(self._activity_result_value(result, "workflow_id") or "").strip() or self._workflow_id
        self._tenant_id = str(self._activity_result_value(result, "tenant_id") or "").strip() or self._tenant_id
        self._project_id = str(self._activity_result_value(result, "project_id") or "").strip() or self._project_id
        self._status = str(self._activity_result_value(result, "status") or "").strip().lower() or self._status
        self._last_error = str(self._activity_result_value(result, "last_error") or "").strip() or None

    @workflow.run
    async def run(self, payload: ProjectDeploymentSetupWorkflowInput) -> WorkflowEngineState:
        self._workflow_id = str(payload.workflow_id or "").strip()
        self._tenant_id = str(payload.tenant_id or "").strip()
        self._project_id = str(payload.project_id or "").strip()
        self._activity_timeout_seconds = max(1, int(payload.activity_start_to_close_timeout_seconds or 0))
        self._status = "running"
        result = await workflow.execute_activity(
            "run_project_deployment_setup_activity",
            payload,
            start_to_close_timeout=timedelta(seconds=self._activity_timeout_seconds),
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=30),
                maximum_interval=timedelta(minutes=5),
                backoff_coefficient=2.0,
                maximum_attempts=3,
            ),
        )
        self._apply_result(result)
        return self.describe_state()

    @workflow.query
    def describe_state(self) -> WorkflowEngineState:
        return WorkflowEngineState(
            workflow_id=self._workflow_id,
            backend="temporal",
            status=self._status,
            active_run_id=None,
        )
