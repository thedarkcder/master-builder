from __future__ import annotations

from urllib.request import urlopen

from orchestrator.tools.jira_oauth_attachment_service import JiraOAuthAttachmentService
from orchestrator.tools.jira_oauth_callback_flow import JiraOAuthCallbackFlow
from orchestrator.tools.jira_oauth_http import JiraOAuthHttpClient
from orchestrator.tools.jira_oauth_issue_service import JiraOAuthIssueService, _to_adf_description
from orchestrator.tools.jira_oauth_models import (
    JiraIssueBulkCreateResult,
    JiraIssueCreateInput,
    JiraIssueCreateResult,
    JiraIssueDetail,
    JiraIssuePreview,
    JiraOAuthClientConfig,
    JiraOAuthError,
    JiraOAuthResource,
    JiraOAuthTokenSet,
    JiraProject,
)
from orchestrator.tools.jira_oauth_webhook_manager import JiraOAuthWebhookManager

__all__ = [
    "JiraOAuthClient",
    "JiraOAuthClientConfig",
    "JiraOAuthError",
    "JiraOAuthResource",
    "JiraOAuthTokenSet",
    "JiraProject",
    "JiraIssuePreview",
    "JiraIssueDetail",
    "JiraIssueCreateInput",
    "JiraIssueCreateResult",
    "JiraIssueBulkCreateResult",
    "_to_adf_description",
]


class JiraOAuthClient:
    """Compatibility facade for Jira OAuth and Jira API operations."""

    def __init__(self, config: JiraOAuthClientConfig):
        self._http = JiraOAuthHttpClient(opener=lambda request, timeout=30: urlopen(request, timeout=timeout))
        self._callback_flow = JiraOAuthCallbackFlow(
            config=config,
            post_json=lambda url, payload: self._post_json(url, payload),
            get_json=lambda url, access_token: self._get_json(url, access_token=access_token),
        )
        self._issue_service = JiraOAuthIssueService(
            get_json=lambda **kwargs: self._get_json(kwargs["url"], access_token=kwargs["access_token"]),
            request_json=lambda **kwargs: self._request_json(**kwargs),
        )
        self._webhook_manager = JiraOAuthWebhookManager(
            request_json=lambda method, url, access_token, payload: self._request_json(
                method=method,
                url=url,
                access_token=access_token,
                payload=payload,
            )
        )
        self._attachment_service = JiraOAuthAttachmentService(
            post_multipart=lambda **kwargs: self._post_multipart(**kwargs),
        )

    def _post_json(self, url: str, payload: dict) -> dict:
        return self._http.post_json(url=url, payload=payload)

    def _get_json(self, url: str, *, access_token: str):
        return self._http.get_json(url=url, access_token=access_token)

    def _request_json(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        payload: dict | None = None,
    ):
        return self._http.request_json(
            method=method,
            url=url,
            access_token=access_token,
            payload=payload,
        )

    def _post_multipart(
        self,
        *,
        url: str,
        access_token: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ):
        return self._http.post_multipart(
            url=url,
            access_token=access_token,
            filename=filename,
            content=content,
            content_type=content_type,
        )

    def build_authorize_url(self, *, state: str) -> str:
        return self._callback_flow.build_authorize_url(state=state)

    def exchange_code(self, *, code: str) -> JiraOAuthTokenSet:
        return self._callback_flow.exchange_code(code=code)

    def refresh_tokens(self, *, refresh_token: str) -> JiraOAuthTokenSet:
        return self._callback_flow.refresh_tokens(refresh_token=refresh_token)

    def list_accessible_resources(self, *, access_token: str) -> list[JiraOAuthResource]:
        return self._callback_flow.list_accessible_resources(access_token=access_token)

    def list_projects(self, *, access_token: str, cloud_id: str) -> list[JiraProject]:
        return self._issue_service.list_projects(access_token=access_token, cloud_id=cloud_id)

    def search_issues_by_jql(
        self,
        *,
        access_token: str,
        cloud_id: str,
        jql: str,
        max_results: int = 20,
    ) -> list[JiraIssuePreview]:
        return self._issue_service.search_issues_by_jql(
            access_token=access_token,
            cloud_id=cloud_id,
            jql=jql,
            max_results=max_results,
        )

    def get_issue_detail(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
    ) -> JiraIssueDetail:
        return self._issue_service.get_issue_detail(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
        )

    def create_issues_bulk(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
        issues: list[JiraIssueCreateInput],
    ) -> JiraIssueBulkCreateResult:
        return self._issue_service.create_issues_bulk(
            access_token=access_token,
            cloud_id=cloud_id,
            project_key=project_key,
            issues=issues,
        )

    def update_issue_fields(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
        description: str | dict,
        labels: list[str],
    ) -> None:
        self._issue_service.update_issue_fields(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
            summary=summary,
            description=description,
            labels=labels,
        )

    def add_issue_comment(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        comment: str | dict,
    ) -> dict:
        return self._issue_service.add_issue_comment(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
            comment=comment,
        )

    def register_webhook(
        self,
        *,
        access_token: str,
        cloud_id: str,
        callback_url: str,
        jql_filter: str,
        events: list[str],
    ) -> list[int]:
        return self._webhook_manager.register_webhook(
            access_token=access_token,
            cloud_id=cloud_id,
            callback_url=callback_url,
            jql_filter=jql_filter,
            events=events,
        )

    def list_webhooks(
        self,
        *,
        access_token: str,
        cloud_id: str,
    ) -> list[dict]:
        return self._webhook_manager.list_webhooks(access_token=access_token, cloud_id=cloud_id)

    def delete_webhooks(
        self,
        *,
        access_token: str,
        cloud_id: str,
        webhook_ids: list[int],
    ) -> None:
        self._webhook_manager.delete_webhooks(
            access_token=access_token,
            cloud_id=cloud_id,
            webhook_ids=webhook_ids,
        )

    def upload_issue_attachment(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> list[dict]:
        return self._attachment_service.upload_issue_attachment(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
            filename=filename,
            content=content,
            content_type=content_type,
        )
