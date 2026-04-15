from __future__ import annotations

import json
from dataclasses import dataclass
from time import perf_counter
from traceback import format_exception
from uuid import uuid4

from orchestrator.core.run_logs import record_run_log_event
from orchestrator.core.worker.stage_events import WorkerStageUpdate
from orchestrator.core.runs import mark_run_terminal
from orchestrator.core.worker.manual_pr_remediation_completion import publish_manual_pr_remediation_completion
from orchestrator.core.workflow.runner import WorkflowResult


@dataclass(frozen=True)
class FinalizationPlan:
    run: object
    workflow_result: WorkflowResult
    persisted_status: str
    last_error: str | None
    persisted_plan: dict[str, object] | None
    event_types: tuple[str, ...]
    tail_steps: tuple[str, ...]


class WorkflowFinalizer:
    def __init__(
        self,
        *,
        session,
        logger,
        finalize_workflow_result_fn,
        mark_run_terminal_fn=mark_run_terminal,
        run_status_failed: str,
        project_id: str | None,
        agent_id: str,
    ) -> None:  # noqa: ANN001
        self._session = session
        self._logger = logger
        self._finalize_workflow_result_fn = finalize_workflow_result_fn
        self._mark_run_terminal_fn = mark_run_terminal_fn
        self._run_status_failed = run_status_failed
        self._project_id = project_id
        self._agent_id = agent_id

    def finalize(
        self,
        *,
        run,
        workflow_result: WorkflowResult,
        stage_updates: list[WorkerStageUpdate | dict[str, str]],
        execution_context: dict[str, str] | None,
        expected_worker_service_instance_id: str | None,
        expected_claim_id: str | None = None,
    ) -> FinalizationPlan:  # noqa: ANN001
        _record_completion_step_event(
            session=self._session,
            run=run,
            agent_id=self._agent_id,
            step="finalize",
            status="started",
        )
        try:
            finalized_run = self._finalize_workflow_result_fn(
                self._session,
                run=run,
                workflow_result=workflow_result,
                stage_updates=stage_updates,
                execution_context=execution_context,
                expected_worker_service_instance_id=expected_worker_service_instance_id,
                expected_claim_id=expected_claim_id,
            )
        except Exception as exc:  # noqa: BLE001
            failure_message = f"Run finalization failed after workflow execution: {type(exc).__name__}: {exc}"
            self._logger.exception(
                "worker_run_finalize_failed tenant_id=%s project_id=%s run_id=%s issue_key=%s error=%s",
                getattr(run, "tenant_id", ""),
                self._project_id,
                getattr(run, "run_id", ""),
                getattr(run, "issue_key", ""),
                exc,
            )
            self._session.rollback()
            try:
                _record_completion_step_event(
                    session=self._session,
                    run=run,
                    agent_id=self._agent_id,
                    step="finalize",
                    status="failed",
                    error_class=type(exc).__name__,
                    error_message=str(exc),
                    stack_trace="".join(format_exception(type(exc), exc, exc.__traceback__)),
                )
            except Exception:  # noqa: BLE001
                self._logger.exception(
                    "worker_run_finalize_failure_log_failed tenant_id=%s project_id=%s run_id=%s issue_key=%s",
                    getattr(run, "tenant_id", ""),
                    self._project_id,
                    getattr(run, "run_id", ""),
                    getattr(run, "issue_key", ""),
                )
            finalized_run = self._mark_run_terminal_fn(
                self._session,
                run_id=run.run_id,
                terminal_status=self._run_status_failed,
                last_error=failure_message,
                expected_claim_id=expected_claim_id,
            )
            return FinalizationPlan(
                run=finalized_run,
                workflow_result=workflow_result,
                persisted_status=getattr(finalized_run, "status", self._run_status_failed),
                last_error=getattr(finalized_run, "last_error", failure_message),
                persisted_plan=dict(getattr(finalized_run, "plan", {}) or {}) if isinstance(getattr(finalized_run, "plan", None), dict) else None,
                event_types=("RUN_FAILED", "TASK_FAILED"),
                tail_steps=(),
            )

        _record_completion_step_event(
            session=self._session,
            run=run,
            agent_id=self._agent_id,
            step="finalize",
            status="succeeded",
        )
        return FinalizationPlan(
            run=finalized_run,
            workflow_result=workflow_result,
            persisted_status=str(getattr(finalized_run, "status", "") or "").strip().lower(),
            last_error=str(getattr(finalized_run, "last_error", "") or "").strip() or None,
            persisted_plan=dict(getattr(finalized_run, "plan", {}) or {}) if isinstance(getattr(finalized_run, "plan", None), dict) else None,
            event_types=_event_types_for(
                workflow_result=workflow_result,
                persisted_status=str(getattr(finalized_run, "status", "") or "").strip().lower(),
            ),
            tail_steps=("orchestration_trace", "jira_feedback", "manual_pr_reporting", "workspace_cleanup"),
        )


class CompletionTailExecutor:
    def __init__(
        self,
        *,
        session,
        tenant,
        project,
        settings,
        logger,
        send_jira_message_fn,
        cleanup_run_workspaces_fn,
        base_dir: str,
        jira_issue_url: str | None,
        agent_id: str,
        workspace_key: str | None,
    ) -> None:  # noqa: ANN001
        self._session = session
        self._tenant = tenant
        self._project = project
        self._settings = settings
        self._logger = logger
        self._send_jira_message_fn = send_jira_message_fn
        self._cleanup_run_workspaces_fn = cleanup_run_workspaces_fn
        self._base_dir = base_dir
        self._jira_issue_url = jira_issue_url
        self._agent_id = agent_id
        self._workspace_key = workspace_key

    def execute(self, plan: FinalizationPlan) -> None:
        if not plan.tail_steps:
            return
        run = plan.run
        workflow_result = plan.workflow_result
        failures: list[dict[str, str]] = []
        for step in plan.tail_steps:
            if step == "orchestration_trace":
                def fn() -> None:
                    _emit_orchestrated_trace_logs(
                        session=self._session,
                        run=run,
                        workflow_result=workflow_result,
                        agent_id=self._agent_id,
                    )
            elif step == "jira_feedback":
                def fn() -> None:
                    _emit_detailed_jira_feedback(
                        session=self._session,
                        tenant=self._tenant,
                        run=run,
                        settings=self._settings,
                        workflow_result=workflow_result,
                        send_jira_message_fn=self._send_jira_message_fn,
                    )
            elif step == "manual_pr_reporting":
                def fn() -> None:
                    publish_manual_pr_remediation_completion(
                        session=self._session,
                        tenant=self._tenant,
                        project=self._project,
                        run=run,
                        workflow_result=workflow_result,
                        settings=self._settings,
                        issue_url=self._jira_issue_url,
                        logger_override=self._logger,
                        terminal_status=getattr(run, "status", None),
                    )
            elif step == "workspace_cleanup":
                def fn() -> None:
                    self._cleanup_run_workspaces_fn(
                        base_dir=self._base_dir,
                        tenant_id=run.tenant_id,
                        project_id=self._project.project_id,
                        run_id=run.run_id,
                        workspace_key=self._workspace_key,
                    )
            else:
                continue
            _run_completion_step(
                session=self._session,
                run=run,
                agent_id=self._agent_id,
                logger=self._logger,
                step=step,
                failures=failures,
                fn=fn,
            )


def _event_types_for(*, workflow_result: WorkflowResult, persisted_status: str) -> tuple[str, ...]:
    if persisted_status == "succeeded":
        return ("TASK_COMPLETED",)
    if persisted_status == "blocked":
        return ("RUN_BLOCKED",)
    if persisted_status != "failed":
        return ()
    event_types: list[str] = ["RUN_FAILED", "TASK_FAILED"]
    diagnostics_stage = (
        workflow_result.diagnostics.stage.upper()
        if workflow_result.diagnostics is not None and workflow_result.diagnostics.stage
        else ""
    )
    if diagnostics_stage == "BUILD":
        event_types.append("BUILD_FAILED")
    if diagnostics_stage == "TEST":
        event_types.append("TEST_FAILED")
    return tuple(event_types)


def _run_completion_step(
    *,
    session,
    run,
    agent_id: str,
    logger,
    step: str,
    failures: list[dict[str, str]],
    fn,
) -> None:  # noqa: ANN001
    _record_completion_step_event(
        session=session,
        run=run,
        agent_id=agent_id,
        step=step,
        status="started",
    )
    started = perf_counter()
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        duration_ms = max(0, int((perf_counter() - started) * 1000))
        stack_trace = "".join(format_exception(type(exc), exc, exc.__traceback__))
        try:
            session.rollback()
        except Exception:  # noqa: BLE001
            logger.exception(
                "worker_completion_step_rollback_failed tenant_id=%s project_id=%s run_id=%s issue_key=%s step=%s",
                getattr(run, "tenant_id", ""),
                getattr(run, "project_id", ""),
                getattr(run, "run_id", ""),
                getattr(run, "issue_key", ""),
                step,
            )
        logger.exception(
            "worker_completion_step_failed tenant_id=%s project_id=%s run_id=%s issue_key=%s step=%s error_class=%s error=%s",
            getattr(run, "tenant_id", ""),
            getattr(run, "project_id", ""),
            getattr(run, "run_id", ""),
            getattr(run, "issue_key", ""),
            step,
            type(exc).__name__,
            exc,
        )
        _record_completion_step_event(
            session=session,
            run=run,
            agent_id=agent_id,
            step=step,
            status="failed",
            duration_ms=duration_ms,
            error_class=type(exc).__name__,
            error_message=str(exc),
            stack_trace=stack_trace,
        )
        failures.append(
            {
                "step": step,
                "error_class": type(exc).__name__,
                "error_message": str(exc),
            }
        )
        return
    duration_ms = max(0, int((perf_counter() - started) * 1000))
    _record_completion_step_event(
        session=session,
        run=run,
        agent_id=agent_id,
        step=step,
        status="succeeded",
        duration_ms=duration_ms,
    )


def _record_completion_step_event(
    *,
    session,
    run,
    agent_id: str,
    step: str,
    status: str,
    duration_ms: int | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
    stack_trace: str | None = None,
) -> None:  # noqa: ANN001
    payload = {
        "event_kind": f"completion_step_{status}",
        "step": step,
        "status": status,
    }
    if duration_ms is not None:
        payload["duration_ms"] = duration_ms
    if error_class:
        payload["error_class"] = error_class
    if error_message:
        payload["error_message"] = error_message
    if stack_trace:
        payload["stack_trace"] = stack_trace
    record_run_log_event(
        session=session,
        tenant_id=run.tenant_id,
        project_id=getattr(run, "project_id", None),
        run_id=run.run_id,
        issue_key=run.issue_key,
        agent_id=agent_id,
        invocation_id=uuid4().hex,
        channel="worker",
        command="workflow.completion",
        working_dir=None,
        stage="telemetry",
        attempt=None,
        stream="system",
        message=json.dumps(payload, sort_keys=True),
    )
    session.commit()


def _emit_orchestrated_trace_logs(
    *,
    session,
    run,
    workflow_result: WorkflowResult,
    agent_id: str,
) -> None:  # noqa: ANN001
    stage_trace = list(workflow_result.orchestration_stage_trace or [])
    workstream_trace = list(workflow_result.orchestration_workstream_trace or [])
    for entry in stage_trace:
        record_run_log_event(
            session=session,
            tenant_id=run.tenant_id,
            project_id=getattr(run, "project_id", None),
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
            invocation_id=uuid4().hex,
            channel="worker",
            command=f"workflow.{entry.get('stage')}",
            working_dir=None,
            stage=str(entry.get("stage") or "workflow"),
            attempt=int(entry.get("attempt") or 0) or None,
            stream="system",
            message=json.dumps(
                {
                    "event_kind": "orchestrated_stage_event",
                    "stage": entry.get("stage"),
                    "status": entry.get("status"),
                    "summary": entry.get("summary"),
                    "attempt": entry.get("attempt"),
                },
                sort_keys=True,
            ),
        )
    for entry in workstream_trace:
        record_run_log_event(
            session=session,
            tenant_id=run.tenant_id,
            project_id=getattr(run, "project_id", None),
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
            invocation_id=uuid4().hex,
            channel="worker",
            command=f"workflow.workstream.{entry.get('workstream')}",
            working_dir=None,
            stage="telemetry",
            attempt=int(entry.get("attempt") or 0) or None,
            stream="system",
            message=json.dumps(
                {
                    "event_kind": "orchestrated_workstream_event",
                    "workstream": entry.get("workstream"),
                    "status": entry.get("status"),
                    "summary": entry.get("summary"),
                    "attempt": entry.get("attempt"),
                },
                sort_keys=True,
            ),
        )
    session.commit()


def _emit_detailed_jira_feedback(
    *,
    session,
    tenant,
    run,
    settings,
    workflow_result: WorkflowResult,
    send_jira_message_fn,
) -> None:  # noqa: ANN001
    if workflow_result.dev_rationale:
        send_jira_message_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage="dev_rationale",
            message="\n".join(f"- {item}" for item in workflow_result.dev_rationale),
            settings=settings,
        )
    if workflow_result.review_summary:
        send_jira_message_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage="review_summary",
            message="\n".join(f"- {item}" for item in workflow_result.review_summary),
            settings=settings,
        )
    if workflow_result.review_feedback:
        send_jira_message_fn(
            session=session,
            tenant=tenant,
            issue_key=run.issue_key,
            stage="review_feedback",
            message=workflow_result.review_feedback,
            settings=settings,
        )
