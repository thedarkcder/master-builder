from __future__ import annotations

import unittest
from io import BytesIO
from urllib.error import HTTPError

from orchestrator.tools.jira_mcp_adapter import JiraMcpAdapter
from orchestrator.tools.atlassian_oauth_confluence_service import AtlassianOAuthConfluenceService
from orchestrator.tools.atlassian_oauth_callback_flow import AtlassianOAuthCallbackFlow
from orchestrator.tools.atlassian_oauth_http import AtlassianOAuthHttpClient
from orchestrator.tools.atlassian_oauth_issue_service import (
    JiraOAuthIssueService,
    _adf_to_plain_text,
    _parse_issue_type_names_from_payload,
    _select_issue_type_name,
    _to_adf_description,
)
from orchestrator.tools.atlassian_oauth_models import JiraIssueCreateInput, AtlassianOAuthClientConfig, AtlassianOAuthError


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


class AtlassianOAuthCallbackFlowTests(unittest.TestCase):
    def _config(self) -> AtlassianOAuthClientConfig:
        return AtlassianOAuthClientConfig(
            client_id="cid",
            client_secret="csecret",
            redirect_uri="https://example.test/callback",
        )

    def test_build_authorize_url_contains_required_parameters(self) -> None:
        flow = AtlassianOAuthCallbackFlow(config=self._config(), post_json=lambda *_args, **_kwargs: {}, get_json=lambda *_args, **_kwargs: [])
        url = flow.build_authorize_url(state="state-1")
        self.assertIn("audience=api.atlassian.com", url)
        self.assertIn("client_id=cid", url)
        self.assertIn("state=state-1", url)

    def test_exchange_code_and_refresh_parse_tokens(self) -> None:
        def _post_json(_url: str, payload: dict):
            if payload["grant_type"] == "authorization_code":
                return {"access_token": "a1", "refresh_token": "r1", "expires_in": 3600, "scope": "read write"}
            return {"access_token": "a2", "refresh_token": "r2", "expires_in": 7200, "scope": ""}

        flow = AtlassianOAuthCallbackFlow(config=self._config(), post_json=_post_json, get_json=lambda *_args, **_kwargs: [])
        token_set = flow.exchange_code(code="abc")
        self.assertEqual(token_set.access_token, "a1")
        self.assertEqual(token_set.scopes, ["read", "write"])

        refreshed = flow.refresh_tokens(refresh_token="r1")
        self.assertEqual(refreshed.access_token, "a2")
        self.assertEqual(refreshed.scopes, [])

    def test_parse_tokens_missing_fields_raise(self) -> None:
        flow = AtlassianOAuthCallbackFlow(config=self._config(), post_json=lambda *_args, **_kwargs: {"access_token": "a"}, get_json=lambda *_args, **_kwargs: [])
        with self.assertRaisesRegex(AtlassianOAuthError, "refresh_token"):
            flow.exchange_code(code="abc")

    def test_list_accessible_resources_validates_payload(self) -> None:
        flow = AtlassianOAuthCallbackFlow(config=self._config(), post_json=lambda *_args, **_kwargs: {}, get_json=lambda *_args, **_kwargs: {})
        with self.assertRaisesRegex(AtlassianOAuthError, "not a list"):
            flow.list_accessible_resources(access_token="token")

        flow_ok = AtlassianOAuthCallbackFlow(
            config=self._config(),
            post_json=lambda *_args, **_kwargs: {},
            get_json=lambda *_args, **_kwargs: [{"id": "c1", "url": "https://site", "name": ""}, {"id": "bad"}],
        )
        resources = flow_ok.list_accessible_resources(access_token="token")
        self.assertEqual(len(resources), 1)
        self.assertEqual(resources[0].name, "https://site")


class AtlassianOAuthHttpClientTests(unittest.TestCase):
    def test_post_get_and_request_json(self) -> None:
        captured = []

        def _opener(request, timeout=30):  # noqa: ANN001, ARG001
            captured.append((request.get_method(), request.full_url, dict(request.header_items()), request.data))
            return _FakeResponse(b'{"ok":true}')

        client = AtlassianOAuthHttpClient(opener=_opener)
        self.assertEqual(client.post_json(url="https://x", payload={"a": 1}), {"ok": True})
        self.assertEqual(client.get_json(url="https://x", access_token="tok"), {"ok": True})
        self.assertEqual(client.request_json(method="PUT", url="https://x", access_token="tok", payload={"b": 2}), {"ok": True})
        self.assertEqual(client.get_bytes(url="https://x/file.txt", access_token="tok"), b'{"ok":true}')
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

        client = AtlassianOAuthHttpClient(opener=_multipart_opener)
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

        client_error = AtlassianOAuthHttpClient(opener=_error_opener)
        with self.assertRaisesRegex(AtlassianOAuthError, "500"):
            client_error.get_json(url="https://x", access_token="tok")

    def test_empty_body_returns_empty_object(self) -> None:
        client = AtlassianOAuthHttpClient(opener=lambda _request, timeout=30: _FakeResponse(b""))  # noqa: ARG005
        self.assertEqual(client.get_json(url="https://x", access_token="tok"), {})


class JiraOAuthIssueServiceTests(unittest.TestCase):
    def test_issue_service_search_list_detail_and_comment(self) -> None:
        get_calls = []
        request_calls = []

        def _get_json(**kwargs):  # noqa: ANN003
            get_calls.append(kwargs["url"])
            url = kwargs["url"]
            if "project/search" in url:
                return {"values": [{"key": "B", "name": "Beta"}, {"key": "A", "name": "Alpha"}]}
            if url.endswith("/comment?startAt=0&maxResults=100"):
                return {
                    "comments": [
                        {
                            "id": "10001",
                            "author": {"displayName": "Alice"},
                            "updated": "2026-03-10T12:00:00.000+0000",
                            "body": {"type": "doc", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "comment body"}]}]},
                        }
                    ],
                    "maxResults": 100,
                    "total": 1,
                }
            return {
                "key": "MAB-1",
                "fields": {
                    "summary": "Summary",
                    "status": {"name": "Done"},
                    "issuetype": {"name": "Epic"},
                    "description": {
                        "type": "doc",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "hello"}]}],
                    },
                },
            }

        def _request_json(**kwargs):  # noqa: ANN003
            request_calls.append(kwargs)
            if kwargs["method"] == "POST" and kwargs["url"].endswith("/search/jql"):
                return {"issues": [{"key": "MAB-1", "fields": {"summary": "MAB-1", "status": {"name": "Done"}}}]}
            if kwargs["method"] == "POST" and kwargs["url"].endswith("/comment"):
                return {"id": "c1"}
            return {"issues": [{"key": "MAB-1", "id": "1001"}], "errors": []}

        service = JiraOAuthIssueService(get_json=_get_json, request_json=_request_json)
        projects = service.list_projects(access_token="tok", cloud_id="cloud")
        self.assertEqual([project.key for project in projects], ["A", "B"])

        issues = service.search_issues_by_jql(access_token="tok", cloud_id="cloud", jql="project=MAB", max_results=99)
        self.assertEqual(issues[0].summary, "MAB-1")
        self.assertEqual(issues[0].status, "Done")
        self.assertEqual(request_calls[0]["payload"]["maxResults"], 50)

        detail = service.get_issue_detail(access_token="tok", cloud_id="cloud", issue_id_or_key=" MAB-1 ")
        self.assertEqual(detail.summary, "Summary")
        self.assertEqual(detail.description, "hello")
        self.assertEqual(detail.issue_type, "Epic")

        comments = service.list_issue_comments(access_token="tok", cloud_id="cloud", issue_id_or_key="MAB-1")
        self.assertEqual(comments[0].comment_id, "10001")
        self.assertEqual(comments[0].author_display_name, "Alice")
        self.assertEqual(comments[0].body, "comment body")

        comment = service.add_issue_comment(access_token="tok", cloud_id="cloud", issue_id_or_key="MAB-1", comment="hi")
        self.assertEqual(comment["id"], "c1")
        self.assertTrue(request_calls)

    def test_issue_search_page_uses_enhanced_search_jql_next_page_token(self) -> None:
        request_calls: list[dict] = []

        def _request_json(**kwargs):  # noqa: ANN003
            request_calls.append(kwargs)
            return {
                "issues": [{"key": "MAB-51", "fields": {"summary": "Page two", "status": {"name": "Backlog"}}}],
                "nextPageToken": "token-2",
            }

        service = JiraOAuthIssueService(
            get_json=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("search must use POST /search/jql")),
            request_json=_request_json,
        )

        page = service.search_issues_by_jql_page(
            access_token="tok",
            cloud_id="cloud",
            jql="project = MAB ORDER BY created ASC",
            max_results=50,
            next_page_token="token-1",
        )

        self.assertEqual([issue.key for issue in page.issues], ["MAB-51"])
        self.assertEqual(page.next_page_token, "token-2")
        self.assertEqual(len(request_calls), 1)
        self.assertEqual(request_calls[0]["method"], "POST")
        self.assertTrue(request_calls[0]["url"].endswith("/rest/api/3/search/jql"))
        self.assertEqual(
            request_calls[0]["payload"],
            {
                "jql": "project = MAB ORDER BY created ASC",
                "maxResults": 50,
                "nextPageToken": "token-1",
                "fields": ["summary", "status"],
            },
        )

    def test_issue_search_rejects_removed_start_at_pagination(self) -> None:
        service = JiraOAuthIssueService(get_json=lambda **_kwargs: {}, request_json=lambda **_kwargs: {})

        with self.assertRaisesRegex(AtlassianOAuthError, "nextPageToken"):
            service.search_issues_by_jql(
                access_token="tok",
                cloud_id="cloud",
                jql="project = MAB ORDER BY created ASC",
                max_results=50,
                start_at=50,
            )

    def test_issue_service_bulk_create_and_update_validation(self) -> None:
        def _get_json(**kwargs):  # noqa: ANN003
            if "createmeta/" in kwargs["url"]:
                return {"values": [{"name": "Task"}, {"name": "Bug"}]}
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
            ],
        )
        self.assertEqual([item.key for item in result.created], ["MAB-1"])
        self.assertIn("summary: bad", result.errors[0])
        self.assertEqual(captured["payload"]["issueUpdates"][0]["fields"]["issuetype"]["name"], "Task")

        with self.assertRaisesRegex(AtlassianOAuthError, "missing summary"):
            service.create_issues_bulk(
                access_token="tok",
                cloud_id="cloud",
                project_key="MAB",
                issues=[JiraIssueCreateInput(summary=" ", description="x", labels=[], issue_type=None)],
            )

        with self.assertRaisesRegex(AtlassianOAuthError, "Missing issue id/key"):
            service.update_issue_fields(access_token="tok", cloud_id="c", issue_id_or_key=" ", summary="x", description="d", labels=[])
        with self.assertRaisesRegex(AtlassianOAuthError, "Missing issue summary"):
            service.update_issue_fields(access_token="tok", cloud_id="c", issue_id_or_key="MAB-1", summary=" ", description="d", labels=[])
        with self.assertRaisesRegex(AtlassianOAuthError, "Missing issue summary"):
            service.update_issue_summary(access_token="tok", cloud_id="c", issue_id_or_key="MAB-1", summary=" ")
        with self.assertRaisesRegex(AtlassianOAuthError, "Missing issue summary"):
            service.update_issue_summary_and_description(
                access_token="tok",
                cloud_id="c",
                issue_id_or_key="MAB-1",
                summary=" ",
                description="d",
            )
        with self.assertRaisesRegex(AtlassianOAuthError, "Missing issue id/key"):
            service.add_issue_comment(access_token="tok", cloud_id="c", issue_id_or_key=" ", comment="hi")

    def test_issue_service_edge_cases(self) -> None:
        def _get_json(**kwargs):  # noqa: ANN003
            url = kwargs["url"]
            if "project/search" in url:
                return {"values": [{"key": "APP", "name": "App"}, {"key": "MAB", "name": "Master"}]}
            if "/issue/" in url:
                return {"key": "MAB-2", "fields": {"summary": "Summary", "status": {"name": "Backlog"}, "description": None}}
            return {}

        captured: list[dict] = []

        def _request_json(**kwargs):  # noqa: ANN003
            captured.append(kwargs)
            if kwargs["method"] == "POST" and kwargs["url"].endswith("/search/jql"):
                return {
                    "issues": [
                        {"key": "MAB-2", "fields": {"summary": "Summary", "status": {"name": "Backlog"}}},
                    ]
                }
            if kwargs["method"] == "PUT":
                return {}
            if kwargs["method"] == "POST":
                return {"id": "comment-1"}
            return {}

        service = JiraOAuthIssueService(get_json=_get_json, request_json=_request_json)
        projects = service.list_projects(access_token="tok", cloud_id="cloud")
        self.assertEqual([project.key for project in projects], ["APP", "MAB"])
        self.assertEqual(projects[1].name, "Master")

        issues = service.search_issues_by_jql(access_token="tok", cloud_id="cloud", jql="project=MAB", max_results=0)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].status, "Backlog")

        detail = service.get_issue_detail(access_token="tok", cloud_id="cloud", issue_id_or_key="MAB-2")
        self.assertEqual(detail.key, "MAB-2")
        self.assertEqual(detail.summary, "Summary")
        self.assertEqual(detail.description, "")

        service.update_issue_fields(
            access_token="tok",
            cloud_id="cloud",
            issue_id_or_key="MAB-2",
            summary="Updated",
            description={"type": "doc", "content": []},
            labels=["a", ""],
        )
        service.update_issue_summary(
            access_token="tok",
            cloud_id="cloud",
            issue_id_or_key="MAB-2",
            summary="Summary-only update",
        )
        service.add_issue_labels(
            access_token="tok",
            cloud_id="cloud",
            issue_id_or_key="MAB-2",
            labels=["worker:linux"],
        )
        service.replace_issue_labels(
            access_token="tok",
            cloud_id="cloud",
            issue_id_or_key="MAB-2",
            labels=["engineering-child", "sync-blocked"],
        )
        self.assertTrue(any(call["method"] == "PUT" for call in captured))
        self.assertIn(
            {"fields": {"summary": "Summary-only update"}},
            [call.get("payload") for call in captured if call.get("method") == "PUT"],
        )
        self.assertIn(
            {"update": {"labels": [{"add": "worker:linux"}]}},
            [call.get("payload") for call in captured if call.get("method") == "PUT"],
        )

    def test_confluence_service_rejects_malformed_space_and_page_rows(self) -> None:
        service = AtlassianOAuthConfluenceService(
            get_json=lambda **_kwargs: {"results": [{"id": "space-1", "key": "ARCH"}]},
            request_json=lambda **_kwargs: {},
        )
        with self.assertRaisesRegex(AtlassianOAuthError, "missing id, key, or name"):
            service.list_spaces(access_token="tok", cloud_id="cloud")

        service = AtlassianOAuthConfluenceService(
            get_json=lambda **_kwargs: {"results": ["not-an-object"]},
            request_json=lambda **_kwargs: {},
        )
        with self.assertRaisesRegex(AtlassianOAuthError, "space response item 0"):
            service.get_space_by_key(access_token="tok", cloud_id="cloud", space_key="ARCH")
        with self.assertRaisesRegex(AtlassianOAuthError, "page response item 0"):
            service.list_pages(access_token="tok", cloud_id="cloud", site_url="https://example.atlassian.net", space_id="space-1")

    def test_issue_service_upserts_remote_issue_link(self) -> None:
        captured: list[dict] = []

        def _request_json(**kwargs):  # noqa: ANN003
            captured.append(kwargs)
            return {"id": "10000"}

        service = JiraOAuthIssueService(get_json=lambda **_kwargs: {}, request_json=_request_json)
        service.upsert_remote_issue_link(
            access_token="tok",
            cloud_id="cloud",
            issue_id_or_key="MAB-2",
            global_id="system=master-builder&issueKey=MAB-2&kind=architecture_document",
            relationship="Architecture",
            title="Decision Engine v2",
            url="https://docs.example.com/decision-engine-v2",
        )

        assert captured[0]["method"] == "POST"
        assert captured[0]["payload"] == {
            "globalId": "system=master-builder&issueKey=MAB-2&kind=architecture_document",
            "relationship": "Architecture",
            "object": {
                "title": "Decision Engine v2",
                "url": "https://docs.example.com/decision-engine-v2",
            },
        }

    def test_issue_service_remote_issue_link_validation(self) -> None:
        service = JiraOAuthIssueService(get_json=lambda **_kwargs: {}, request_json=lambda **_kwargs: {})

        with self.assertRaisesRegex(AtlassianOAuthError, "Missing issue id/key"):
            service.upsert_remote_issue_link(
                access_token="tok",
                cloud_id="cloud",
                issue_id_or_key=" ",
                global_id="gid",
                relationship="architecture_document",
                title="Title",
                url="https://docs.example.com/x",
            )
        with self.assertRaisesRegex(AtlassianOAuthError, "Missing global id for remote issue link"):
            service.upsert_remote_issue_link(
                access_token="tok",
                cloud_id="cloud",
                issue_id_or_key="MAB-2",
                global_id=" ",
                relationship="architecture_document",
                title="Title",
                url="https://docs.example.com/x",
            )

    def test_issue_service_validation_errors_and_issue_type_discovery_strictness(self) -> None:
        service = JiraOAuthIssueService(get_json=lambda **_kwargs: {}, request_json=lambda **_kwargs: {})
        with self.assertRaisesRegex(AtlassianOAuthError, "missing values list"):
            service.list_projects(access_token="tok", cloud_id="cloud")
        with self.assertRaisesRegex(AtlassianOAuthError, "missing issues list"):
            service.search_issues_by_jql(access_token="tok", cloud_id="cloud", jql="project=MAB")
        with self.assertRaisesRegex(AtlassianOAuthError, "Missing issue id/key"):
            service.get_issue_detail(access_token="tok", cloud_id="cloud", issue_id_or_key=" ")
        with self.assertRaisesRegex(AtlassianOAuthError, "not an object"):
            JiraOAuthIssueService(get_json=lambda **_kwargs: [], request_json=lambda **_kwargs: {}).get_issue_detail(
                access_token="tok", cloud_id="cloud", issue_id_or_key="MAB-1"
            )
        with self.assertRaisesRegex(AtlassianOAuthError, "not an object"):
            JiraOAuthIssueService(get_json=lambda **_kwargs: {}, request_json=lambda **_kwargs: []).add_issue_comment(
                access_token="tok", cloud_id="cloud", issue_id_or_key="MAB-1", comment="hi"
            )

        def _get_json(**kwargs):  # noqa: ANN003
            if "createmeta/" in kwargs["url"]:
                return {"values": [{"name": "Task"}]}
            return {}

        service = JiraOAuthIssueService(get_json=_get_json, request_json=lambda **_kwargs: {})
        names = service._list_project_issue_types_for_create(access_token="tok", cloud_id="cloud", project_key="mab")
        self.assertEqual(names, ["Task"])
        with self.assertRaisesRegex(AtlassianOAuthError, "Missing project key"):
            service._list_project_issue_types_for_create(access_token="tok", cloud_id="cloud", project_key=" ")
        strict_service = JiraOAuthIssueService(
            get_json=lambda **_kwargs: (_ for _ in ()).throw(AtlassianOAuthError("upstream")),
            request_json=lambda **_kwargs: {},
        )
        with self.assertRaisesRegex(AtlassianOAuthError, "upstream"):
            strict_service._list_project_issue_types_for_create(access_token="tok", cloud_id="cloud", project_key="mab")

    def test_issue_service_rejects_malformed_provider_rows(self) -> None:
        def _get_json(**kwargs):  # noqa: ANN003
            url = kwargs["url"]
            if "project/search" in url:
                return {"values": ["skip", {"key": "MAB", "name": "Master"}]}
            return {"key": "MAB-1", "fields": "bad-fields"}

        def _request_json(**kwargs):  # noqa: ANN003
            if kwargs["method"] == "POST" and kwargs["url"].endswith("/search/jql"):
                return {"issues": ["skip", {"key": "MAB-1", "fields": "bad-fields"}]}
            return ["not-an-object"]

        service = JiraOAuthIssueService(get_json=_get_json, request_json=_request_json)
        with self.assertRaisesRegex(AtlassianOAuthError, "Project search response item 0"):
            service.list_projects(access_token="tok", cloud_id="cloud")
        with self.assertRaisesRegex(AtlassianOAuthError, "Issue search response item 0"):
            service.search_issues_by_jql(access_token="tok", cloud_id="cloud", jql="project=MAB")
        with self.assertRaisesRegex(AtlassianOAuthError, "fields object"):
            service.get_issue_detail(access_token="tok", cloud_id="cloud", issue_id_or_key="MAB-1")


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
        self.assertEqual(_adf_to_plain_text({"type": "paragraph", "content": []}), "")
        self.assertEqual(_adf_to_plain_text({"type": "custom", "content": [{"type": "text", "text": "x"}]}), "x")

        names = _parse_issue_type_names_from_payload({"projects": [{"issuetypes": [{"name": "Bug"}]}, {"name": "Task"}]})
        self.assertEqual(names, ["Bug", "Task"])

        names_from_list = _parse_issue_type_names_from_payload(
            [{"name": "Story"}, {"issueTypes": [{"name": "Task"}, "skip"]}, "skip"]
        )
        self.assertEqual(names_from_list, ["Story", "Task"])

        self.assertEqual(_select_issue_type_name(requested_issue_type="Bug", available_issue_types=["Task", "Bug"]), "Bug")
        self.assertEqual(_select_issue_type_name(requested_issue_type="Task", available_issue_types=["Task", "Bug"]), "Task")
        with self.assertRaisesRegex(AtlassianOAuthError, "not available"):
            _select_issue_type_name(requested_issue_type="Defect", available_issue_types=["Task"])
        with self.assertRaisesRegex(AtlassianOAuthError, "missing issue type"):
            _select_issue_type_name(requested_issue_type="", available_issue_types=["Task"])
        with self.assertRaisesRegex(AtlassianOAuthError, "no issue types"):
            _select_issue_type_name(requested_issue_type="Task", available_issue_types=[])

    def test_additional_branch_edges_for_issue_service_helpers(self) -> None:
        # _to_adf_description branches: non-doc dict, None, non-str, and empty text fallback.
        self.assertEqual(
            _to_adf_description({"not": "doc"})["content"][0]["content"][0]["text"],
            "No description provided",
        )
        self.assertEqual(
            _to_adf_description(None)["content"][0]["content"][0]["text"],
            "No description provided",
        )
        self.assertEqual(
            _to_adf_description(123)["content"][0]["content"][0]["text"],
            "123",
        )
        self.assertEqual(
            _to_adf_description(" \n\t ")["content"][0]["content"][0]["text"],
            "No description provided",
        )

        # _adf_to_plain_text branch: content is not a list.
        self.assertEqual(_adf_to_plain_text({"type": "paragraph", "content": "x"}), "")

        # _parse_issue_type_names_from_payload branches: dict payload without list keys and duplicates.
        self.assertEqual(_parse_issue_type_names_from_payload({"x": "y"}), [])
        names = _parse_issue_type_names_from_payload(
            {
                "values": [
                    {"name": "Task", "issueTypes": [{"name": "Task"}, {"name": "Bug"}, {"name": "Bug"}]},
                ]
            }
        )
        self.assertEqual(names, ["Task", "Bug"])
        self.assertEqual(_parse_issue_type_names_from_payload("invalid"), [])  # type: ignore[arg-type]


class JiraOAuthIssueServiceCoverageEdgesTests(unittest.TestCase):
    def test_search_issues_preserves_known_status_name_branch(self) -> None:
        service = JiraOAuthIssueService(
            get_json=lambda **_kwargs: {},
            request_json=lambda **_kwargs: {"issues": [{"key": "MAB-1", "fields": {"summary": "S", "status": {"name": "Done"}}}]},
        )
        issues = service.search_issues_by_jql(access_token="tok", cloud_id="cloud", jql="project=MAB")
        self.assertEqual(issues[0].status, "Done")

    def test_bulk_create_rejects_malformed_created_rows(self) -> None:
        service = JiraOAuthIssueService(
            get_json=lambda **_kwargs: {"issueTypes": [{"name": "Task"}]},
            request_json=lambda **_kwargs: {
                "issues": [
                    {"key": "MAB-1", "id": "1001"},
                    {"key": "", "id": "100"},
                ],
                "errors": [],
            },
        )
        with self.assertRaisesRegex(AtlassianOAuthError, "missing issue key/id"):
            service.create_issues_bulk(
                access_token="tok",
                cloud_id="cloud",
                project_key="MAB",
                issues=[JiraIssueCreateInput(summary="One", description="Desc", labels=["a"], issue_type="Task")],
            )

    def test_bulk_create_error_messages_and_select_issue_type_strictness(self) -> None:
        service = JiraOAuthIssueService(
            get_json=lambda **_kwargs: {"issueTypes": [{"name": "Task"}]},
            request_json=lambda **_kwargs: {
                "issues": [],
                "errors": [
                    {
                        "failedElementNumber": 0,
                        "elementErrors": {"errorMessages": ["bad request", "  "]},
                    }
                ],
            },
        )
        result = service.create_issues_bulk(
            access_token="tok",
            cloud_id="cloud",
            project_key="MAB",
            issues=[JiraIssueCreateInput(summary="One", description="Desc", labels=["a"], issue_type="Task")],
        )
        self.assertEqual(result.created, [])
        self.assertEqual(result.errors, ["Item 0: bad request"])

        with self.assertRaisesRegex(AtlassianOAuthError, "not available"):
            _select_issue_type_name(requested_issue_type="story", available_issue_types=["Chore"])

    def test_create_issue_and_link_support_parent_hierarchy(self) -> None:
        captured: list[dict] = []

        def _request_json(**kwargs):  # noqa: ANN003
            captured.append(kwargs)
            if kwargs["url"].endswith("/issueLink"):
                return {}
            return {"key": "MAB-2", "id": "1002"}

        service = JiraOAuthIssueService(
            get_json=lambda **_kwargs: {"issueTypes": [{"name": "Sub-task"}, {"name": "Task"}]},
            request_json=_request_json,
        )
        created = service.create_issue(
            access_token="tok",
            cloud_id="cloud",
            project_key="MAB",
            issue=JiraIssueCreateInput(
                summary="Child task",
                description="Desc",
                labels=["engineering-child"],
                issue_type="Sub-task",
                parent_issue_key="MAB-1",
            ),
        )
        self.assertEqual(created.key, "MAB-2")
        self.assertEqual(
            captured[0]["payload"]["fields"]["parent"]["key"],
            "MAB-1",
        )

        service.create_issue(
            access_token="tok",
            cloud_id="cloud",
            project_key="MAB",
            issue=JiraIssueCreateInput(
                summary="Child story",
                description="Desc",
                labels=["engineering-child"],
                issue_type="Task",
                parent_issue_key="MAB-1",
            ),
        )
        self.assertEqual(
            captured[1]["payload"]["fields"]["parent"]["key"],
            "MAB-1",
        )

        service.add_issue_link(
            access_token="tok",
            cloud_id="cloud",
            inward_issue_key="MAB-2",
            outward_issue_key="MAB-1",
        )
        self.assertEqual(captured[2]["payload"]["inwardIssue"]["key"], "MAB-2")
        self.assertEqual(captured[2]["payload"]["outwardIssue"]["key"], "MAB-1")

    def test_create_issue_raises_when_subtask_type_missing(self) -> None:
        service = JiraOAuthIssueService(
            get_json=lambda **_kwargs: {"issueTypes": [{"name": "Task"}]},
            request_json=lambda **_kwargs: {},
        )
        with self.assertRaisesRegex(AtlassianOAuthError, "Subtask issue type is not available"):
            service.create_issue(
                access_token="tok",
                cloud_id="cloud",
                project_key="MAB",
                issue=JiraIssueCreateInput(
                    summary="Child task",
                    description="Desc",
                    labels=["engineering-child"],
                    issue_type="Sub-task",
                    parent_issue_key="MAB-1",
                ),
            )


if __name__ == "__main__":
    unittest.main()
