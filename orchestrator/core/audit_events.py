from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from orchestrator.core.guardrails import redact_sensitive_text
from orchestrator.core.observability import current_log_context
from orchestrator.core.observability_policy import normalize_tenant_observability_policy
from orchestrator.core.telemetry import current_trace_context
from orchestrator.storage.models import AuditEvent, Tenant, WorkflowExecution, WorkflowOperation


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_payload(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, object] = {}
    for key, value in payload.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if isinstance(value, str):
            normalized[normalized_key] = redact_sensitive_text(value)
        elif isinstance(value, dict):
            normalized[normalized_key] = _normalized_payload(value)
        elif isinstance(value, list):
            normalized[normalized_key] = [
                redact_sensitive_text(item) if isinstance(item, str) else item
                for item in value
            ]
        else:
            normalized[normalized_key] = value
    return normalized


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
) -> AuditEvent:
    log_context = current_log_context()
    trace_context = current_trace_context()
    event = AuditEvent(
        event_id=uuid4().hex,
        tenant_id=str(tenant_id or "").strip(),
        project_id=str(project_id or "").strip() or None,
        workflow_id=str(workflow_id or "").strip() or None,
        run_id=str(run_id or "").strip() or None,
        operation_id=str(operation_id or "").strip() or None,
        attempt_id=str(attempt_id or "").strip() or None,
        issue_key=str(issue_key or "").strip() or None,
        actor_type=str(actor_type or "").strip() or None,
        actor_id=str(actor_id or "").strip() or None,
        source_component=str(source_component or "").strip(),
        event_kind=str(event_kind or "").strip(),
        level=str(level or "").strip().lower() or "info",
        correlation_id=str(correlation_id or log_context["correlation_id"] or "").strip() or None,
        trace_id=str(trace_id or trace_context["trace_id"] or "").strip() or None,
        span_id=str(span_id or trace_context["span_id"] or "").strip() or None,
        message=redact_sensitive_text(str(message or "").strip()),
        payload_json=_normalized_payload(payload),
        recorded_at=recorded_at or _utcnow(),
    )
    session.add(event)
    session.flush()
    return event


def record_workflow_operation_audit_event(
    session: Session,
    *,
    operation: WorkflowOperation,
    source_component: str,
    event_kind: str,
    level: str,
    message: str,
    attempt_id: str | None = None,
    payload: dict | None = None,
    actor_type: str | None = None,
    actor_id: str | None = None,
) -> AuditEvent:
    workflow = session.get(WorkflowExecution, operation.workflow_id)
    if workflow is None:
        raise ValueError(f"Workflow {operation.workflow_id} is missing for operation audit event.")
    return record_audit_event(
        session,
        tenant_id=workflow.tenant_id,
        project_id=workflow.project_id,
        workflow_id=workflow.workflow_id,
        run_id=operation.run_id,
        operation_id=operation.operation_id,
        attempt_id=attempt_id,
        issue_key=workflow.source_ref,
        actor_type=actor_type,
        actor_id=actor_id,
        source_component=source_component,
        event_kind=event_kind,
        level=level,
        message=message,
        payload=payload,
    )


def prune_audit_events(
    *,
    session: Session,
    now: datetime | None = None,
) -> int:
    timestamp = now or _utcnow()
    deleted = 0
    tenant_rows = session.execute(select(Tenant.tenant_id, Tenant.policy_config)).all()
    for tenant_id, raw_policy in tenant_rows:
        policy = normalize_tenant_observability_policy(raw_policy if isinstance(raw_policy, dict) else None)
        if policy.legal_hold_enabled:
            continue
        cutoff = timestamp - timedelta(days=policy.audit_retention_days)
        result = session.execute(
            delete(AuditEvent).where(
                AuditEvent.tenant_id == str(tenant_id or "").strip(),
                AuditEvent.recorded_at < cutoff,
            )
        )
        deleted += int(result.rowcount or 0)
    return deleted
