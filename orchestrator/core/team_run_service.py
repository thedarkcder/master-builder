from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.team_run_engine import team_run_engine
from orchestrator.storage.models import Run

TEAM_RUN_ENTRY_STAGE = "team"


def initialize_team_run(*, session: Session, run_id: str) -> Run:
    return team_run_engine.initialize(session=session, run_id=run_id)


def execute_next_ready_team_task(*, session: Session, run_id: str) -> tuple[Run, str | None]:
    return team_run_engine.advance_auto(session=session, run_id=run_id)


def complete_team_task(
    *,
    session: Session,
    run_id: str,
    task_key: str,
    artifact_payload: dict[str, Any] | None = None,
    summary: str | None = None,
) -> Run:
    return team_run_engine.complete_task(
        session=session,
        run_id=run_id,
        task_key=task_key,
        artifact_payload=artifact_payload,
        summary=summary,
    )


def submit_team_approval(
    *,
    session: Session,
    run_id: str,
    task_key: str,
    decision: str,
    comment: str | None = None,
) -> Run:
    return team_run_engine.submit_approval(
        session=session,
        run_id=run_id,
        task_key=task_key,
        decision=decision,
        comment=comment,
    )


def resume_team_human_input(
    *,
    session: Session,
    request_id: str,
) -> Run:
    return team_run_engine.resume_human_input(session=session, request_id=request_id)
