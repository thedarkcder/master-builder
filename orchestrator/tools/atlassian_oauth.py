from __future__ import annotations

from urllib.request import urlopen

from orchestrator.tools.atlassian_oauth_attachment_service import AtlassianOAuthAttachmentService
from orchestrator.tools.atlassian_oauth_callback_flow import AtlassianOAuthCallbackFlow
from orchestrator.tools.atlassian_oauth_confluence_service import AtlassianOAuthConfluenceService
from orchestrator.tools.atlassian_oauth_http import AtlassianOAuthHttpClient
from orchestrator.tools.atlassian_oauth_issue_service import JiraOAuthIssueService, _to_adf_description
from orchestrator.tools.atlassian_oauth_models import (
    ConfluencePage,
    ConfluenceSpace,
    JiraIssueAttachment,
    JiraIssueBulkCreateResult,
    JiraIssueComment,
    JiraIssueCreateInput,
    JiraIssueCreateResult,
    JiraIssueDetail,
    JiraIssuePreview,
    JiraIssueSearchPage,
    AtlassianOAuthClientConfig,
    AtlassianOAuthError,
    AtlassianOAuthResource,
    AtlassianOAuthTokenSet,
    JiraProject,
)
from orchestrator.tools.atlassian_oauth_webhook_manager import AtlassianOAuthWebhookManager

__all__ = [
    "AtlassianOAuthClient",
    "AtlassianOAuthClientConfig",
    "AtlassianOAuthError",
    "AtlassianOAuthResource",
    "AtlassianOAuthTokenSet",
    "JiraProject",
    "ConfluenceSpace",
    "ConfluencePage",
    "JiraIssuePreview",
    "JiraIssueSearchPage",
    "JiraIssueDetail",
    "JiraIssueComment",
    "JiraIssueAttachment",
    "JiraIssueCreateInput",
    "JiraIssueCreateResult",
    "JiraIssueBulkCreateResult",
    "_to_adf_description",
]


class AtlassianOAuthClient:
    """Atlassian and Jira API client operations."""

    def __init__(self, config: AtlassianOAuthClientConfig):
        self._http = AtlassianOAuthHttpClient(opener=lambda request, timeout=30: urlopen(request, timeout=timeout))
        self._callback_flow = AtlassianOAuthCallbackFlow(
            config=config,
            post_json=lambda url, payload: self._post_json(url, payload),
            get_json=lambda url, access_token: self._get_json(url, access_token=access_token),
        )
        self._issue_service = JiraOAuthIssueService(
            get_json=lambda *, url, access_token: self._get_json(url, access_token=access_token),
            request_json=lambda *, method, url, access_token, payload=None: self._request_json(
                method=method,
                url=url,
                access_token=access_token,
                payload=payload,
            ),
        )
        self._webhook_manager = AtlassianOAuthWebhookManager(
            request_json=lambda method, url, access_token, payload: self._request_json(
                method=method,
                url=url,
                access_token=access_token,
                payload=payload,
            )
        )
        self._attachment_service = AtlassianOAuthAttachmentService(
            post_multipart=lambda *, url, access_token, filename, content, content_type="application/octet-stream": self._post_multipart(
                url=url,
                access_token=access_token,
                filename=filename,
                content=content,
                content_type=content_type,
            ),
            get_bytes=lambda *, url, access_token: self._get_bytes(url=url, access_token=access_token),
        )
        self._confluence_service = AtlassianOAuthConfluenceService(
            get_json=lambda *, url, access_token: self._get_json(url, access_token=access_token),
            request_json=lambda *, method, url, access_token, payload=None: self._request_json(
                method=method,
                url=url,
                access_token=access_token,
                payload=payload,
            ),
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

    def _get_bytes(self, *, url: str, access_token: str) -> bytes:
        return self._http.get_bytes(url=url, access_token=access_token)

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

    def exchange_code(self, *, code: str) -> AtlassianOAuthTokenSet:
        return self._callback_flow.exchange_code(code=code)

    def refresh_tokens(self, *, refresh_token: str) -> AtlassianOAuthTokenSet:
        return self._callback_flow.refresh_tokens(refresh_token=refresh_token)

    def list_accessible_resources(self, *, access_token: str) -> list[AtlassianOAuthResource]:
        return self._callback_flow.list_accessible_resources(access_token=access_token)

    def list_projects(self, *, access_token: str, cloud_id: str) -> list[JiraProject]:
        return self._issue_service.list_projects(access_token=access_token, cloud_id=cloud_id)

    def get_confluence_space_by_key(
        self,
        *,
        access_token: str,
        cloud_id: str,
        space_key: str,
    ) -> ConfluenceSpace:
        return self._confluence_service.get_space_by_key(
            access_token=access_token,
            cloud_id=cloud_id,
            space_key=space_key,
        )

    def list_confluence_spaces(
        self,
        *,
        access_token: str,
        cloud_id: str,
        limit: int = 250,
    ) -> list[ConfluenceSpace]:
        return self._confluence_service.list_spaces(
            access_token=access_token,
            cloud_id=cloud_id,
            limit=limit,
        )

    def list_confluence_pages(
        self,
        *,
        access_token: str,
        cloud_id: str,
        site_url: str,
        space_id: str,
        limit: int = 250,
    ) -> list[ConfluencePage]:
        return self._confluence_service.list_pages(
            access_token=access_token,
            cloud_id=cloud_id,
            site_url=site_url,
            space_id=space_id,
            limit=limit,
        )

    def get_confluence_page(
        self,
        *,
        access_token: str,
        cloud_id: str,
        site_url: str,
        page_id: str,
    ) -> ConfluencePage:
        return self._confluence_service.get_page(
            access_token=access_token,
            cloud_id=cloud_id,
            site_url=site_url,
            page_id=page_id,
        )

    def create_confluence_page(
        self,
        *,
        access_token: str,
        cloud_id: str,
        site_url: str,
        space_id: str,
        title: str,
        body_storage_value: str,
        parent_page_id: str | None = None,
    ) -> ConfluencePage:
        return self._confluence_service.create_page(
            access_token=access_token,
            cloud_id=cloud_id,
            site_url=site_url,
            space_id=space_id,
            title=title,
            body_storage_value=body_storage_value,
            parent_page_id=parent_page_id,
        )

    def update_confluence_page_title(
        self,
        *,
        access_token: str,
        cloud_id: str,
        site_url: str,
        page_id: str,
        title: str,
    ) -> ConfluencePage:
        return self._confluence_service.update_page_title(
            access_token=access_token,
            cloud_id=cloud_id,
            site_url=site_url,
            page_id=page_id,
            title=title,
        )

    def search_issues_by_jql(
        self,
        *,
        access_token: str,
        cloud_id: str,
        jql: str,
        max_results: int = 20,
        start_at: int = 0,
    ) -> list[JiraIssuePreview]:
        return self._issue_service.search_issues_by_jql(
            access_token=access_token,
            cloud_id=cloud_id,
            jql=jql,
            max_results=max_results,
            start_at=start_at,
        )

    def search_issues_by_jql_page(
        self,
        *,
        access_token: str,
        cloud_id: str,
        jql: str,
        max_results: int = 20,
        next_page_token: str | None = None,
    ) -> JiraIssueSearchPage:
        return self._issue_service.search_issues_by_jql_page(
            access_token=access_token,
            cloud_id=cloud_id,
            jql=jql,
            max_results=max_results,
            next_page_token=next_page_token,
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

    def list_issue_comments(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
    ) -> list[JiraIssueComment]:
        return self._issue_service.list_issue_comments(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
        )

    def list_issue_attachments(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
    ) -> list[JiraIssueAttachment]:
        return self._issue_service.list_issue_attachments(
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

    def create_issue(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
        issue: JiraIssueCreateInput,
    ) -> JiraIssueCreateResult:
        return self._issue_service.create_issue(
            access_token=access_token,
            cloud_id=cloud_id,
            project_key=project_key,
            issue=issue,
        )

    def list_project_issue_types_for_create(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
    ) -> list[str]:
        return self._issue_service._list_project_issue_types_for_create(
            access_token=access_token,
            cloud_id=cloud_id,
            project_key=project_key,
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

    def add_issue_link(
        self,
        *,
        access_token: str,
        cloud_id: str,
        inward_issue_key: str,
        outward_issue_key: str,
        link_type: str = "Relates",
    ) -> dict:
        return self._issue_service.add_issue_link(
            access_token=access_token,
            cloud_id=cloud_id,
            inward_issue_key=inward_issue_key,
            outward_issue_key=outward_issue_key,
            link_type=link_type,
        )

    def upsert_remote_issue_link(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        global_id: str,
        relationship: str,
        title: str,
        url: str,
    ) -> dict:
        return self._issue_service.upsert_remote_issue_link(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
            global_id=global_id,
            relationship=relationship,
            title=title,
            url=url,
        )

    def transition_issue(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        target_status: str,
    ) -> dict:
        return self._issue_service.transition_issue(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
            target_status=target_status,
        )

    def add_issue_labels(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        labels: list[str],
    ) -> None:
        self._issue_service.add_issue_labels(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
            labels=labels,
        )

    def replace_issue_labels(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        labels: list[str],
    ) -> None:
        self._issue_service.replace_issue_labels(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
            labels=labels,
        )

    def update_issue_summary(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
    ) -> None:
        self._issue_service.update_issue_summary(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
            summary=summary,
        )

    def update_issue_summary_and_description(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
        description: str | dict,
    ) -> None:
        self._issue_service.update_issue_summary_and_description(
            access_token=access_token,
            cloud_id=cloud_id,
            issue_id_or_key=issue_id_or_key,
            summary=summary,
            description=description,
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

    def download_attachment(
        self,
        *,
        access_token: str,
        content_url: str,
    ) -> bytes:
        return self._attachment_service.download_attachment(
            access_token=access_token,
            content_url=content_url,
        )
