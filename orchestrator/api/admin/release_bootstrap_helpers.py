from __future__ import annotations

from datetime import datetime, timezone

from orchestrator.api.schemas import ReleaseBootstrapReportRead
from orchestrator.storage.models import JiraOAuthConnection, Tenant
from orchestrator.tools.jira_oauth import JiraOAuthError


def release_bootstrap_report_from_config(*, tenant_id: str, jira_config: dict) -> ReleaseBootstrapReportRead | None:
    raw_report = jira_config.get("release_bootstrap")
    if not isinstance(raw_report, dict):
        return None
    raw_checked_at = raw_report.get("checked_at")
    checked_at = str(raw_checked_at).strip() if isinstance(raw_checked_at, str) and str(raw_checked_at).strip() else ""
    if not checked_at:
        return None
    raw_checks = raw_report.get("checks")
    checks = (
        {str(key): bool(value) for key, value in raw_checks.items()}
        if isinstance(raw_checks, dict)
        else {}
    )
    raw_details = raw_report.get("details")
    details = [str(item) for item in raw_details] if isinstance(raw_details, list) else []
    return ReleaseBootstrapReportRead(
        tenant_id=tenant_id,
        ok=bool(raw_report.get("ok")),
        checks=checks,
        details=details,
        checked_at=checked_at,
    )


def compute_release_bootstrap_result(
    *,
    session,
    tenant: Tenant,
    tenant_id: str,
    settings,
    required_statuses: tuple[str, ...],
    refresh_jira_connection_tokens_fn,
    jira_oauth_client_fn,
) -> tuple[bool, dict[str, bool], list[str], dict]:  # noqa: ANN001
    checks: dict[str, bool] = {
        "jira_connection": False,
        "jira_project_keys": False,
        "jira_required_statuses": False,
        "github_installation": False,
    }
    details: list[str] = []

    jira_config = dict(tenant.jira_config or {})
    project_keys = jira_config.get("project_keys")
    if isinstance(project_keys, list):
        normalized_project_keys = [str(item).strip() for item in project_keys if str(item).strip()]
    else:
        normalized_project_keys = []
    checks["jira_project_keys"] = bool(normalized_project_keys)
    if not normalized_project_keys:
        details.append("Missing Jira project keys.")

    connection_id = jira_config.get("connection_id")
    if not isinstance(connection_id, str) or not connection_id:
        details.append("Jira OAuth connection is not linked.")
        connection = None
    else:
        connection = session.get(JiraOAuthConnection, connection_id)
        if connection is None:
            details.append("Configured Jira OAuth connection was not found.")
    checks["jira_connection"] = connection is not None

    if connection is not None and normalized_project_keys:
        try:
            access_token = refresh_jira_connection_tokens_fn(
                session,
                connection=connection,
                settings=settings,
                tenant_id=tenant_id,
            )
            client = jira_oauth_client_fn(session=session, settings=settings, tenant_id=tenant_id)
            quoted_projects = ", ".join(f'"{key}"' for key in normalized_project_keys)
            for required_status in required_statuses:
                jql = (
                    f"project in ({quoted_projects}) AND status = \"{required_status}\" "
                    "ORDER BY updated DESC"
                )
                client.search_issues_by_jql(
                    access_token=access_token,
                    cloud_id=connection.cloud_id,
                    jql=jql,
                    max_results=1,
                )
            checks["jira_required_statuses"] = True
        except (ValueError, JiraOAuthError) as exc:
            details.append(
                "Jira required status validation failed "
                f"for {', '.join(required_statuses)}: {exc}"
            )
            checks["jira_required_statuses"] = False

    github_installation_id = str(tenant.github_config.get("installation_id") or "").strip()
    checks["github_installation"] = bool(github_installation_id)
    if not github_installation_id:
        details.append("GitHub App installation is not connected.")

    ok = all(checks.values())
    if ok:
        details.append("Release bootstrap checks passed.")

    checked_at = datetime.now(timezone.utc).isoformat()
    report_payload = {
        "ok": ok,
        "checks": checks,
        "details": details,
        "checked_at": checked_at,
    }
    return ok, checks, details, report_payload
