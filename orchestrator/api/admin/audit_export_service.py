from __future__ import annotations

import json
from collections.abc import Iterator

from sqlalchemy import Select, and_, select
from sqlalchemy.orm import Session

from orchestrator.api.admin.schema_mappers import workflow_observability_event_to_schema
from orchestrator.api.schemas import AuditEventExportRequest
from orchestrator.storage.models import AuditEvent


def _audit_event_query(*, request: AuditEventExportRequest) -> Select[tuple[AuditEvent]]:
    filters = [AuditEvent.tenant_id == request.tenant_id]
    if request.project_id:
        filters.append(AuditEvent.project_id == request.project_id)
    if request.execution_id:
        filters.append(AuditEvent.workflow_id == request.execution_id)
    if request.operation_id:
        filters.append(AuditEvent.operation_id == request.operation_id)
    if request.run_id:
        filters.append(AuditEvent.run_id == request.run_id)
    if request.issue_key:
        filters.append(AuditEvent.issue_key == request.issue_key)
    if request.recorded_after is not None:
        filters.append(AuditEvent.recorded_at >= request.recorded_after)
    if request.recorded_before is not None:
        filters.append(AuditEvent.recorded_at <= request.recorded_before)
    return (
        select(AuditEvent)
        .where(and_(*filters))
        .order_by(AuditEvent.recorded_at.asc(), AuditEvent.event_id.asc())
    )


def iter_audit_event_export(
    *,
    session: Session,
    request: AuditEventExportRequest,
) -> Iterator[str]:
    for event in session.execute(_audit_event_query(request=request)).scalars():
        payload = workflow_observability_event_to_schema(event).model_dump(mode="json")
        yield json.dumps(payload, sort_keys=True) + "\n"
