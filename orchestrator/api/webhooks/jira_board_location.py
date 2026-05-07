from __future__ import annotations

from urllib.parse import quote_plus

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.atlassian_oauth.connection_service import tenant_atlassian_oauth_context
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext
from orchestrator.tools.atlassian_oauth import AtlassianOAuthError
from orchestrator.tools.atlassian_oauth_http import AtlassianOAuthHttpClient


def _issues_payload_contains_issue(*, payload: object, issue_key: str) -> bool:
    if not isinstance(payload, dict):
        return False
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return False
    normalized_issue_key = issue_key.strip().upper()
    for issue in issues:
        if not isinstance(issue, dict):
            continue
        candidate_key = str(issue.get("key") or "").strip().upper()
        if candidate_key == normalized_issue_key:
            return True
    return False


def _fetch_issue_board_location(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
    board_id: int,
) -> tuple[str, str | None]:
    try:
        oauth_context = tenant_atlassian_oauth_context(
            session=session,
            tenant=context.tenant,
            settings=settings,
        )
    except HTTPException as exc:
        return "error", str(exc.detail)
    issue_jql = quote_plus(f'key = "{context.issue_key}"')
    base_url = f"https://api.atlassian.com/ex/jira/{oauth_context.connection.cloud_id}/rest/agile/1.0"
    backlog_url = f"{base_url}/board/{board_id}/backlog?jql={issue_jql}&maxResults=1"
    board_url = f"{base_url}/board/{board_id}/issue?jql={issue_jql}&maxResults=1"
    http_client = AtlassianOAuthHttpClient()
    backlog_error_detail: str | None = None
    try:
        backlog_payload = http_client.get_json(
            url=backlog_url,
            access_token=oauth_context.access_token,
        )
        if _issues_payload_contains_issue(payload=backlog_payload, issue_key=context.issue_key):
            return "backlog", None
    except (AtlassianOAuthError, ValueError) as exc:
        backlog_error_detail = f"backlog lookup failed: {exc}"
    try:
        board_payload = http_client.get_json(
            url=board_url,
            access_token=oauth_context.access_token,
        )
        if _issues_payload_contains_issue(payload=board_payload, issue_key=context.issue_key):
            return "board", None
    except (AtlassianOAuthError, ValueError) as exc:
        board_error_detail = f"board lookup failed: {exc}"
        if backlog_error_detail:
            return "error", f"{backlog_error_detail}; {board_error_detail}"
        return "error", board_error_detail
    if backlog_error_detail:
        return "not_on_board", backlog_error_detail
    return "not_on_board", None


def resolve_project_issue_board_location(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> tuple[str | None, str | None]:
    raw_board_id = None
    if context.project is not None:
        raw_board_id = (context.project.policy_overrides or {}).get("run_board_id")
    if raw_board_id is None:
        return None, None
    try:
        board_id = int(raw_board_id)
    except (TypeError, ValueError):
        board_id = 0
    if board_id <= 0:
        return None, None
    return _fetch_issue_board_location(
        context=context,
        session=session,
        settings=settings,
        board_id=board_id,
    )
