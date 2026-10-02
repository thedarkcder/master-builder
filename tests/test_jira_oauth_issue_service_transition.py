from __future__ import annotations

import pytest

from orchestrator.tools.atlassian_oauth_issue_service import JiraOAuthIssueService
from orchestrator.tools.atlassian_oauth_models import AtlassianOAuthError


def test_transition_issue_by_target_status_name() -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def _request_json(*, method: str, url: str, access_token: str, payload=None):  # noqa: ANN001
        _ = access_token
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/transitions"):
            return {
                "transitions": [
                    {
                        "id": "11",
                        "name": "Start progress",
                        "to": {"name": "In Progress"},
                    },
                    {"id": "21", "name": "Done", "to": {"name": "Done"}},
                ]
            }
        return {}

    service = JiraOAuthIssueService(get_json=lambda **_: {}, request_json=_request_json)
    result = service.transition_issue(
        access_token="token",
        cloud_id="cloud",
        issue_id_or_key="MAB-1",
        target_status="In Progress",
    )

    assert result["transition_id"] == "11"
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"
    assert calls[1][2] == {"transition": {"id": "11"}}


def test_transition_issue_raises_when_target_not_available() -> None:
    def _request_json(*, method: str, url: str, access_token: str, payload=None):  # noqa: ANN001
        _ = method, url, access_token, payload
        return {"transitions": [{"id": "21", "name": "Done", "to": {"name": "Done"}}]}

    service = JiraOAuthIssueService(get_json=lambda **_: {}, request_json=_request_json)
    with pytest.raises(AtlassianOAuthError):
        service.transition_issue(
            access_token="token",
            cloud_id="cloud",
            issue_id_or_key="MAB-1",
            target_status="In Progress",
        )
