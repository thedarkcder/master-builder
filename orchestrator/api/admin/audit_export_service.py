from __future__ import annotations

import json
from collections.abc import Iterator

from sqlalchemy.orm import Session

from orchestrator.api.admin.schema_mappers import workflow_observability_event_to_schema
from orchestrator.api.schemas import AuditEventExportRequest
from orchestrator.core.observability.events import list_product_events


def iter_audit_event_export(
    *,
    session: Session,
    request: AuditEventExportRequest,
) -> Iterator[str]:
    del session
    filters = {
        "tenant_id": request.tenant_id,
        "project_id": request.project_id,
        "workflow_id": request.execution_id,
        "operation_id": request.operation_id,
        "run_id": request.run_id,
        "issue_key": request.issue_key,
    }
    for event in list_product_events(
        event_class="audit_evidence",
        filters=filters,
        limit=2000,
        newest_first=False,
    ):
        if request.recorded_after is not None and event.recorded_at < request.recorded_after:
            continue
        if request.recorded_before is not None and event.recorded_at > request.recorded_before:
            continue
        payload = workflow_observability_event_to_schema(event).model_dump(mode="json")
        yield json.dumps(payload, sort_keys=True) + "\n"
