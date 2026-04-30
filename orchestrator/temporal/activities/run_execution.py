from __future__ import annotations

import logging
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select
from temporalio import activity

from orchestrator.core.config import get_settings
from orchestrator.core.run_human_input_service import _resume_workflow_from_human_input_answer_legacy
from orchestrator.core.workflow_execution_projection import WorkflowExecutionProjection
from orchestrator.core.workflow_step_runner import (
    complete_workflow_step_attempt,
    fail_workflow_step_attempt,
    start_workflow_step_attempt,
)
from orchestrator.core.workflow_type_catalog import (
    ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME,
    ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
    get_workflow_type,
)
from orchestrator.core.worker.execution_service import build_run_process_kwargs
from orchestrator.core.worker.run_lifecycle import claim_run_for_dispatch
from orchestrator.core.worker.runtime_factory import build_workflow_runner_for_session
from orchestrator.core.worker.process_service import process_claimed_run
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Run, RunHumanInputRequest, Tenant, WorkflowExecution
from orchestrator.temporal.payloads import (
    DevelopmentTeamRunActivityResult,
    DevelopmentTeamRunWorkflowInput,
    HumanInputResumeInput,
)

logger = logging.getLogger(__name__)


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
            runner = build_workflow_runner_for_session(session=session)
            processed = process_claimed_run(
                session=session,
                runner=runner,
                settings=settings,
                selection=SimpleNamespace(run=run, tenant=tenant, terminal_run=None),
                **build_run_process_kwargs(
                    session=session,
                    settings=settings,
                    worker_service_instance_id=str(getattr(run, "worker_service_instance_id", "") or "").strip() or None,
                ),
            )
            if processed is None:
                raise RuntimeError(f"Temporal run activity returned no run for workflow_id={workflow.workflow_id}")
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                summary=f"Run attempt {processed.run_id} finished with status {processed.status}",
            )
            session.commit()
            return _result_for_run(session=session, workflow_id=workflow.workflow_id, run=processed)
        except Exception as exc:  # noqa: BLE001
            logger.exception("temporal_run_execute_failed workflow_id=%s run_id=%s", workflow.workflow_id, run.run_id)
            fail_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                category="run_execution_failed",
                message=str(exc),
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
            resumed_run = _resume_workflow_from_human_input_answer_legacy(
                session=session,
                settings=settings,
                request=request,
            )
            claimed = claim_run_for_dispatch(
                session,
                run=resumed_run,
                expected_status="queued",
                worker_service_instance_id=f"temporal:{workflow.workflow_id}",
                claim_id=uuid4().hex,
            )
            if claimed is None:
                raise RuntimeError(f"Unable to claim resumed run {resumed_run.run_id} for temporal execution")
            tenant = session.get(Tenant, claimed.tenant_id)
            if tenant is None:
                raise RuntimeError(f"Temporal resume activity missing tenant {claimed.tenant_id}")
            runner = build_workflow_runner_for_session(session=session)
            processed = process_claimed_run(
                session=session,
                runner=runner,
                settings=settings,
                selection=SimpleNamespace(run=claimed, tenant=tenant, terminal_run=None),
                **build_run_process_kwargs(
                    session=session,
                    settings=settings,
                    worker_service_instance_id=str(getattr(claimed, "worker_service_instance_id", "") or "").strip() or None,
                ),
            )
            if processed is None:
                raise RuntimeError(f"Temporal resume activity returned no run for workflow_id={workflow.workflow_id}")
            complete_workflow_step_attempt(
                lifecycle=lifecycle,
                step=step,
                summary=f"Resumed run {processed.run_id} finished with status {processed.status}",
            )
            session.commit()
            return _result_for_run(
                session=session,
                workflow_id=workflow.workflow_id,
                run=processed,
                claim_id=str(getattr(claimed, "claim_id", "") or "").strip() or None,
            )
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
