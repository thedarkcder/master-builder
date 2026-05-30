from __future__ import annotations

import logging
import threading

from sqlalchemy import select
from temporalio import activity

from orchestrator.core.config import get_settings
from orchestrator.core.runs.human_input_service import resume_run_from_human_input_answer
from orchestrator.core.workflow.operation_service import (
    ACTIVE_OPERATION_ATTEMPT_STATUSES,
    touch_workflow_operation_attempt_heartbeat,
)
from orchestrator.core.workflow.execution_projection import WorkflowExecutionProjection
from orchestrator.core.workflow.step_runner import (
    complete_workflow_step_attempt,
    fail_workflow_step_attempt,
    start_workflow_step_attempt,
    wait_workflow_step_attempt,
)
from orchestrator.core.workflow.work_units import run_work_unit
from orchestrator.core.workflow.type_catalog import (
    ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME,
    get_workflow_type,
)
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Run, RunHumanInputRequest, WorkflowExecution, WorkflowOperationAttempt
from orchestrator.temporal.payloads import (
    DevelopmentTeamRunActivityResult,
    DevelopmentTeamRunWorkflowInput,
    HumanInputResumeInput,
)

logger = logging.getLogger(__name__)
_CLAIMABLE_OR_EXECUTABLE_RUN_STATUSES = {"queued", "dispatching", "running"}
_SUCCESS_RUN_STATUSES = {"succeeded"}
_FAILED_RUN_STATUSES = {"blocked", "failed", "cancelled"}


class _CompositeHeartbeatController:
    def __init__(self, *controllers) -> None:  # noqa: ANN001
        self._controllers = controllers

    def start(self) -> None:
        for controller in self._controllers:
            controller.start()

    def stop(self) -> None:
        for controller in reversed(self._controllers):
            controller.stop()


class _WorkflowOperationAttemptHeartbeatController:
    def __init__(
        self,
        *,
        database_url: str,
        attempt_id: str,
        lease_owner: str,
        heartbeat_interval_seconds: int,
    ) -> None:
        self._session_factory = create_session_factory(database_url)
        self._attempt_id = attempt_id
        self._lease_owner = lease_owner
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name=f"workflow-operation-heartbeat-{self._attempt_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, float(self._heartbeat_interval_seconds)))

    def _run(self) -> None:
        interval_seconds = max(5, int(self._heartbeat_interval_seconds))
        while not self._stop_event.wait(interval_seconds):
            if not self._run_once():
                return

    def _run_once(self) -> bool:
        try:
            with self._session_factory() as session:
                attempt = session.get(WorkflowOperationAttempt, self._attempt_id)
                if attempt is None or str(attempt.status or "").strip().lower() not in ACTIVE_OPERATION_ATTEMPT_STATUSES:
                    return False
                touch_workflow_operation_attempt_heartbeat(
                    session,
                    attempt=attempt,
                    lease_owner=self._lease_owner,
                )
                session.commit()
                return True
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "workflow_operation_attempt_heartbeat_failed attempt_id=%s error=%s",
                self._attempt_id,
                exc,
            )
            return True


def _pending_request_id(*, session, workflow_id: str) -> str | None:
    request_id = session.execute(
        select(RunHumanInputRequest.request_id)
        .where(
            RunHumanInputRequest.workflow_id == workflow_id,
            RunHumanInputRequest.status == "pending",
        )
        .order_by(RunHumanInputRequest.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return str(request_id or "").strip() or None


def _result_for_run(*, session, workflow_id: str, run: Run, claim_id: str | None = None) -> DevelopmentTeamRunActivityResult:
    return DevelopmentTeamRunActivityResult(
        workflow_id=workflow_id,
        run_id=str(run.run_id),
        status=str(run.status),
        issue_key=str(run.issue_key),
        claim_id=claim_id,
        pending_request_id=(
            _pending_request_id(session=session, workflow_id=workflow_id)
            if str(run.status or "").strip().lower() == "waiting_for_input"
            else None
        ),
        last_error=str(getattr(run, "last_error", "") or "").strip() or None,
    )


def _run_result_from_payload(payload: dict[str, object]) -> DevelopmentTeamRunActivityResult:
    return DevelopmentTeamRunActivityResult(
        workflow_id=str(payload["workflow_id"]),
        run_id=str(payload["run_id"]),
        status=str(payload["status"]),
        issue_key=str(payload["issue_key"]),
        claim_id=str(payload.get("claim_id") or "").strip() or None,
        pending_request_id=str(payload.get("pending_request_id") or "").strip() or None,
        last_error=str(payload.get("last_error") or "").strip() or None,
    )


def _run_result_to_payload(result: DevelopmentTeamRunActivityResult) -> dict[str, object]:
    return {
        "workflow_id": result.workflow_id,
        "run_id": result.run_id,
        "status": result.status,
        "issue_key": result.issue_key,
        "claim_id": result.claim_id,
        "pending_request_id": result.pending_request_id,
        "last_error": result.last_error,
    }


def _finish_workflow_step_for_run_result(
    *,
    lifecycle: WorkflowExecutionProjection,
    step,
    result: DevelopmentTeamRunActivityResult,
    action_label: str,
) -> None:  # noqa: ANN001
    result_status = str(result.status or "").strip().lower()
    summary = f"{action_label} {result.run_id} finished with status {result_status}"
    if result_status in _SUCCESS_RUN_STATUSES:
        complete_workflow_step_attempt(lifecycle=lifecycle, step=step, summary=summary)
        return
    if result_status == "waiting_for_input":
        wait_workflow_step_attempt(lifecycle=lifecycle, step=step, summary=summary)
        return
    if result_status in _FAILED_RUN_STATUSES:
        fail_workflow_step_attempt(
            lifecycle=lifecycle,
            step=step,
            category=f"{action_label.lower().replace(' ', '_')}_{result_status}",
            message=result.last_error or summary,
        )
        return
    complete_workflow_step_attempt(lifecycle=lifecycle, step=step, summary=summary)


@activity.defn(name="execute_claimed_run_activity")
def execute_claimed_run_activity(payload: DevelopmentTeamRunWorkflowInput) -> DevelopmentTeamRunActivityResult:
    session_factory = create_session_factory()
    with session_factory() as session:
        workflow = session.get(WorkflowExecution, str(payload.workflow_id))
        run = session.get(Run, str(payload.run_id))
        if workflow is None or run is None:
            raise RuntimeError(f"Temporal run activity missing workflow/run for workflow_id={payload.workflow_id} run_id={payload.run_id}")
        return _result_for_run(
            session=session,
            workflow_id=workflow.workflow_id,
            run=run,
            claim_id=str(getattr(run, "claim_id", "") or "").strip() or None,
        )


@activity.defn(name="resume_human_input_activity")
def resume_human_input_activity(payload: HumanInputResumeInput) -> DevelopmentTeamRunActivityResult:
    settings = get_settings()
    session_factory = create_session_factory()
    with session_factory() as session:
        request = session.get(RunHumanInputRequest, str(payload.request_id))
        if request is None:
            raise RuntimeError(f"Temporal resume activity missing request {payload.request_id}")
        workflow = session.get(WorkflowExecution, str(request.workflow_id))
        if workflow is None:
            raise RuntimeError(f"Temporal resume activity missing workflow {request.workflow_id}")
        workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
        lifecycle = WorkflowExecutionProjection(session=session, workflow=workflow, workflow_type=workflow_type)
        step = start_workflow_step_attempt(
            lifecycle=lifecycle,
            run_id=request.source_run_id,
            operation_type=ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME,
            idempotency_key=f"human-input-resume:{request.request_id}",
            target_system="workflow_engine",
            target_ref=request.request_id,
            summary="Resume workflow from human input",
        )
        session.commit()

        try:
            def _execute(_context) -> DevelopmentTeamRunActivityResult:  # noqa: ANN001
                resumed_run = resume_run_from_human_input_answer(
                    session=session,
                    settings=settings,
                    request=request,
                )
                step.operation.run_id = resumed_run.run_id
                session.flush()
                return _result_for_run(
                    session=session,
                    workflow_id=workflow.workflow_id,
                    run=resumed_run,
                    claim_id=str(getattr(resumed_run, "claim_id", "") or "").strip() or None,
                )

            result = run_work_unit(
                session,
                operation=step.operation,
                operation_attempt=step.attempt,
                unit_key="human_input_resume.resume",
                idempotency_key=f"human-input-resume:{request.request_id}",
                input_payload={"request_id": request.request_id, "workflow_id": workflow.workflow_id},
                execute=_execute,
                serialize=_run_result_to_payload,
                deserialize=_run_result_from_payload,
            )
            _finish_workflow_step_for_run_result(
                lifecycle=lifecycle,
                step=step,
                result=result,
                action_label="Resumed run",
            )
            session.commit()
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("temporal_resume_execute_failed workflow_id=%s request_id=%s", workflow.workflow_id, request.request_id)
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                category="human_input_resume_failed",
                message=str(exc),
            )
            session.commit()
            raise
