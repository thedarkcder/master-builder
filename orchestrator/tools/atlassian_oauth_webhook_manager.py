from __future__ import annotations

import re
from typing import Any, Callable

from orchestrator.tools.atlassian_oauth_models import AtlassianOAuthError

_TRANSIENT_WEBHOOK_ERROR_CODES = {502, 503, 504}
_TRANSIENT_WEBHOOK_MAX_ATTEMPTS = 3


class AtlassianOAuthWebhookManager:
    def __init__(
        self,
        *,
        request_json: Callable[
            [str, str, str, dict[str, Any] | None], dict[str, Any] | list[Any]
        ],
    ) -> None:
        self._request_json = request_json

    def _request_json_with_retries(
        self,
        method: str,
        url: str,
        access_token: str,
        payload: dict[str, Any] | None,
    ) -> dict[str, Any] | list[Any]:
        last_error: AtlassianOAuthError | None = None
        for _attempt in range(_TRANSIENT_WEBHOOK_MAX_ATTEMPTS):
            try:
                return self._request_json(method, url, access_token, payload)
            except AtlassianOAuthError as exc:
                last_error = exc
                if not _is_transient_webhook_error(exc):
                    raise
        if last_error is not None:
            raise last_error
        raise AtlassianOAuthError("Webhook request failed without a captured error")

    def register_webhook(
        self,
        *,
        access_token: str,
        cloud_id: str,
        callback_url: str,
        jql_filter: str,
        events: list[str],
    ) -> list[int]:
        payload = self._request_json_with_retries(
            "POST",
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/webhook",
            access_token,
            {
                "url": callback_url,
                "webhooks": [
                    {
                        "jqlFilter": jql_filter,
                        "events": events,
                    }
                ],
            },
        )
        normalized_ids = _extract_created_webhook_ids(payload)
        if not normalized_ids:
            summary = _summarize_webhook_registration_failure(payload)
            raise AtlassianOAuthError(
                f"Webhook registration did not return any webhook IDs ({summary})"
            )
        return normalized_ids

    def list_webhooks(
        self,
        *,
        access_token: str,
        cloud_id: str,
    ) -> list[dict[str, Any]]:
        payload = self._request_json_with_retries(
            "GET",
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/webhook",
            access_token,
            None,
        )
        if not isinstance(payload, dict):
            return []
        values = payload.get("values")
        if not isinstance(values, list):
            return []
        return [item for item in values if isinstance(item, dict)]

    def delete_webhooks(
        self,
        *,
        access_token: str,
        cloud_id: str,
        webhook_ids: list[int],
    ) -> None:
        if not webhook_ids:
            return
        self._request_json_with_retries(
            "DELETE",
            f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3/webhook",
            access_token,
            {"webhookIds": webhook_ids},
        )


def _extract_created_webhook_ids(payload: dict[str, Any] | list[Any]) -> list[int]:
    candidates: list[object] = []
    if isinstance(payload, dict):
        candidates.extend(
            [payload.get("createdWebhookId"), payload.get("createdWebhookIds")]
        )
        registration_results = payload.get("webhookRegistrationResult")
        if isinstance(registration_results, list):
            for item in registration_results:
                if isinstance(item, dict):
                    candidates.extend(
                        [item.get("createdWebhookId"), item.get("createdWebhookIds")]
                    )

    normalized_ids: list[int] = []
    for candidate in candidates:
        if isinstance(candidate, int):
            normalized_ids.append(candidate)
            continue
        if isinstance(candidate, str) and candidate.isdigit():
            normalized_ids.append(int(candidate))
            continue
        if isinstance(candidate, list):
            for item in candidate:
                if isinstance(item, int):
                    normalized_ids.append(item)
                elif isinstance(item, str) and item.isdigit():
                    normalized_ids.append(int(item))
    return normalized_ids


def _is_transient_webhook_error(exc: AtlassianOAuthError) -> bool:
    message = str(exc)
    code_matches = re.findall(r"\b(\d{3})\b", message)
    if not code_matches:
        return False
    return any(int(code) in _TRANSIENT_WEBHOOK_ERROR_CODES for code in code_matches)


def _summarize_webhook_registration_failure(payload: dict[str, Any] | list[Any]) -> str:
    if isinstance(payload, list):
        return f"response was a list with {len(payload)} item(s)"

    if not isinstance(payload, dict):
        return f"unexpected response type: {type(payload).__name__}"

    error_messages = payload.get("errorMessages")
    if isinstance(error_messages, list) and error_messages:
        joined = "; ".join(
            str(item).strip() for item in error_messages if str(item).strip()
        )
        if joined:
            return f"errorMessages: {joined}"

    errors = payload.get("errors")
    if isinstance(errors, dict) and errors:
        pairs = ", ".join(f"{key}: {value}" for key, value in errors.items())
        return f"errors: {pairs}"

    registration_results = payload.get("webhookRegistrationResult")
    if isinstance(registration_results, list) and registration_results:
        item_summaries: list[str] = []
        for item in registration_results:
            if not isinstance(item, dict):
                continue
            item_errors = item.get("errors")
            if isinstance(item_errors, list) and item_errors:
                joined = "; ".join(
                    str(part).strip() for part in item_errors if str(part).strip()
                )
                if joined:
                    item_summaries.append(joined)
        if item_summaries:
            return "webhookRegistrationResult errors: " + " | ".join(item_summaries)
        return f"webhookRegistrationResult present without IDs ({len(registration_results)} item(s))"

    keys = ", ".join(sorted(str(key) for key in payload.keys()))
    return f"response keys: {keys or 'none'}"
