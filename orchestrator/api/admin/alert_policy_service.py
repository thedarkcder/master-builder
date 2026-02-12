from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from orchestrator.api.schemas import AlertEvaluationRead, AlertRead
from orchestrator.core.alerting import (
    AlertCandidate,
    alert_dedup_registry,
    utcnow,
)
from orchestrator.storage.models import Run, Tenant


def _coerce_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=utcnow().tzinfo)
    return value


def _tenant_candidates(*, tenant: Tenant, runs: list[Run], now: datetime) -> list[AlertCandidate]:
    candidates: list[AlertCandidate] = []
    jira_config = dict(tenant.jira_config or {})
    github_config = dict(tenant.github_config or {})

    if not str(jira_config.get("connection_id") or "").strip():
        candidates.append(
            AlertCandidate(
                alert_key=f"tenant:{tenant.tenant_id}:jira_disconnected",
                severity="HIGH",
                scope_type="tenant",
                scope_id=tenant.tenant_id,
                reason="Jira integration is not connected.",
            )
        )
    if not str(github_config.get("installation_id") or "").strip():
        candidates.append(
            AlertCandidate(
                alert_key=f"tenant:{tenant.tenant_id}:github_disconnected",
                severity="HIGH",
                scope_type="tenant",
                scope_id=tenant.tenant_id,
                reason="GitHub App installation is not connected.",
            )
        )

    webhook_error = str(jira_config.get("webhook_last_error") or "").strip()
    if webhook_error:
        candidates.append(
            AlertCandidate(
                alert_key=f"tenant:{tenant.tenant_id}:jira_webhook_error",
                severity="HIGH",
                scope_type="tenant",
                scope_id=tenant.tenant_id,
                reason="Jira webhook has a recorded provisioning/runtime error.",
            )
        )

    received_at_raw = str(jira_config.get("webhook_last_received_at") or "").strip()
    if str(jira_config.get("connection_id") or "").strip() and not webhook_error:
        stale = True
        if received_at_raw:
            try:
                parsed = datetime.fromisoformat(received_at_raw.replace("Z", "+00:00"))
                stale = _coerce_aware(parsed) < (now - timedelta(hours=24))
            except ValueError:
                stale = True
        if stale:
            candidates.append(
                AlertCandidate(
                    alert_key=f"tenant:{tenant.tenant_id}:jira_webhook_stale",
                    severity="MEDIUM",
                    scope_type="tenant",
                    scope_id=tenant.tenant_id,
                    reason="Jira webhook has not been received in the last 24 hours.",
                )
            )

    if runs:
        failed = sum(1 for run in runs if run.status == "failed")
        failure_rate = failed / len(runs)
        if len(runs) >= 4 and failure_rate >= 0.5:
            candidates.append(
                AlertCandidate(
                    alert_key=f"tenant:{tenant.tenant_id}:run_failure_rate_high",
                    severity="MEDIUM",
                    scope_type="tenant",
                    scope_id=tenant.tenant_id,
                    reason="Tenant run failure rate exceeded threshold (>= 50% over latest sample).",
                )
            )

    return candidates


def _platform_candidates(*, runs: list[Run], now: datetime) -> list[AlertCandidate]:
    candidates: list[AlertCandidate] = []
    window = [run for run in runs if _coerce_aware(run.created_at) >= (now - timedelta(hours=1))]
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
    tenants_query = select(Tenant).where(Tenant.is_enabled.is_(True))
    if tenant_id:
        tenants_query = tenants_query.where(Tenant.tenant_id == tenant_id)
    tenants = session.execute(tenants_query).scalars().all()

    runs = session.execute(select(Run)).scalars().all()
    tenant_runs: dict[str, list[Run]] = {}
    for run in runs:
        tenant_runs.setdefault(run.tenant_id, []).append(run)

    candidates = _platform_candidates(runs=runs, now=now)
    for tenant in tenants:
        candidates.extend(_tenant_candidates(tenant=tenant, runs=tenant_runs.get(tenant.tenant_id, []), now=now))

    emitted = alert_dedup_registry.filter_candidates(
        candidates=candidates,
        now=now,
        cooldown_seconds=cooldown_seconds,
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

