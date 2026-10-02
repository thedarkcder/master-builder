from __future__ import annotations

from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select

from orchestrator.api.admin.token_diagnostics_service import get_token_stage_diagnostics
from orchestrator.api.schemas import (
    TokenProjectStageDiagnosticsRead,
    TokenStageDiagnosticsCompareRead,
)
from orchestrator.storage.models import Project


def _normalize_project_ids(project_ids: str) -> list[str]:
    values = [
        value.strip() for value in str(project_ids or "").split(",") if value.strip()
    ]
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        deduped.append(value)
        seen.add(value)
    return deduped


def get_token_stage_diagnostics_compare(
    *,
    session,
    tenant_id: str,
    project_ids: str,
    issue_key: str | None = None,
    run_status: str | None = None,
    stage: str | None = None,
    attempt: int | None = None,
    model: str | None = None,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    only_retried: bool = False,
    only_with_test_stage: bool = False,
) -> TokenStageDiagnosticsCompareRead:
    normalized_tenant = str(tenant_id).strip()
    if not normalized_tenant:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="tenant_id is required"
        )

    ids = _normalize_project_ids(project_ids)
    if not ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="project_ids is required"
        )

    projects = (
        session.execute(
            select(Project)
            .where(Project.tenant_id == normalized_tenant)
            .where(Project.project_id.in_(ids))
        )
        .scalars()
        .all()
    )
    project_lookup = {str(project.project_id): project for project in projects}

    missing_ids = [project_id for project_id in ids if project_id not in project_lookup]
    if missing_ids:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Projects not found for tenant: {', '.join(missing_ids)}",
        )

    project_summaries: list[TokenProjectStageDiagnosticsRead] = []
    for project_id in ids:
        diagnostics = get_token_stage_diagnostics(
            session=session,
            tenant_id=normalized_tenant,
            project_id=project_id,
            issue_key=issue_key,
            run_status=run_status,
            stage=stage,
            attempt=attempt,
            model=model,
            start_date=start_date,
            end_date=end_date,
            only_retried=only_retried,
            only_with_test_stage=only_with_test_stage,
            page=1,
            page_size=1,
        )
        project = project_lookup[project_id]
        project_summaries.append(
            TokenProjectStageDiagnosticsRead(
                project_id=project_id,
                project_name=str(project.name or "") or None,
                stages=diagnostics.stages,
                retest_waste_score=diagnostics.retest_waste_score,
            )
        )

    return TokenStageDiagnosticsCompareRead(projects=project_summaries)
