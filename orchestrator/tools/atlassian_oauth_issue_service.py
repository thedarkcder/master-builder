from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlencode

from orchestrator.tools.atlassian_oauth_models import (
    JiraIssueAttachment,
    JiraIssueBulkCreateResult,
    JiraIssueComment,
    JiraIssueCreateInput,
    JiraIssueCreateResult,
    JiraIssueDetail,
    JiraIssuePreview,
    JiraIssueSearchPage,
    AtlassianOAuthError,
    JiraProject,
)

MAX_JIRA_ADF_DOCUMENT_BYTES = 28_000
_JIRA_ADF_TRUNCATION_NOTICE = "Content truncated to fit Jira content size limit."


class JiraOAuthIssueService:
    def __init__(
        self,
        *,
        get_json,
        request_json,
    ) -> None:
        self._get_json = get_json
        self._request_json = request_json

    def list_projects(self, *, access_token: str, cloud_id: str) -> list[JiraProject]:
        payload = self._get_json(
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/project/search?maxResults=100",
            access_token=access_token,
        )
        values = payload.get("values") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            raise AtlassianOAuthError("Project search response missing values list")

        projects: list[JiraProject] = []
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise AtlassianOAuthError(f"Project search response item {index} was not an object")
            key = item.get("key")
            name = item.get("name")
            if not isinstance(key, str) or not key:
                raise AtlassianOAuthError(f"Project search response item {index} missing project key")
            if not isinstance(name, str) or not name:
                raise AtlassianOAuthError(f"Project search response item {index} missing project name")
            projects.append(JiraProject(key=key, name=name))
        projects.sort(key=lambda project: project.key)
        return projects

    def search_issues_by_jql(
        self,
        *,
        access_token: str,
        cloud_id: str,
        jql: str,
        max_results: int = 20,
        start_at: int = 0,
    ) -> list[JiraIssuePreview]:
        if int(start_at) != 0:
            raise AtlassianOAuthError("Jira enhanced search uses nextPageToken pagination; start_at offsets are unsupported")
        return self.search_issues_by_jql_page(
            access_token=access_token,
            cloud_id=cloud_id,
            jql=jql,
            max_results=max_results,
            next_page_token=None,
        ).issues

    def search_issues_by_jql_page(
        self,
        *,
        access_token: str,
        cloud_id: str,
        jql: str,
        max_results: int = 20,
        next_page_token: str | None = None,
    ) -> JiraIssueSearchPage:
        bounded_max_results = max(1, min(max_results, 50))
        request_payload: dict[str, object] = {
            "jql": jql,
            "maxResults": bounded_max_results,
            "fields": ["summary", "status"],
        }
        normalized_next_page_token = str(next_page_token or "").strip()
        if normalized_next_page_token:
            request_payload["nextPageToken"] = normalized_next_page_token
        payload = self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search/jql",
            access_token=access_token,
            payload=request_payload,
        )
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if not isinstance(issues, list):
            raise AtlassianOAuthError("Issue search response missing issues list")

        results: list[JiraIssuePreview] = []
        for index, item in enumerate(issues):
            if not isinstance(item, dict):
                raise AtlassianOAuthError(f"Issue search response item {index} was not an object")
            key = item.get("key")
            fields = item.get("fields")
            if not isinstance(fields, dict):
                raise AtlassianOAuthError(f"Issue search response item {index} missing fields object")
            summary = fields.get("summary")
            status_obj = fields.get("status")
            status_name = status_obj.get("name") if isinstance(status_obj, dict) else None

            if not isinstance(key, str) or not key:
                raise AtlassianOAuthError(f"Issue search response item {index} missing issue key")
            if not isinstance(summary, str) or not summary:
                raise AtlassianOAuthError(f"Issue search response item {index} missing issue summary")
            if not isinstance(status_name, str) or not status_name:
                raise AtlassianOAuthError(f"Issue search response item {index} missing issue status")

            results.append(JiraIssuePreview(key=key, summary=summary, status=status_name))
        raw_next_page_token = payload.get("nextPageToken") if isinstance(payload, dict) else None
        if raw_next_page_token is None:
            parsed_next_page_token = None
        elif isinstance(raw_next_page_token, str):
            parsed_next_page_token = raw_next_page_token.strip() or None
        else:
            raise AtlassianOAuthError("Issue search response nextPageToken was not a string")
        return JiraIssueSearchPage(issues=results, next_page_token=parsed_next_page_token)

    def get_issue_detail(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
    ) -> JiraIssueDetail:
        normalized_issue = issue_id_or_key.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for issue detail fetch")

        query = urlencode({"fields": "summary,status,description,labels,issuetype,parent"})
        payload = self._get_json(
            url=(
                f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/"
                f"{quote(normalized_issue, safe='')}?{query}"
            ),
            access_token=access_token,
        )
        if not isinstance(payload, dict):
            raise AtlassianOAuthError("Issue detail response was not an object")

        key_raw = payload.get("key")
        if not isinstance(key_raw, str) or not key_raw.strip():
            raise AtlassianOAuthError("Issue detail response missing issue key")
        key = key_raw.strip()
        issue_id_raw = payload.get("id")
        issue_id = issue_id_raw.strip() if isinstance(issue_id_raw, str) and issue_id_raw.strip() else None

        fields = payload.get("fields")
        if not isinstance(fields, dict):
            raise AtlassianOAuthError("Issue detail response missing fields object")

        summary_raw = fields.get("summary")
        if not isinstance(summary_raw, str) or not summary_raw.strip():
            raise AtlassianOAuthError("Issue detail response missing issue summary")
        summary = summary_raw.strip()

        status_category_key: str | None = None
        status_obj = fields.get("status")
        if not isinstance(status_obj, dict):
            raise AtlassianOAuthError("Issue detail response missing status object")
        status_raw = status_obj.get("name")
        if not isinstance(status_raw, str) or not status_raw.strip():
            raise AtlassianOAuthError("Issue detail response missing issue status")
        status_name = status_raw.strip()
        status_category = status_obj.get("statusCategory")
        if isinstance(status_category, dict):
            category_raw = status_category.get("key")
            if isinstance(category_raw, str) and category_raw.strip():
                status_category_key = category_raw.strip()

        issue_type: str | None = None
        issue_type_hierarchy_level: int | None = None
        issue_type_is_subtask: bool | None = None
        issue_type_obj = fields.get("issuetype")
        if isinstance(issue_type_obj, dict):
            issue_type_raw = issue_type_obj.get("name")
            if isinstance(issue_type_raw, str) and issue_type_raw.strip():
                issue_type = issue_type_raw.strip()
            hierarchy_level_raw = issue_type_obj.get("hierarchyLevel")
            if isinstance(hierarchy_level_raw, int):
                issue_type_hierarchy_level = hierarchy_level_raw
            subtask_raw = issue_type_obj.get("subtask")
            if isinstance(subtask_raw, bool):
                issue_type_is_subtask = subtask_raw

        description = _adf_to_plain_text(fields.get("description")).strip()
        labels_raw = fields.get("labels")
        labels: list[str] = []
        if isinstance(labels_raw, list):
            labels = [str(label).strip() for label in labels_raw if str(label).strip()]
        parent_key: str | None = None
        parent_issue_id: str | None = None
        parent_obj = fields.get("parent")
        if isinstance(parent_obj, dict):
            raw_parent_key = parent_obj.get("key")
            raw_parent_id = parent_obj.get("id")
            parent_key = raw_parent_key.strip() if isinstance(raw_parent_key, str) and raw_parent_key.strip() else None
            parent_issue_id = raw_parent_id.strip() if isinstance(raw_parent_id, str) and raw_parent_id.strip() else None
        return JiraIssueDetail(
            key=key,
            summary=summary,
            status=status_name,
            status_category_key=status_category_key,
            issue_type=issue_type,
            issue_type_hierarchy_level=issue_type_hierarchy_level,
            issue_type_is_subtask=issue_type_is_subtask,
            description=description,
            labels=labels,
            issue_id=issue_id,
            parent_key=parent_key,
            parent_issue_id=parent_issue_id,
        )

    def list_issue_comments(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
    ) -> list[JiraIssueComment]:
        normalized_issue = issue_id_or_key.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for issue comments fetch")

        comments: list[JiraIssueComment] = []
        start_at = 0
        while True:
            query = urlencode({"startAt": start_at, "maxResults": 100})
            payload = self._get_json(
                url=(
                    f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/"
                    f"{quote(normalized_issue, safe='')}/comment?{query}"
                ),
                access_token=access_token,
            )
            values = payload.get("comments") if isinstance(payload, dict) else None
            if not isinstance(values, list):
                raise AtlassianOAuthError("Issue comments response missing comments list")

            for index, item in enumerate(values):
                if not isinstance(item, dict):
                    raise AtlassianOAuthError(f"Issue comments response item {index} was not an object")
                comment_id = str(item.get("id") or "").strip()
                if not comment_id:
                    raise AtlassianOAuthError(f"Issue comments response item {index} missing comment id")
                author = item.get("author") if isinstance(item.get("author"), dict) else {}
                comments.append(
                    JiraIssueComment(
                        comment_id=comment_id,
                        body=_adf_to_plain_text(item.get("body")).strip(),
                        author_display_name=(
                            str(author.get("displayName") or "").strip() or None
                            if isinstance(author, dict)
                            else None
                        ),
                        updated_at=_parse_jira_datetime(item.get("updated")),
                    )
                )

            if not values:
                break
            total = int(payload.get("total") or 0) if isinstance(payload, dict) else 0
            batch_size = int(payload.get("maxResults") or len(values)) if isinstance(payload, dict) else len(values)
            start_at += max(1, batch_size)
            if total and start_at >= total:
                break
            if len(values) < batch_size:
                break

        return comments

    def list_issue_attachments(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
    ) -> list[JiraIssueAttachment]:
        normalized_issue = issue_id_or_key.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for issue attachments fetch")

        query = urlencode({"fields": "attachment"})
        payload = self._get_json(
            url=(
                f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/"
                f"{quote(normalized_issue, safe='')}?{query}"
            ),
            access_token=access_token,
        )
        if not isinstance(payload, dict):
            raise AtlassianOAuthError("Issue attachments response was not an object")

        fields = payload.get("fields")
        if not isinstance(fields, dict):
            raise AtlassianOAuthError("Issue attachments response missing fields object")
        attachments_raw = fields.get("attachment")
        if attachments_raw is None:
            return []
        if not isinstance(attachments_raw, list):
            raise AtlassianOAuthError("Issue attachments response missing attachment list")

        attachments: list[JiraIssueAttachment] = []
        for index, item in enumerate(attachments_raw):
            if not isinstance(item, dict):
                raise AtlassianOAuthError(f"Issue attachments response item {index} was not an object")
            attachment_id = str(item.get("id") or "").strip()
            filename = str(item.get("filename") or "").strip()
            content_url = str(item.get("content") or "").strip()
            if not attachment_id or not filename or not content_url:
                raise AtlassianOAuthError(f"Issue attachments response item {index} missing required fields")
            size_raw = item.get("size")
            attachments.append(
                JiraIssueAttachment(
                    attachment_id=attachment_id,
                    filename=filename,
                    content_url=content_url,
                    mime_type=str(item.get("mimeType") or "").strip() or None,
                    size_bytes=int(size_raw) if isinstance(size_raw, int) else None,
                    created_at=_parse_jira_datetime(item.get("created")),
                )
            )
        return attachments

    def create_issues_bulk(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
        issues: list[JiraIssueCreateInput],
    ) -> JiraIssueBulkCreateResult:
        available_issue_types = self._list_project_issue_types_for_create(
            access_token=access_token,
            cloud_id=cloud_id,
            project_key=project_key,
        )
        issue_updates = []
        for index, issue in enumerate(issues):
            summary = issue.summary.strip()
            if not summary:
                raise AtlassianOAuthError(f"Jira bulk create issue {index} missing summary")
            issue_updates.append({"fields": self._build_issue_fields_payload(
                project_key=project_key,
                issue=issue,
                available_issue_types=available_issue_types,
            )})

        if not issue_updates:
            raise AtlassianOAuthError("No valid issue payloads were provided for Jira bulk create")

        payload = self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/bulk",
            access_token=access_token,
            payload={"issueUpdates": issue_updates},
        )
        created_raw = payload.get("issues") if isinstance(payload, dict) else None
        errors_raw = payload.get("errors") if isinstance(payload, dict) else None
        if not isinstance(created_raw, list):
            raise AtlassianOAuthError("Jira bulk create response missing issues list")
        if not isinstance(errors_raw, list):
            raise AtlassianOAuthError("Jira bulk create response missing errors list")

        created: list[JiraIssueCreateResult] = []
        for index, item in enumerate(created_raw):
            if not isinstance(item, dict):
                raise AtlassianOAuthError(f"Jira bulk create response issue {index} was not an object")
            key = item.get("key")
            issue_id = item.get("id")
            if not isinstance(key, str) or not key or not isinstance(issue_id, str) or not issue_id:
                raise AtlassianOAuthError(f"Jira bulk create response issue {index} missing issue key/id")
            created.append(JiraIssueCreateResult(key=key, issue_id=issue_id))

        errors: list[str] = []
        for index, item in enumerate(errors_raw):
            if not isinstance(item, dict):
                raise AtlassianOAuthError(f"Jira bulk create response error {index} was not an object")
            failed_element = item.get("failedElementNumber")
            element_errors = item.get("elementErrors") if isinstance(item.get("elementErrors"), dict) else {}
            error_messages = element_errors.get("errorMessages")
            reason_parts: list[str] = []
            if isinstance(error_messages, list):
                reason_parts.extend(str(part).strip() for part in error_messages if str(part).strip())
            field_errors = element_errors.get("errors")
            if isinstance(field_errors, dict):
                for field_name, field_reason in field_errors.items():
                    normalized_field_name = str(field_name).strip()
                    normalized_field_reason = str(field_reason).strip()
                    if normalized_field_name and normalized_field_reason:
                        reason_parts.append(f"{normalized_field_name}: {normalized_field_reason}")
            if not reason_parts:
                raise AtlassianOAuthError(f"Jira bulk create response error {index} missing error reason")
            reason_text = "; ".join(reason_parts)
            errors.append(f"Item {failed_element}: {reason_text}")

        return JiraIssueBulkCreateResult(created=created, errors=errors)

    def create_issue(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
        issue: JiraIssueCreateInput,
    ) -> JiraIssueCreateResult:
        available_issue_types = self._list_project_issue_types_for_create(
            access_token=access_token,
            cloud_id=cloud_id,
            project_key=project_key,
        )
        payload = self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue",
            access_token=access_token,
            payload={
                "fields": self._build_issue_fields_payload(
                    project_key=project_key,
                    issue=issue,
                    available_issue_types=available_issue_types,
                )
            },
        )
        if not isinstance(payload, dict):
            raise AtlassianOAuthError("Jira create issue response was not an object")
        key = str(payload.get("key") or "").strip()
        issue_id = str(payload.get("id") or "").strip()
        if not key or not issue_id:
            raise AtlassianOAuthError("Jira create issue response did not include issue key/id")
        return JiraIssueCreateResult(key=key, issue_id=issue_id)

    def update_issue_fields(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
        description: str | dict[str, Any],
        labels: list[str],
    ) -> None:
        normalized_issue = issue_id_or_key.strip()
        normalized_summary = summary.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for issue update")
        if not normalized_summary:
            raise AtlassianOAuthError("Missing issue summary for issue update")

        self._request_json(
            method="PUT",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}",
            access_token=access_token,
            payload={
                "fields": {
                    "summary": normalized_summary,
                    "description": _to_adf_description(description),
                    "labels": [label for label in labels if label],
                }
            },
        )

    def add_issue_labels(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        labels: list[str],
    ) -> None:
        normalized_issue = issue_id_or_key.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for issue label update")
        normalized_labels = [str(label).strip() for label in labels if str(label).strip()]
        if not normalized_labels:
            return
        self._request_json(
            method="PUT",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}",
            access_token=access_token,
            payload={
                "update": {
                    "labels": [{"add": label} for label in normalized_labels],
                }
            },
        )

    def replace_issue_labels(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        labels: list[str],
    ) -> None:
        normalized_issue = issue_id_or_key.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for issue label replace")
        normalized_labels = [str(label).strip() for label in labels if str(label).strip()]
        self._request_json(
            method="PUT",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}",
            access_token=access_token,
            payload={
                "fields": {
                    "labels": normalized_labels,
                }
            },
        )

    def add_issue_comment(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        comment: str | dict[str, Any],
    ) -> dict[str, Any]:
        normalized_issue = issue_id_or_key.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for comment create")

        payload = self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}/comment",
            access_token=access_token,
            payload={"body": _to_adf_description(comment)},
        )
        if not isinstance(payload, dict):
            raise AtlassianOAuthError("Jira comment create response was not an object")
        return payload

    def add_issue_link(
        self,
        *,
        access_token: str,
        cloud_id: str,
        inward_issue_key: str,
        outward_issue_key: str,
        link_type: str = "Relates",
    ) -> dict[str, Any]:
        inward = str(inward_issue_key or "").strip()
        outward = str(outward_issue_key or "").strip()
        normalized_link_type = str(link_type or "").strip() or "Relates"
        if not inward or not outward:
            raise AtlassianOAuthError("Missing issue key for issue link create")
        payload = self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issueLink",
            access_token=access_token,
            payload={
                "type": {"name": normalized_link_type},
                "inwardIssue": {"key": inward},
                "outwardIssue": {"key": outward},
            },
        )
        if isinstance(payload, dict):
            return payload
        raise AtlassianOAuthError("Jira issue link response was not an object")

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
    ) -> dict[str, Any]:
        normalized_issue = issue_id_or_key.strip()
        normalized_global_id = global_id.strip()
        normalized_relationship = relationship.strip()
        normalized_title = title.strip()
        normalized_url = url.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for remote issue link")
        if not normalized_global_id:
            raise AtlassianOAuthError("Missing global id for remote issue link")
        if not normalized_relationship:
            raise AtlassianOAuthError("Missing relationship for remote issue link")
        if not normalized_title:
            raise AtlassianOAuthError("Missing title for remote issue link")
        if not normalized_url:
            raise AtlassianOAuthError("Missing url for remote issue link")
        payload = self._request_json(
            method="POST",
            url=(
                f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/"
                f"{quote(normalized_issue, safe='')}/remotelink"
            ),
            access_token=access_token,
            payload={
                "globalId": normalized_global_id,
                "relationship": normalized_relationship,
                "object": {
                    "title": normalized_title,
                    "url": normalized_url,
                },
            },
        )
        if not isinstance(payload, dict):
            raise AtlassianOAuthError("Remote issue link response was not an object")
        return payload

    def transition_issue(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        target_status: str,
    ) -> dict[str, Any]:
        normalized_issue = issue_id_or_key.strip()
        normalized_target = target_status.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for transition")
        if not normalized_target:
            raise AtlassianOAuthError("Missing target status for transition")

        transitions_payload = self._request_json(
            method="GET",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}/transitions",
            access_token=access_token,
        )
        transitions = transitions_payload.get("transitions") if isinstance(transitions_payload, dict) else None
        if not isinstance(transitions, list):
            raise AtlassianOAuthError("Jira transitions response did not include transitions list")

        desired = normalized_target.casefold()
        selected_transition_id: str | None = None
        selected_transition_name: str | None = None
        selected_to_status: str | None = None
        available_statuses: list[str] = []

        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            transition_id = transition.get("id")
            transition_name = transition.get("name")
            to_obj = transition.get("to") if isinstance(transition.get("to"), dict) else {}
            to_name = to_obj.get("name") if isinstance(to_obj, dict) else None
            if isinstance(to_name, str) and to_name.strip():
                available_statuses.append(to_name.strip())
            if not isinstance(transition_id, str) or not transition_id.strip():
                continue
            normalized_name = transition_name.strip().casefold() if isinstance(transition_name, str) else ""
            normalized_to = to_name.strip().casefold() if isinstance(to_name, str) else ""
            if desired in {normalized_name, normalized_to}:
                selected_transition_id = transition_id.strip()
                selected_transition_name = transition_name.strip() if isinstance(transition_name, str) else None
                selected_to_status = to_name.strip() if isinstance(to_name, str) else None
                break

        if not selected_transition_id:
            available = ", ".join(sorted({item for item in available_statuses if item})) or "none"
            raise AtlassianOAuthError(
                f"Transition '{normalized_target}' not available for {normalized_issue}; available statuses: {available}"
            )

        self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}/transitions",
            access_token=access_token,
            payload={"transition": {"id": selected_transition_id}},
        )
        return {
            "transition_id": selected_transition_id,
            "transition_name": selected_transition_name,
            "to_status": selected_to_status,
        }

    def update_issue_summary(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
    ) -> None:
        normalized_issue = issue_id_or_key.strip()
        normalized_summary = summary.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for issue update")
        if not normalized_summary:
            raise AtlassianOAuthError("Missing issue summary for issue update")

        self._request_json(
            method="PUT",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}",
            access_token=access_token,
            payload={
                "fields": {
                    "summary": normalized_summary,
                }
            },
        )

    def update_issue_summary_and_description(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
        summary: str,
        description: str | dict[str, Any],
    ) -> None:
        normalized_issue = issue_id_or_key.strip()
        normalized_summary = summary.strip()
        if not normalized_issue:
            raise AtlassianOAuthError("Missing issue id/key for issue update")
        if not normalized_summary:
            raise AtlassianOAuthError("Missing issue summary for issue update")

        self._request_json(
            method="PUT",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}",
            access_token=access_token,
            payload={
                "fields": {
                    "summary": normalized_summary,
                    "description": _to_adf_description(description),
                }
            },
        )

    def _list_project_issue_types_for_create(
        self,
        *,
        access_token: str,
        cloud_id: str,
        project_key: str,
    ) -> list[str]:
        normalized_project_key = project_key.strip().upper()
        if not normalized_project_key:
            raise AtlassianOAuthError("Missing project key for Jira issue type discovery")

        endpoint = (
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/createmeta/"
            f"{quote(normalized_project_key, safe='')}/issuetypes"
        )
        parsed = _parse_issue_type_names_from_payload(self._get_json(url=endpoint, access_token=access_token))
        if not parsed:
            raise AtlassianOAuthError(f"Jira issue type discovery returned no issue types for project {normalized_project_key}")
        return parsed

    def _build_issue_fields_payload(
        self,
        *,
        project_key: str,
        issue: JiraIssueCreateInput,
        available_issue_types: list[str],
    ) -> dict[str, Any]:
        summary = issue.summary.strip()
        if not summary:
            raise AtlassianOAuthError("Missing issue summary for Jira create")
        requested_issue_type = str(issue.issue_type or "").strip().lower()
        parent_issue_key = str(issue.parent_issue_key or "").strip() or None
        if parent_issue_key and requested_issue_type in {"sub-task", "subtask"}:
            available_by_lower = {name.lower() for name in available_issue_types}
            if not ({"sub-task", "subtask"} & available_by_lower):
                raise AtlassianOAuthError(
                    f"Subtask issue type is not available for project {project_key}"
                )
        issue_type = _select_issue_type_name(
            requested_issue_type=issue.issue_type,
            available_issue_types=available_issue_types,
        )
        fields: dict[str, Any] = {
            "project": {"key": project_key},
            "issuetype": {"name": issue_type},
            "summary": summary,
            "description": _to_adf_description(issue.description),
            "labels": [label for label in issue.labels if label],
        }
        if parent_issue_key:
            if requested_issue_type in {"sub-task", "subtask"} and issue_type.lower() not in {"sub-task", "subtask"}:
                raise AtlassianOAuthError(
                    f"Subtask issue type is not available for project {project_key}"
                )
            if issue_type.lower() == "epic":
                raise AtlassianOAuthError("Epic issue type cannot be created as a child issue")
            fields["parent"] = {"key": parent_issue_key}
        return fields


def _to_adf_description(text: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(text, dict):
        if text.get("type") == "doc" and isinstance(text.get("content"), list):
            return _truncate_adf_document_to_limit(text)
        text = ""
    elif text is None:
        text = ""
    elif not isinstance(text, str):
        text = str(text)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        lines = ["No description provided"]
    paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines]
    return _truncate_adf_document_to_limit({
        "type": "doc",
        "version": 1,
        "content": paragraphs,
    })


def _adf_document_size_bytes(document: dict[str, Any]) -> int:
    return len(json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _truncation_paragraph() -> dict[str, Any]:
    return {
        "type": "paragraph",
        "content": [{"type": "text", "text": _JIRA_ADF_TRUNCATION_NOTICE}],
    }


def _ensure_truncation_notice(document: dict[str, Any]) -> None:
    content = document.setdefault("content", [])
    if not isinstance(content, list):
        document["content"] = []
        content = document["content"]
    if not content:
        content.append(_truncation_paragraph())
        return
    last = content[-1]
    if _adf_to_plain_text(last).strip() == _JIRA_ADF_TRUNCATION_NOTICE:
        return
    content.append(_truncation_paragraph())


def _collect_text_nodes(node: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(node, dict):
        if isinstance(node.get("text"), str):
            nodes.append(node)
        content = node.get("content")
        if isinstance(content, list):
            for child in content:
                nodes.extend(_collect_text_nodes(child))
    elif isinstance(node, list):
        for child in node:
            nodes.extend(_collect_text_nodes(child))
    return nodes


def _truncate_adf_document_to_limit(document: dict[str, Any]) -> dict[str, Any]:
    candidate = copy.deepcopy(document)
    if _adf_document_size_bytes(candidate) <= MAX_JIRA_ADF_DOCUMENT_BYTES:
        return candidate

    _ensure_truncation_notice(candidate)
    content = candidate.get("content")
    if not isinstance(content, list):
        return {"type": "doc", "version": 1, "content": [_truncation_paragraph()]}

    while _adf_document_size_bytes(candidate) > MAX_JIRA_ADF_DOCUMENT_BYTES and len(content) > 1:
        content.pop(-2)

    if _adf_document_size_bytes(candidate) <= MAX_JIRA_ADF_DOCUMENT_BYTES:
        return candidate

    text_nodes = _collect_text_nodes(content[:-1])
    for node in reversed(text_nodes):
        text = str(node.get("text") or "")
        while text and _adf_document_size_bytes(candidate) > MAX_JIRA_ADF_DOCUMENT_BYTES:
            overflow = _adf_document_size_bytes(candidate) - MAX_JIRA_ADF_DOCUMENT_BYTES
            shrink_by = max(64, overflow + 8)
            next_len = max(0, len(text) - shrink_by)
            text = text[:next_len].rstrip()
            node["text"] = f"{text}…" if text else ""
        if _adf_document_size_bytes(candidate) <= MAX_JIRA_ADF_DOCUMENT_BYTES:
            return candidate

    minimal = {"type": "doc", "version": 1, "content": [_truncation_paragraph()]}
    return minimal


def _adf_to_plain_text(node: object) -> str:
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        if isinstance(node, list):
            return "\n".join(part for part in (_adf_to_plain_text(item) for item in node) if part).strip()
        return ""

    node_type = str(node.get("type") or "").strip().lower()
    if node_type == "text":
        text = node.get("text")
        return text if isinstance(text, str) else ""

    content = node.get("content")
    if isinstance(content, list):
        rendered = [_adf_to_plain_text(item).strip() for item in content]
        rendered = [part for part in rendered if part]
        if not rendered:
            return ""
        if node_type in {"doc", "bulletlist", "orderedlist"}:
            return "\n".join(rendered).strip()
        if node_type == "listitem":
            return "\n".join(rendered).strip()
        if node_type in {"heading", "paragraph"}:
            return " ".join(rendered).strip()
        return " ".join(rendered).strip()
    return ""


def _parse_jira_datetime(value: object) -> datetime | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_issue_type_names_from_payload(payload: dict[str, Any] | list[Any]) -> list[str]:
    candidates: list[object] = []
    if isinstance(payload, list):
        candidates.extend(payload)
    elif isinstance(payload, dict):
        for key in ("values", "issueTypes", "issuetypes", "projects"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)

    names: list[str] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        raw_name = item.get("name")
        name = str(raw_name).strip() if raw_name is not None else ""
        if name and name not in names:
            names.append(name)

        nested_issue_types = item.get("issueTypes") or item.get("issuetypes")
        if isinstance(nested_issue_types, list):
            for nested in nested_issue_types:
                if not isinstance(nested, dict):
                    continue
                raw_nested_name = nested.get("name")
                nested_name = str(raw_nested_name).strip() if raw_nested_name is not None else ""
                if nested_name and nested_name not in names:
                    names.append(nested_name)
    return names


def _select_issue_type_name(*, requested_issue_type: str | None, available_issue_types: list[str]) -> str:
    requested = str(requested_issue_type or "").strip()
    if not available_issue_types:
        raise AtlassianOAuthError("Jira issue type discovery returned no issue types")
    if not requested:
        raise AtlassianOAuthError("Jira issue create input missing issue type")

    by_lower = {name.lower(): name for name in available_issue_types}
    direct = by_lower.get(requested.lower())
    if direct:
        return direct
    available = ", ".join(available_issue_types)
    raise AtlassianOAuthError(f"Jira issue type '{requested}' is not available; available issue types: {available}")
