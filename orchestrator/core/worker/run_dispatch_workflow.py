from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

from orchestrator.core.run_logs import record_run_log_event
from orchestrator.core.worker.run_dispatch_gateways import RunExecutionGateway
from orchestrator.core.worker.run_dispatch_gateways import RunDispatchIdentityGateway
from orchestrator.core.worker.run_dispatch_gateways import RunProjectGateway
from orchestrator.core.worker.run_dispatch_gateways import RunDispatchStatusConfig
from orchestrator.core.worker.run_dispatch_gateways import RunStageUpdateGateway
from orchestrator.core.worker.run_outcome_policy import RunOutcomePolicy
from orchestrator.core.worker.run_preparation_service import RunPreparationService
from orchestrator.core.worker_workspace import resolve_worker_workspace_key


@dataclass(frozen=True)
class RunDispatchWorkflowDeps:
    identity: RunDispatchIdentityGateway
    project: RunProjectGateway
    stage_updates: RunStageUpdateGateway
    execution: RunExecutionGateway
    statuses: RunDispatchStatusConfig = RunDispatchStatusConfig()


class RunDispatchWorkflow:
    def __init__(
        self,
        *,
        session,
        runner,
        settings,
        deps: RunDispatchWorkflowDeps,
    ) -> None:  # noqa: ANN001
        self._session = session
        self._runner = runner
        self._settings = settings
        self._deps = deps

    def execute(self, *, selection):  # noqa: ANN001
        prepared = RunPreparationService(
            session=self._session,
            settings=self._settings,
            deps=self._deps,
            emit_issue_assigned_fn=self._emit_issue_assigned,
            workspace_key_resolver_fn=resolve_worker_workspace_key,
            ownership_matcher_fn=_run_matches_execution_ownership,
            cleanup_run_workspaces_safe_fn=_cleanup_run_workspaces_safe,
        ).prepare(selection=selection)
        if prepared is None or getattr(prepared, "run_id", None) is not None or getattr(prepared, "status", None):
            return prepared

        def _emit_test_feedback(attempt: int, feedback: str) -> None:
            self._deps.stage_updates.send_jira_message_fn(
                session=self._session,
                tenant=prepared.tenant,
                issue_key=prepared.run.issue_key,
                stage="test_feedback",
                message=(
                    f"Test feedback (attempt {attempt}) for run {prepared.run.run_id}:\n{feedback}\n"
                    "Routing back to Dev for another iteration."
                ),
                settings=self._settings,
            )

        heartbeat_controller = self._deps.identity.build_run_heartbeat_controller_fn(
            run_id=prepared.run.run_id,
            worker_service_instance_id=prepared.worker_service_instance_id,
            claim_id=prepared.claim_id,
            heartbeat_interval_seconds=max(
                5,
                int(getattr(self._settings, "worker_run_heartbeat_interval_seconds", 30)),
            ),
        )
        execution_context = _execution_context(workflow_request=prepared.workflow_request)
        _emit_queue_wait_metric(
            session=self._session,
            run=prepared.run,
            project_id=prepared.project.project_id,
            agent_id=prepared.agent_id,
        )
        if bool(prepared.effective_policy.get("allow_jira_transitions")) and self._deps.project.transition_issue_status_fn is not None:
            self._deps.project.transition_issue_status_fn(
                session=self._session,
                tenant=prepared.tenant,
                issue_key=prepared.run.issue_key,
                target_status="In Progress",
                settings=self._settings,
            )
        self._deps.identity.emit_agent_event_fn(
            event_type="TASK_STARTED",
            tenant_id=prepared.run.tenant_id,
            project_id=prepared.project.project_id,
            run_id=prepared.run.run_id,
            issue_key=prepared.run.issue_key,
            agent_id=prepared.agent_id,
        )
        heartbeat_controller.start()
        try:
            workflow_result = self._runner.run(
                prepared.workflow_request,
                test_feedback_hook=_emit_test_feedback,
                stage_checkpoint_hook=lambda checkpoint: self._deps.execution.persist_stage_checkpoint_fn(
                    self._session,
                    run=prepared.run,
                    checkpoint=checkpoint,
                    execution_context=execution_context,
                    expected_worker_service_instance_id=prepared.worker_service_instance_id,
                    expected_claim_id=prepared.claim_id,
                ),
            )
            return RunOutcomePolicy(
                session=self._session,
                settings=self._settings,
                deps=self._deps,
                cleanup_run_workspaces_safe_fn=_cleanup_run_workspaces_safe,
            ).complete(
                prepared=prepared,
                workflow_result=workflow_result,
                execution_context=execution_context,
            )
        finally:
            heartbeat_controller.stop()

    def _emit_issue_assigned(self, *, run, agent_id: str) -> None:  # noqa: ANN001
        self._deps.identity.emit_agent_event_fn(
            event_type="ISSUE_ASSIGNED",
            tenant_id=run.tenant_id,
            project_id=run.project_id,
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
        )


def _run_matches_execution_ownership(
    run,  # noqa: ANN001
    *,
    expected_worker_service_instance_id: str | None,
    expected_claim_id: str | None,
    expected_status: str,
) -> bool:
    return (
        str(getattr(run, "status", "") or "").strip().lower() == str(expected_status or "").strip().lower()
        and str(getattr(run, "worker_service_instance_id", "") or "").strip()
        == str(expected_worker_service_instance_id or "").strip()
        and str(getattr(run, "claim_id", "") or "").strip() == str(expected_claim_id or "").strip()
    )


def _emit_queue_wait_metric(*, session, run, project_id: str | None, agent_id: str) -> None:  # noqa: ANN001
    if run.started_at is None or run.created_at is None:
        return
    wait_ms = max(0, int((run.started_at - run.created_at).total_seconds() * 1000))
    record_run_log_event(
        session=session,
        tenant_id=run.tenant_id,
        project_id=project_id,
        run_id=run.run_id,
        issue_key=run.issue_key,
        agent_id=agent_id,
        invocation_id=uuid4().hex,
        channel="worker",
        command="workflow.queue_wait",
        working_dir=None,
        stage="telemetry",
        attempt=None,
        stream="system",
        message=json.dumps(
            {
                "event_kind": "queue_wait",
                "queue_wait_ms": wait_ms,
                "created_at": run.created_at.isoformat(),
                "started_at": run.started_at.isoformat(),
            },
            sort_keys=True,
        ),
    )
    session.commit()


def _execution_context(*, workflow_request) -> dict[str, str] | None:  # noqa: ANN001
    context: dict[str, str] = {}
    for source_attr, field_name in (
        ("execution_repo_dir", "execution_repo_dir"),
        ("workspace_key", "workspace_key"),
        ("execution_branch", "execution_branch"),
        ("integration_branch", "integration_branch"),
        ("base_branch", "base_branch"),
        ("start_point_ref", "start_point_ref"),
        ("start_point_sha", "start_point_sha"),
    ):
        value = str(getattr(workflow_request, source_attr, "") or "").strip()
        if value:
            context[field_name] = value
    return context or None


def _cleanup_run_workspaces_safe(
    *,
    cleanup_run_workspaces_fn,
    logger,
    base_dir: str,
    tenant_id: str,
    project_id: str,
    run_id: str,
    workspace_key: str | None = None,
) -> None:  # noqa: ANN001
    try:
        cleanup_run_workspaces_fn(
            base_dir=base_dir,
            tenant_id=tenant_id,
            project_id=project_id,
            run_id=run_id,
            workspace_key=workspace_key,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "worker_run_workspace_cleanup_failed tenant_id=%s project_id=%s run_id=%s workspace_key=%s",
            tenant_id,
            project_id,
            run_id,
            workspace_key,
        )
