from __future__ import annotations

import logging
import threading
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select
from temporalio import activity

from orchestrator.core.config import get_settings
from orchestrator.core.runs.human_input_service import resume_run_from_human_input_answer
from orchestrator.core.runs.service import mark_run_terminal
from orchestrator.core.workflow.operation_service import (
    ACTIVE_OPERATION_ATTEMPT_STATUSES,
    touch_workflow_operation_attempt_heartbeat,
)
from orchestrator.core.workflow.execution_projection import WorkflowExecutionProjection
from orchestrator.core.workflow.step_runner import (
    complete_workflow_step_attempt,
    fail_workflow_step_attempt,
    start_workflow_step_attempt,
)
from orchestrator.core.workflow.work_units import run_work_unit
from orchestrator.core.workflow.type_catalog import (
    ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME,
    ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
    get_workflow_type,
)
from orchestrator.core.worker.execution_service import build_run_process_kwargs
from orchestrator.core.worker.run_execution_context import resolve_run_execution_policy_context
from orchestrator.core.worker.run_lifecycle import claim_run_for_dispatch
from orchestrator.core.worker.runtime_factory import build_workflow_runner_for_session
from orchestrator.core.worker.process_service import process_claimed_run
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Run, RunHumanInputRequest, Tenant, WorkflowExecution, WorkflowOperationAttempt
from orchestrator.temporal.payloads import (
    DevelopmentTeamRunActivityResult,
    DevelopmentTeamRunWorkflowInput,
    HumanInputResumeInput,
)

logger = logging.getLogger(__name__)


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


def _run_process_kwargs_with_operation_attempt_heartbeat(
    *,
    session,
    settings,
    step,
    worker_service_instance_id: str | None,
) -> dict[str, object]:  # noqa: ANN001
    kwargs = build_run_process_kwargs(
        session=session,
        settings=settings,
        worker_service_instance_id=worker_service_instance_id,
    )
    build_run_heartbeat_controller_fn = kwargs["build_run_heartbeat_controller_fn"]
    attempt_id = str(step.attempt.attempt_id)

    def _build_heartbeat_controller(*, run_id, worker_service_instance_id, claim_id, heartbeat_interval_seconds):  # noqa: ANN001
        run_controller = build_run_heartbeat_controller_fn(
            run_id=run_id,
            worker_service_instance_id=worker_service_instance_id,
            claim_id=claim_id,
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        operation_controller = _WorkflowOperationAttemptHeartbeatController(
            database_url=settings.database_url,
            attempt_id=attempt_id,
            lease_owner=f"temporal:{step.workflow_id}:{run_id}",
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        return _CompositeHeartbeatController(run_controller, operation_controller)

    kwargs["build_run_heartbeat_controller_fn"] = _build_heartbeat_controller
    return kwargs


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


def _claim_queued_run_for_temporal_activity(*, session, workflow: WorkflowExecution, run: Run) -> Run:  # noqa: ANN001
    normalized_status = str(getattr(run, "status", "") or "").strip().lower()
    if normalized_status != "queued":
        return run
    temporal_owner = f"temporal:{workflow.workflow_id}"
    claimed = claim_run_for_dispatch(
        session,
        run=run,
        expected_status="queued",
        worker_service_instance_id=temporal_owner,
        claim_id=uuid4().hex,
    )
    if claimed is None:
        raise RuntimeError(f"Unable to claim queued run {run.run_id} for temporal execution")
    return claimed


def _claimed_run_ref_for_temporal_activity(*, session, tenant: Tenant, run: Run):  # noqa: ANN001, ANN202
    policy_context = resolve_run_execution_policy_context(
        session,
        tenant=tenant,
        run=run,
    )
    return SimpleNamespace(
        run=run,
        tenant=tenant,
        project=policy_context.project,
        effective_policy=policy_context.effective_policy,
        run_id=run.run_id,
        claim_id=str(getattr(run, "claim_id", "") or "").strip(),
        worker_service_instance_id=str(getattr(run, "worker_service_instance_id", "") or "").strip(),
        status=str(getattr(run, "status", "") or "").strip(),
    )


@activity.defn(name="execute_claimed_run_activity")
def execute_claimed_run_activity(payload: DevelopmentTeamRunWorkflowInput) -> DevelopmentTeamRunActivityResult:
    settings = get_settings()
    session_factory = create_session_factory()
    with session_factory() as session:
        workflow = session.get(WorkflowExecution, str(payload.workflow_id))
        run = session.get(Run, str(payload.run_id))
        if workflow is None or run is None:
            raise RuntimeError(f"Temporal run activity missing workflow/run for workflow_id={payload.workflow_id} run_id={payload.run_id}")
        tenant = session.get(Tenant, run.tenant_id)
        if tenant is None:
            raise RuntimeError(f"Temporal run activity missing tenant {run.tenant_id}")

        workflow_type = get_workflow_type(session, workflow_type_key=workflow.workflow_type_key)
        lifecycle = WorkflowExecutionProjection(session=session, workflow=workflow, workflow_type=workflow_type)
        step = start_workflow_step_attempt(
            lifecycle=lifecycle,
            run_id=run.run_id,
            operation_type=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
            idempotency_key=f"run-attempt:{run.run_id}",
            target_system="workflow_engine",
            target_ref=run.run_id,
            summary=f"Execute run attempt {run.attempt_number}",
        )
        session.commit()

        try:
            def _execute(_context) -> DevelopmentTeamRunActivityResult:  # noqa: ANN001
                claimed_run = _claim_queued_run_for_temporal_activity(session=session, workflow=workflow, run=run)
                runner = build_workflow_runner_for_session(session=session)
                claimed_worker_service_instance_id = (
                    str(getattr(claimed_run, "worker_service_instance_id", "") or "").strip() or None
                )
                claimed_claim_id = str(getattr(claimed_run, "claim_id", "") or "").strip()
                processed = process_claimed_run(
                    session=session,
                    runner=runner,
                    settings=settings,
                    selection=SimpleNamespace(
                        run=claimed_run,
                        tenant=tenant,
                        terminal_run=None,
                        claimed_run=_claimed_run_ref_for_temporal_activity(
                            session=session,
                            tenant=tenant,
                            run=claimed_run,
                        ),
                    ),
                    **_run_process_kwargs_with_operation_attempt_heartbeat(
                        session=session,
                        settings=settings,
                        step=step,
                        worker_service_instance_id=claimed_worker_service_instance_id,
                    ),
                )
                if processed is None:
                    raise RuntimeError(f"Temporal run activity returned no run for workflow_id={workflow.workflow_id}")
                return _result_for_run(
                    session=session,
                    workflow_id=workflow.workflow_id,
                    run=processed,
                    claim_id=claimed_claim_id or None,
                )

            result = run_work_unit(
                session,
                operation=step.operation,
                operation_attempt=step.attempt,
                unit_key="run_attempt_execution.runtime_invocation",
                idempotency_key=f"run:{run.run_id}:runtime_invocation:{step.attempt.attempt_id}",
                input_payload={
                    "workflow_id": workflow.workflow_id,
                    "run_id": run.run_id,
                    "attempt_number": run.attempt_number,
                    "operation_attempt_id": step.attempt.attempt_id,
                    "issue_key": run.issue_key,
                },
                execute=_execute,
                serialize=_run_result_to_payload,
                deserialize=_run_result_from_payload,
            )
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                summary=f"Run attempt {result.run_id} finished with status {result.status}",
            )
            session.commit()
            return result
        except Exception as exc:  # noqa: BLE001
            logger.exception("temporal_run_execute_failed workflow_id=%s run_id=%s", workflow.workflow_id, run.run_id)
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                category="run_execution_failed",
                message=str(exc),
            )
            if str(getattr(run, "status", "") or "").strip().lower() in {"dispatching", "running"}:
                mark_run_terminal(
                    session,
                    run_id=run.run_id,
                    terminal_status="failed",
                    last_error=f"Temporal run activity failed: {exc}",
                    expected_worker_service_instance_id=str(getattr(run, "worker_service_instance_id", "") or "").strip()
                    or None,
                    expected_claim_id=str(payload.claim_id or run.claim_id or "").strip() or None,
                )
            session.commit()
            raise


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
                temporal_owner = f"temporal:{workflow.workflow_id}"
                claimed = None
                if str(resumed_run.status or "").strip().lower() == "dispatching":
                    existing_owner = str(getattr(resumed_run, "worker_service_instance_id", "") or "").strip()
                    existing_claim_id = str(getattr(resumed_run, "claim_id", "") or "").strip()
                    if existing_owner == temporal_owner and existing_claim_id:
                        claimed = resumed_run
                if claimed is None:
                    claimed = claim_run_for_dispatch(
                        session,
                        run=resumed_run,
                        expected_status="queued",
                        worker_service_instance_id=temporal_owner,
                        claim_id=uuid4().hex,
                    )
                if claimed is None:
                    raise RuntimeError(f"Unable to claim resumed run {resumed_run.run_id} for temporal execution")
                step.operation.run_id = claimed.run_id
                session.flush()
                tenant = session.get(Tenant, claimed.tenant_id)
                if tenant is None:
                    raise RuntimeError(f"Temporal resume activity missing tenant {claimed.tenant_id}")
                runner = build_workflow_runner_for_session(session=session)
                claimed_ref = _claimed_run_ref_for_temporal_activity(
                    session=session,
                    tenant=tenant,
                    run=claimed,
                )
                processed = process_claimed_run(
                    session=session,
                    runner=runner,
                    settings=settings,
                    selection=SimpleNamespace(
                        run=claimed,
                        tenant=tenant,
                        terminal_run=None,
                        claimed_run=claimed_ref,
                    ),
                    **_run_process_kwargs_with_operation_attempt_heartbeat(
                        session=session,
                        settings=settings,
                        step=step,
                        worker_service_instance_id=str(getattr(claimed, "worker_service_instance_id", "") or "").strip()
                        or None,
                    ),
                )
                if processed is None:
                    raise RuntimeError(f"Temporal resume activity returned no run for workflow_id={workflow.workflow_id}")
                return _result_for_run(
                    session=session,
                    workflow_id=workflow.workflow_id,
                    run=processed,
                    claim_id=str(getattr(claimed, "claim_id", "") or "").strip() or None,
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
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                summary=f"Resumed run {result.run_id} finished with status {result.status}",
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
