from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import ProjectAutomation, ProjectAutomationExecution


def list_project_automations(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
) -> tuple[ProjectAutomation, ...]:
    return tuple(
        session.execute(
            select(ProjectAutomation)
            .where(
                ProjectAutomation.tenant_id == tenant_id,
                ProjectAutomation.project_id == project_id,
            )
            .order_by(ProjectAutomation.kind.asc())
        ).scalars()
    )


def get_project_automation(
    *,
    session: Session,
    automation_id: str,
) -> ProjectAutomation | None:
    normalized = str(automation_id or "").strip()
    if not normalized:
        return None
    return session.get(ProjectAutomation, normalized)


def get_project_automation_by_kind(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    kind: str,
) -> ProjectAutomation | None:
    normalized_kind = str(kind or "").strip()
    if not normalized_kind:
        return None
    return session.execute(
        select(ProjectAutomation).where(
            ProjectAutomation.tenant_id == tenant_id,
            ProjectAutomation.project_id == project_id,
            ProjectAutomation.kind == normalized_kind,
        )
    ).scalar_one_or_none()


def list_due_project_automations(
    *,
    session: Session,
    now: datetime,
) -> tuple[ProjectAutomation, ...]:
    return tuple(
        session.execute(
            select(ProjectAutomation)
            .where(
                ProjectAutomation.enabled.is_(True),
                ProjectAutomation.next_run_at <= now,
            )
            .order_by(
                ProjectAutomation.next_run_at.asc(), ProjectAutomation.created_at.asc()
            )
        ).scalars()
    )


def get_execution_by_dedupe_key(
    *,
    session: Session,
    dedupe_key: str,
) -> ProjectAutomationExecution | None:
    normalized = str(dedupe_key or "").strip()
    if not normalized:
        return None
    return session.execute(
        select(ProjectAutomationExecution).where(
            ProjectAutomationExecution.dedupe_key == normalized
        )
    ).scalar_one_or_none()


def get_project_automation_execution(
    *,
    session: Session,
    execution_id: str,
) -> ProjectAutomationExecution | None:
    normalized = str(execution_id or "").strip()
    if not normalized:
        return None
    return session.get(ProjectAutomationExecution, normalized)


def list_project_automation_executions(
    *,
    session: Session,
    automation_id: str,
    limit: int = 20,
) -> tuple[ProjectAutomationExecution, ...]:
    safe_limit = max(1, min(200, int(limit)))
    return tuple(
        session.execute(
            select(ProjectAutomationExecution)
            .where(ProjectAutomationExecution.automation_id == automation_id)
            .order_by(ProjectAutomationExecution.scheduled_for.desc())
            .limit(safe_limit)
        ).scalars()
    )
