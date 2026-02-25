from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote, urlencode

from orchestrator.tools.jira_oauth_models import (
    JiraIssueBulkCreateResult,
    JiraIssueCreateInput,
    JiraIssueCreateResult,
    JiraIssueDetail,
    JiraIssuePreview,
    JiraOAuthError,
    JiraProject,
)

logger = logging.getLogger(__name__)


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
            raise JiraOAuthError("Project search response missing values list")

        projects: list[JiraProject] = []
        for item in values:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            name = item.get("name")
            if not isinstance(key, str) or not key:
                continue
            if not isinstance(name, str) or not name:
                name = key
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
    ) -> list[JiraIssuePreview]:
        bounded_max_results = max(1, min(max_results, 50))
        query = urlencode(
            {
                "jql": jql,
                "maxResults": bounded_max_results,
                "fields": "summary,status",
            }
        )
        payload = self._get_json(
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/search/jql?{query}",
            access_token=access_token,
        )
        issues = payload.get("issues") if isinstance(payload, dict) else None
        if not isinstance(issues, list):
            raise JiraOAuthError("Issue search response missing issues list")

        results: list[JiraIssuePreview] = []
        for item in issues:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            fields = item.get("fields")
            if not isinstance(fields, dict):
                fields = {}
            summary = fields.get("summary")
            status_obj = fields.get("status")
            status_name = status_obj.get("name") if isinstance(status_obj, dict) else None

            if not isinstance(key, str) or not key:
                continue
            if not isinstance(summary, str) or not summary:
                summary = key
            if not isinstance(status_name, str) or not status_name:
                status_name = "Unknown"

            results.append(JiraIssuePreview(key=key, summary=summary, status=status_name))
        return results

    def get_issue_detail(
        self,
        *,
        access_token: str,
        cloud_id: str,
        issue_id_or_key: str,
    ) -> JiraIssueDetail:
        normalized_issue = issue_id_or_key.strip()
        if not normalized_issue:
            raise JiraOAuthError("Missing issue id/key for issue detail fetch")

        query = urlencode({"fields": "summary,status,description,labels"})
        payload = self._get_json(
            url=(
                f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/"
                f"{quote(normalized_issue, safe='')}?{query}"
            ),
            access_token=access_token,
        )
        if not isinstance(payload, dict):
            raise JiraOAuthError("Issue detail response was not an object")

        key_raw = payload.get("key")
        key = str(key_raw).strip() if isinstance(key_raw, str) and key_raw.strip() else normalized_issue

        fields = payload.get("fields")
        if not isinstance(fields, dict):
            fields = {}

        summary_raw = fields.get("summary")
        summary = summary_raw.strip() if isinstance(summary_raw, str) and summary_raw.strip() else key

        status_name = "Unknown"
        status_obj = fields.get("status")
        if isinstance(status_obj, dict):
            status_raw = status_obj.get("name")
            if isinstance(status_raw, str) and status_raw.strip():
                status_name = status_raw.strip()

        description = _adf_to_plain_text(fields.get("description")).strip()
        labels_raw = fields.get("labels")
        labels: list[str] = []
        if isinstance(labels_raw, list):
            labels = [str(label).strip() for label in labels_raw if str(label).strip()]
        return JiraIssueDetail(
            key=key,
            summary=summary,
            status=status_name,
            description=description,
            labels=labels,
        )

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
        for issue in issues:
            summary = issue.summary.strip()
            if not summary:
                continue
            issue_type = _select_issue_type_name(
                requested_issue_type=issue.issue_type,
                available_issue_types=available_issue_types,
            )
            issue_updates.append(
                {
                    "fields": {
                        "project": {"key": project_key},
                        "issuetype": {"name": issue_type},
                        "summary": summary,
                        "description": _to_adf_description(issue.description),
                        "labels": [label for label in issue.labels if label],
                    }
                }
            )

        if not issue_updates:
            raise JiraOAuthError("No valid issue payloads were provided for Jira bulk create")

        payload = self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/bulk",
            access_token=access_token,
            payload={"issueUpdates": issue_updates},
        )
        created_raw = payload.get("issues") if isinstance(payload, dict) else None
        errors_raw = payload.get("errors") if isinstance(payload, dict) else None
        if not isinstance(created_raw, list):
            created_raw = []
        if not isinstance(errors_raw, list):
            errors_raw = []

        created: list[JiraIssueCreateResult] = []
        for item in created_raw:
            if not isinstance(item, dict):
                continue
            key = item.get("key")
            issue_id = item.get("id")
            if isinstance(key, str) and key and isinstance(issue_id, str) and issue_id:
                created.append(JiraIssueCreateResult(key=key, issue_id=issue_id))

        errors: list[str] = []
        for item in errors_raw:
            if not isinstance(item, dict):
                continue
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
            reason_text = "; ".join(reason_parts) or "Unknown error"
            errors.append(f"Item {failed_element}: {reason_text}")

        return JiraIssueBulkCreateResult(created=created, errors=errors)

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
            raise JiraOAuthError("Missing issue id/key for issue update")
        if not normalized_summary:
            raise JiraOAuthError("Missing issue summary for issue update")

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
            raise JiraOAuthError("Missing issue id/key for issue label update")
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
            raise JiraOAuthError("Missing issue id/key for comment create")

        payload = self._request_json(
            method="POST",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}/comment",
            access_token=access_token,
            payload={"body": _to_adf_description(comment)},
        )
        if not isinstance(payload, dict):
            raise JiraOAuthError("Jira comment create response was not an object")
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
            raise JiraOAuthError("Missing issue id/key for transition")
        if not normalized_target:
            raise JiraOAuthError("Missing target status for transition")

        transitions_payload = self._request_json(
            method="GET",
            url=f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/{quote(normalized_issue, safe='')}/transitions",
            access_token=access_token,
        )
        transitions = transitions_payload.get("transitions") if isinstance(transitions_payload, dict) else None
        if not isinstance(transitions, list):
            raise JiraOAuthError("Jira transitions response did not include transitions list")

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
            raise JiraOAuthError(
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
            raise JiraOAuthError("Missing issue id/key for issue update")
        if not normalized_summary:
            raise JiraOAuthError("Missing issue summary for issue update")

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
            raise JiraOAuthError("Missing issue id/key for issue update")
        if not normalized_summary:
            raise JiraOAuthError("Missing issue summary for issue update")

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
            return []

        for endpoint in (
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/createmeta/{quote(normalized_project_key, safe='')}/issuetypes",
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/issue/createmeta?projectKeys={quote(normalized_project_key, safe='')}&expand=projects.issuetypes",
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/project/{quote(normalized_project_key, safe='')}",
        ):
            try:
                payload = self._get_json(url=endpoint, access_token=access_token)
            except JiraOAuthError as exc:
                logger.exception(
                    "jira_issue_type_discovery_endpoint_failed project_key=%s endpoint=%s error=%s",
                    normalized_project_key,
                    endpoint,
                    exc,
                )
                continue
            parsed = _parse_issue_type_names_from_payload(payload)
            if parsed:
                return parsed
        return []


def _to_adf_description(text: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(text, dict):
        if text.get("type") == "doc" and isinstance(text.get("content"), list):
            return text
        text = ""
    elif text is None:
        text = ""
    elif not isinstance(text, str):
        text = str(text)

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        lines = ["No description provided"]
    paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in lines]
    return {
        "type": "doc",
        "version": 1,
        "content": paragraphs,
    }


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
        return requested or "Task"

    by_lower = {name.lower(): name for name in available_issue_types}
    if requested:
        direct = by_lower.get(requested.lower())
        if direct:
            return direct

    def _first_present(candidates: list[str]) -> str | None:
        for candidate in candidates:
            existing = by_lower.get(candidate.lower())
            if existing:
                return existing
        return None

    normalized = requested.lower()
    if normalized in {"bug", "defect", "incident"}:
        bug_choice = _first_present(["Bug", "Defect", "Incident", "Task", "Story", "Issue"])
        if bug_choice:
            return bug_choice
    if normalized in {"story", "feature", "enhancement"}:
        story_choice = _first_present(["Story", "Task", "Issue", "Epic", "Bug"])
        if story_choice:
            return story_choice
    if normalized in {"epic"}:
        epic_choice = _first_present(["Epic", "Story", "Task", "Issue"])
        if epic_choice:
            return epic_choice

    default_choice = _first_present(["Task", "Story", "Issue", "Bug"])
    if default_choice:
        return default_choice
    return available_issue_types[0]
