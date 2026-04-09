from __future__ import annotations

from temporalio import activity

from orchestrator.core.team_run_engine import team_run_engine
from orchestrator.storage.db import create_session_factory
from orchestrator.temporal.team_run_payloads import (
    TeamRunApprovalInput,
    TeamRunHumanInputInput,
    TeamRunTaskCompletionInput,
    TeamRunUpdateResult,
)


@activity.defn
def initialize_team_run_activity(run_id: str) -> TeamRunUpdateResult:
    session_factory = create_session_factory()
    with session_factory() as session:
        run = team_run_engine.initialize(session=session, run_id=run_id)
        team_run = run.plan["context"]["execution_context"]["team_run"]
        return TeamRunUpdateResult(
            operation_id="initialize",
            run_id=str(run.run_id),
            task_key=None,
            team_status=str(team_run.get("status") or "").strip() or None,
            run_status=str(run.status or "").strip() or None,
        )


@activity.defn
def execute_ready_team_task_activity(run_id: str) -> TeamRunUpdateResult:
    session_factory = create_session_factory()
    with session_factory() as session:
        run, executed_task_key = team_run_engine.advance_auto(session=session, run_id=run_id)
        team_run = run.plan["context"]["execution_context"]["team_run"]
        return TeamRunUpdateResult(
            operation_id=f"auto:{run_id}",
            run_id=str(run.run_id),
            task_key=executed_task_key,
            team_status=str(team_run.get("status") or "").strip() or None,
            run_status=str(run.status or "").strip() or None,
        )


@activity.defn
def complete_team_task_activity(payload: TeamRunTaskCompletionInput) -> TeamRunUpdateResult:
    session_factory = create_session_factory()
    with session_factory() as session:
        run = team_run_engine.complete_task(
            session=session,
            run_id=payload.run_id,
            task_key=payload.task_key,
            artifact_payload=payload.artifact_payload,
            summary=payload.summary,
        )
        team_run = run.plan["context"]["execution_context"]["team_run"]
        return TeamRunUpdateResult(
            operation_id=payload.operation_id,
            run_id=str(run.run_id),
            task_key=payload.task_key,
            team_status=str(team_run.get("status") or "").strip() or None,
            run_status=str(run.status or "").strip() or None,
        )


@activity.defn
def submit_team_approval_activity(payload: TeamRunApprovalInput) -> TeamRunUpdateResult:
    session_factory = create_session_factory()
    with session_factory() as session:
        run = team_run_engine.submit_approval(
            session=session,
            run_id=payload.run_id,
            task_key=payload.task_key,
            decision=payload.decision,
            comment=payload.comment,
        )
        team_run = run.plan["context"]["execution_context"]["team_run"]
        return TeamRunUpdateResult(
            operation_id=payload.operation_id,
            run_id=str(run.run_id),
            task_key=payload.task_key,
            team_status=str(team_run.get("status") or "").strip() or None,
            run_status=str(run.status or "").strip() or None,
        )


@activity.defn
def resume_team_human_input_activity(payload: TeamRunHumanInputInput) -> TeamRunUpdateResult:
    session_factory = create_session_factory()
    with session_factory() as session:
        run = team_run_engine.resume_human_input(
            session=session,
            request_id=payload.request_id,
        )
        team_run = run.plan["context"]["execution_context"]["team_run"]
        return TeamRunUpdateResult(
            operation_id=payload.operation_id,
            run_id=str(run.run_id),
            task_key=None,
            team_status=str(team_run.get("status") or "").strip() or None,
            run_status=str(run.status or "").strip() or None,
        )
