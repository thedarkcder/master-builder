from __future__ import annotations

import json
import unittest
from io import BytesIO
from urllib.error import HTTPError

from orchestrator.tools.jira_mcp_adapter import JiraMcpAdapter
from orchestrator.tools.jira_oauth_callback_flow import JiraOAuthCallbackFlow
from orchestrator.tools.jira_oauth_http import JiraOAuthHttpClient
from orchestrator.tools.jira_oauth_issue_service import (
    JiraOAuthIssueService,
    _adf_to_plain_text,
    _parse_issue_type_names_from_payload,
    _select_issue_type_name,
)
from orchestrator.tools.jira_oauth_models import JiraIssueCreateInput, JiraOAuthClientConfig, JiraOAuthError


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False


class JiraMcpAdapterTests(unittest.TestCase):
    def test_discover_capabilities_health(self) -> None:
        adapter = JiraMcpAdapter(
            [
                "search_issues",
                "get_issue",
                "add_comment",
                "add_labels",
                "remove_labels",
                "transition_issue",
            ]
        )
        report = adapter.discover_capabilities()
        self.assertTrue(report.is_healthy)
        self.assertIn("transition_issue", report.optional_available)

    def test_discover_capabilities_missing_required(self) -> None:
        adapter = JiraMcpAdapter(["search_issues"])
        report = adapter.discover_capabilities()
        self.assertFalse(report.is_healthy)
        self.assertIn("get_issue", report.missing_required)


class JiraOAuthCallbackFlowTests(unittest.TestCase):
    def _config(self) -> JiraOAuthClientConfig:
        return JiraOAuthClientConfig(
            client_id="cid",
            client_secret="csecret",
            redirect_uri="https://example.test/callback",
        )

    def test_build_authorize_url_contains_required_parameters(self) -> None:
        flow = JiraOAuthCallbackFlow(config=self._config(), post_json=lambda *_args, **_kwargs: {}, get_json=lambda *_args, **_kwargs: [])
        url = flow.build_authorize_url(state="state-1")
        self.assertIn("audience=api.atlassian.com", url)
        self.assertIn("client_id=cid", url)
        self.assertIn("state=state-1", url)

    def test_exchange_code_and_refresh_parse_tokens(self) -> None:
        def _post_json(_url: str, payload: dict):
            if payload["grant_type"] == "authorization_code":
                return {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600, "scope": "read write"}
            return {"access_token": "a2", "refresh_token": "r2", "expires_in": 7200, "scope": ""}

        flow = JiraOAuthCallbackFlow(config=self._config(), post_json=_post_json, get_json=lambda *_args, **_kwargs: [])
        token_set = flow.exchange_code(code="abc")
        self.assertEqual(token_set.access_token, "a1")
        self.assertEqual(token_set.scopes, ["read", "write"])

        refreshed = flow.refresh_tokens(refresh_token="r1")
        self.assertEqual(refreshed.access_token, "a2")
        self.assertEqual(refreshed.scopes, [])

    def test_parse_tokens_missing_fields_raise(self) -> None:
        flow = JiraOAuthCallbackFlow(config=self._config(), post_json=lambda *_args, **_kwargs: {"access_token": "a"}, get_json=lambda *_args, **_kwargs: [])
        with self.assertRaisesRegex(JiraOAuthError, "refresh_token"):
            flow.exchange_code(code="abc")

    def test_list_accessible_resources_validates_payload(self) -> None:
        flow = JiraOAuthCallbackFlow(config=self._config(), post_json=lambda *_args, **_kwargs: {}, get_json=lambda *_args, **_kwargs: {})
        with self.assertRaisesRegex(JiraOAuthError, "not a list"):
            flow.list_accessible_resources(access_token="token")

        flow_ok = JiraOAuthCallbackFlow(
            config=self._config(),
            post_json=lambda *_args, **_kwargs: {},
            get_json=lambda *_args, **_kwargs: [{"id": "c1", "url": "https://site", "name": ""}, {"id": "bad"}],
        )
        resources = flow_ok.list_accessible_resources(access_token="token")
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].name, "https://site")


class JiraOAuthHttpClientTests(unittest.TestCase):
    def test_post_get_and_request_json(self) -> None:
        captured = []

        def _opener(request, timeout=30):  # noqa: ANN001, ARG001
            captured.append((request.get_method(), request.full_url, dict(request.header_items()), request.data))
            return _FakeResponse(b'{"ok":true}')

        client = JiraOAuthHttpClient(opener=_opener)
        self.assertEqual(client.post_json(url="https://x", payload={"a": 1}), {"ok": True})
        self.assertEqual(client.get_json(url="https://x", access_token="tok"), {"ok": True})
        self.assertEqual(client.request_json(method="PUT", url="https://x", access_token="tok", payload={"b": 2}), {"ok": True})
        self.assertEqual(captured[0][0], "POST")
        self.assertEqual(captured[1][0], "GET")
        self.assertEqual(captured[2][0], "PUT")

    def test_post_multipart_and_error(self) -> None:
        def _multipart_opener(request, timeout=30):  # noqa: ANN001, ARG001
            headers = dict(request.header_items())
            self.assertIn("multipart/form-data; boundary=", str(headers.get("Content-type", "")))
            self.assertEqual(headers.get("X-atlassian-token"), "no-check")
            self.assertIn(b"file.bin", request.data)
            return _FakeResponse(b"[]")

        client = JiraOAuthHttpClient(opener=_multipart_opener)
        self.assertEqual(
            client.post_multipart(
                url="https://x",
                access_token="tok",
                filename="file.bin",
                content=b"payload",
                content_type="application/octet-stream",
            ),
            [],
        )

        error = HTTPError(url="https://x", code=500, msg="Boom", hdrs=None, fp=BytesIO(b"failure"))

        def _error_opener(request, timeout=30):  # noqa: ANN001, ARG001
            raise error

        client_error = JiraOAuthHttpClient(opener=_error_opener)
        with self.assertRaisesRegex(JiraOAuthError, "500"):
            client_error.get_json(url="https://x", access_token="tok")

    def test_empty_body_returns_empty_object(self) -> None:
        client = JiraOAuthHttpClient(opener=lambda _request, timeout=30: _FakeResponse(b""))  # noqa: ARG005
        self.assertEqual(client.get_json(url="https://x", access_token="tok"), {})


class JiraOAuthIssueServiceTests(unittest.TestCase):
    def test_issue_service_search_list_detail_and_comment(self) -> None:
        get_calls = []
        request_calls = []

        def _get_json(**kwargs):  # noqa: ANN003
            get_calls.append(kwargs["url"])
            url = kwargs["url"]
            if "project/search" in url:
                return {"values": [{"key": "B", "name": "Beta"}, {"key": "A", "name": ""}]}
            if "search/jql" in url:
                return {"issues": [{"key": "MAB-1", "fields": {"summary": "", "status": {}}}]}
            return {"key": "MAB-1", "fields": {"summary": "Summary", "status": {"name": "Done"}, "description": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}]}}}

        def _request_json(**kwargs):  # noqa: ANN003
            request_calls.append(kwargs)
            if kwargs["method"] == "POST" and kwargs["url"].endswith("/comment"):
                return {"id": "c1"}
            return {"issues": [{"key": "MAB-1", "id": "1001"}], "errors": []}

        service = JiraOAuthIssueService(get_json=_get_json, request_json=_request_json)
        projects = service.list_projects(access_token="tok", cloud_id="cloud")
        self.assertEqual([project.key for project in projects], ["A", "B"])

        issues = service.search_issues_by_jql(access_token="tok", cloud_id="cloud", jql="project=MAB", max_results=99)
        self.assertEqual(issues[0].summary, "MAB-1")
        self.assertEqual(issues[0].status, "Unknown")
        self.assertIn("maxResults=50", get_calls[1])

        detail = service.get_issue_detail(access_token="tok", cloud_id="cloud", issue_id_or_key=" MAB-1 ")
        self.assertEqual(detail.summary, "Summary")
        self.assertEqual(detail.description, "hello")

        comment = service.add_issue_comment(access_token="tok", cloud_id="cloud", issue_id_or_key="MAB-1", comment="hi")
        self.assertEqual(comment["id"], "c1")
        self.assertTrue(request_calls)

    def test_issue_service_bulk_create_and_update_validation(self) -> None:
        get_count = {"n": 0}

        def _get_json(**kwargs):  # noqa: ANN003
            get_count["n"] += 1
            if get_count["n"] == 1:
                raise JiraOAuthError("first fails")
            if "createmeta?" in kwargs["url"]:
                return {"projects": [{"issuetypes": [{"name": "Story"}, {"name": "Bug"}]}]}
            return {}

        captured = {}

        def _request_json(**kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return {"issues": [{"key": "MAB-1", "id": "100"}], "errors": [{"failedElementNumber": 1, "elementErrors": {"errors": {"summary": "bad"}}}]}

        service = JiraOAuthIssueService(get_json=_get_json, request_json=_request_json)
        result = service.create_issues_bulk(
            access_token="tok",
            cloud_id="cloud",
            project_key="mab",
            issues=[
                JiraIssueCreateInput(summary="One", description="Desc", labels=["a", ""], issue_type="Task"),
                JiraIssueCreateInput(summary=" ", description="Skip me", labels=[], issue_type=None),
            ],
        )
        self.assertEqual([item.key for item in result.created], ["MAB-1"])
        self.assertIn("summary: bad", result.errors[0])
        self.assertEqual(captured["payload"]["issueUpdates"][0]["fields"]["issuetype"]["name"], "Story")

        with self.assertRaisesRegex(JiraOAuthError, "No valid issue payloads"):
            service.create_issues_bulk(
                access_token="tok",
                cloud_id="cloud",
                project_key="MAB",
                issues=[JiraIssueCreateInput(summary=" ", description="x", labels=[], issue_type=None)],
            )

        with self.assertRaisesRegex(JiraOAuthError, "Missing issue id/key"):
            service.update_issue_fields(access_token="tok", cloud_id="c", issue_id_or_key=" ", summary="x", description="d", labels=[])
        with self.assertRaisesRegex(JiraOAuthError, "Missing issue summary"):
            service.update_issue_fields(access_token="tok", cloud_id="c", issue_id_or_key="MAB-1", summary=" ", description="d", labels=[])
        with self.assertRaisesRegex(JiraOAuthError, "Missing issue id/key"):
            service.add_issue_comment(access_token="tok", cloud_id="c", issue_id_or_key=" ", comment="hi")


class JiraOAuthIssueHelpersTests(unittest.TestCase):
    def test_adf_to_plain_text_and_issue_type_helpers(self) -> None:
        plain = _adf_to_plain_text(
            {
                "type": "doc",
                "content": [
                    {"type": "heading", "content": [{"type": "text", "text": "Head"}]},
                    {"type": "paragraph", "content": [{"type": "text", "text": "Body"}]},
                    {"type": "bulletList", "content": [{"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Item"}]}]}]},
                ],
            }
        )
        self.assertIn("Head", plain)
        self.assertIn("Body", plain)
        self.assertIn("Item", plain)
        self.assertEqual(_adf_to_plain_text("raw"), "raw")
        self.assertEqual(_adf_to_plain_text(123), "")
        self.assertEqual(_adf_to_plain_text(["a", "b"]), "a\nb")

        names = _parse_issue_type_names_from_payload({"projects": [{"issuetypes": [{"name": "Bug"}]}, {"name": "Task"}]})
        self.assertEqual(names, ["Bug", "Task"])

        self.assertEqual(_select_issue_type_name(requested_issue_type="Bug", available_issue_types=["Task", "Bug"]), "Bug")
        self.assertEqual(_select_issue_type_name(requested_issue_type="Defect", available_issue_types=["Task"]), "Task")
        self.assertEqual(_select_issue_type_name(requested_issue_type="Epic", available_issue_types=["Story", "Task"]), "Story")
        self.assertEqual(_select_issue_type_name(requested_issue_type="", available_issue_types=[]), "Task")


if __name__ == "__main__":
    unittest.main()
