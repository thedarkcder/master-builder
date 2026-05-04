from __future__ import annotations

from datetime import timedelta

from orchestrator.api.schemas import AlertEvaluationRead, AlertRead
from orchestrator.core.observability.alerting import AlertCandidate, alert_dedup_registry, utcnow
from orchestrator.core.platform.operational_health_service import list_enabled_tenant_operational_health
from orchestrator.storage.models import Run


def _tenant_candidates(*, snapshot) -> list[AlertCandidate]:  # noqa: ANN001
    candidates: list[AlertCandidate] = []
    if not snapshot.integrations.jira_connected:
        candidates.append(
            AlertCandidate(
                alert_key=f"tenant:{snapshot.tenant_id}:jira_disconnected",
                severity="HIGH",
                scope_type="tenant",
                scope_id=snapshot.tenant_id,
                reason="Atlassian integration is not connected.",
            )
        )
    if not snapshot.integrations.github_connected:
        candidates.append(
            AlertCandidate(
                alert_key=f"tenant:{snapshot.tenant_id}:github_disconnected",
                severity="HIGH",
                scope_type="tenant",
                scope_id=snapshot.tenant_id,
                reason="GitHub App installation is not connected.",
            )
        )
    if snapshot.integrations.jira_webhook_last_error:
        candidates.append(
            AlertCandidate(
                alert_key=f"tenant:{snapshot.tenant_id}:jira_webhook_error",
                severity="HIGH",
                scope_type="tenant",
                scope_id=snapshot.tenant_id,
                reason="Jira webhook has a recorded provisioning/runtime error.",
            )
        )
    if snapshot.integrations.jira_connected and not snapshot.integrations.jira_webhook_last_error and not snapshot.integrations.jira_webhook_healthy:
        candidates.append(
            AlertCandidate(
                alert_key=f"tenant:{snapshot.tenant_id}:jira_webhook_stale",
                severity="MEDIUM",
                scope_type="tenant",
                scope_id=snapshot.tenant_id,
                reason="Jira webhook has not been received in the last 24 hours.",
            )
        )
    if snapshot.total_runs >= 4 and snapshot.run_failure_rate_ratio >= 0.5:
        candidates.append(
            AlertCandidate(
                alert_key=f"tenant:{snapshot.tenant_id}:run_failure_rate_high",
                severity="MEDIUM",
                scope_type="tenant",
                scope_id=snapshot.tenant_id,
                reason="Tenant run failure rate exceeded threshold (>= 50% over latest sample).",
            )
        )
    return candidates


def _platform_candidates(*, session, now) -> list[AlertCandidate]:  # noqa: ANN001
    window = session.query(Run).filter(Run.created_at >= (now - timedelta(hours=1))).all()
    candidates: list[AlertCandidate] = []
    if window:
        failed = sum(1 for run in window if run.status == "failed")
        failure_rate = failed / len(window)
        if len(window) >= 6 and failure_rate >= 0.5:
            candidates.append(
                AlertCandidate(
                    alert_key="platform:error_rate_spike",
                    severity="CRITICAL",
                    scope_type="platform",
                    scope_id=None,
                    reason="Platform run failure rate spiked in the last hour.",
                )
            )
    return candidates


def evaluate_alerts(
    *,
    session,
    tenant_id: str | None = None,
    cooldown_seconds: int = 600,
    now_fn=utcnow,
) -> AlertEvaluationRead:  # noqa: ANN001
    now = now_fn()
    snapshots = list_enabled_tenant_operational_health(session=session, tenant_id=tenant_id)
    candidates = _platform_candidates(session=session, now=now)
    for snapshot in snapshots:
        candidates.extend(_tenant_candidates(snapshot=snapshot))
    emitted = alert_dedup_registry.filter_candidates(
        candidates=candidates,
        now=now,
        cooldown_seconds=cooldown_seconds,
        prune_missing_keys=tenant_id is None,
    )
    alerts = [
        AlertRead(
            alert_key=candidate.alert_key,
            severity=candidate.severity,
            scope_type=candidate.scope_type,
            scope_id=candidate.scope_id,
            reason=candidate.reason,
            emitted_at=now,
        )
        for candidate in emitted
    ]
    return AlertEvaluationRead(
        evaluated_at=now,
        cooldown_seconds=max(1, cooldown_seconds),
        alerts=alerts,
    )
