from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from orchestrator.core.observability.otel import current_log_context
from orchestrator.core.observability.events import ProductEvent, record_product_event
from orchestrator.core.observability.otel_telemetry import current_trace_context
from orchestrator.core.workflow.attempt_ref import WorkflowAttemptRef
from orchestrator.storage.models import WorkflowExecution, WorkflowOperation


def record_audit_event(
    session: Session,
    *,
    tenant_id: str,
    project_id: str | None,
    workflow_id: str | None,
    run_id: str | None,
    operation_id: str | None,
    attempt_id: str | None,
    issue_key: str | None,
    actor_type: str | None,
    actor_id: str | None,
    source_component: str,
    event_kind: str,
    level: str,
    message: str,
    correlation_id: str | None = None,
    trace_id: str | None = None,
    span_id: str | None = None,
    payload: dict | None = None,
    recorded_at: datetime | None = None,
) -> ProductEvent:
    log_context = current_log_context()
    trace_context = current_trace_context()
    audit_payload = dict(payload or {})
    audit_payload.update(
        {
            "actor_type": str(actor_type or "").strip() or None,
            "actor_id": str(actor_id or "").strip() or None,
            "correlation_id": str(correlation_id or log_context["correlation_id"] or "").strip() or None,
            "trace_id": str(trace_id or trace_context["trace_id"] or "").strip() or None,
            "span_id": str(span_id or trace_context["span_id"] or "").strip() or None,
        }
    )
    return record_product_event(
        session,
        event_class="audit_evidence",
        tenant_id=tenant_id,
        project_id=project_id,
        workflow_id=workflow_id,
        run_id=run_id,
        operation_id=operation_id,
        attempt_id=attempt_id,
        issue_key=issue_key,
        event_kind=event_kind,
        level=level,
        source_component=source_component,
        message=message,
        payload=audit_payload,
        recorded_at=recorded_at,
    )


def record_workflow_operation_audit_event(
    session: Session,
    *,
    operation: WorkflowOperation,
    source_component: str,
    event_kind: str,
    level: str,
    message: str,
    attempt_ref: WorkflowAttemptRef,
    payload: dict | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
) -> ProductEvent:
    workflow = session.get(WorkflowExecution, operation.workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow {operation.workflow_id} is missing for operation audit event.")
    attempt_ref.assert_matches_operation(operation.operation_id)
    return record_audit_event(
        session,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        workflow_id=workflow.workflow_id,
        run_id=operation.run_id,
        operation_id=operation.operation_id,
        attempt_id=attempt_ref.require_attempt_id(),
        issue_key=workflow.source_ref,
        actor_type=actor_type,
        actor_id=actor_id,
        source_component=source_component,
        event_kind=event_kind,
        level=level,
        message=message,
        payload=payload,
    )


def prune_audit_events(*, session: Session, now: datetime | None = None) -> int:
    del session, now
    raise RuntimeError("Audit evidence retention is managed by ClickHouse/object storage policy")
