import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from orchestrator.tools.jira_oauth import (
    JiraIssueCreateInput,
    JiraOAuthClient,
    JiraOAuthClientConfig,
    _to_adf_description,
)


class JiraOAuthTests(unittest.TestCase):
    def test_authorize_url_includes_offline_access_scope(self) -> None:
        client = JiraOAuthClient(
            JiraOAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://example.test/callback",
            )
        )

        authorize_url = client.build_authorize_url(state="state-token")
        parsed = urlparse(authorize_url)
        scopes = parse_qs(parsed.query)["scope"][0].split(" ")
        self.assertIn("offline_access", scopes)
        self.assertIn("read:board-scope:jira-software", scopes)
        self.assertIn("read:board-scope.admin:jira-software", scopes)
        self.assertIn("read:jira-software", scopes)
        self.assertIn("read:project:jira", scopes)
        self.assertIn("read:attachment:jira", scopes)
        self.assertIn("write:attachment:jira", scopes)

    def test_search_issues_uses_search_jql_endpoint(self) -> None:
        client = JiraOAuthClient(
            JiraOAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://example.test/callback",
            )
        )

        with patch.object(client, "_get_json", return_value={"issues": []}) as mocked:
            issues = client.search_issues_by_jql(
                access_token="token",
                cloud_id="cloud-id",
                jql='project = "MAB"',
                max_results=25,
            )

        self.assertEqual(issues, [])
        mocked.assert_called_once_with(
            "https://api.atlassian.com/ex/jira/cloud-id/rest/api/3/search/jql?jql=project+%3D+%22MAB%22&maxResults=25&fields=summary%2Cstatus",
            access_token="token",
        )

    def test_upload_issue_attachment_posts_multipart_payload(self) -> None:
        client = JiraOAuthClient(
            JiraOAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://example.test/callback",
            )
        )
        captured: dict[str, object] = {}

        class _FakeResponse:
            headers = {}

            def read(self) -> bytes:
                return b'[{"id":"1001","filename":"screen.png"}]'

            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        def _fake_urlopen(request, timeout=30):  # noqa: ANN001, ARG001
            captured["url"] = request.full_url
            captured["method"] = request.get_method()
            captured["headers"] = dict(request.header_items())
            captured["payload"] = request.data
            return _FakeResponse()

        with patch("orchestrator.tools.jira_oauth.urlopen", side_effect=_fake_urlopen):
            result = client.upload_issue_attachment(
                access_token="token",
                cloud_id="cloud-id",
                issue_id_or_key="MAB-1",
                filename="screen.png",
                content=b"binary-data",
                content_type="image/png",
            )

        self.assertEqual(captured["method"], "POST")
        self.assertIn("/issue/MAB-1/attachments", str(captured["url"]))
        normalized_headers = {str(key).lower(): str(value) for key, value in dict(captured["headers"]).items()}
        self.assertIn("multipart/form-data; boundary=", normalized_headers.get("content-type", ""))
        self.assertEqual(normalized_headers.get("x-atlassian-token"), "no-check")
        self.assertIn(b"filename=\"screen.png\"", captured["payload"])
        self.assertIn(b"binary-data", captured["payload"])
        self.assertEqual(result[0]["id"], "1001")

    def test_create_issues_bulk_uses_valid_project_issue_type_when_requested_type_missing(self) -> None:
        client = JiraOAuthClient(
            JiraOAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://example.test/callback",
            )
        )
        captured_payload: dict[str, object] = {}

        def _fake_request_json(**kwargs: object) -> dict:
            payload = kwargs.get("payload")
            if isinstance(payload, dict):
                captured_payload.update(payload)
            return {"issues": [{"key": "MAB-1", "id": "1001"}], "errors": []}

        with (
            patch.object(client, "_get_json", return_value={"values": [{"name": "Story"}, {"name": "Bug"}]}),
            patch.object(client, "_request_json", side_effect=_fake_request_json),
        ):
            result = client.create_issues_bulk(
                access_token="token",
                cloud_id="cloud-id",
                project_key="MAB",
                issues=[
                    JiraIssueCreateInput(
                        summary="Seed issue",
                        description="Description",
                        labels=["discord-seeded"],
                        issue_type="Task",
                    )
                ],
            )

        self.assertEqual([created.key for created in result.created], ["MAB-1"])
        issue_updates = captured_payload.get("issueUpdates")
        self.assertIsInstance(issue_updates, list)
        first_issue = issue_updates[0]
        self.assertEqual(first_issue["fields"]["issuetype"]["name"], "Story")

    def test_create_issues_bulk_surfaces_field_level_errors(self) -> None:
        client = JiraOAuthClient(
            JiraOAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://example.test/callback",
            )
        )
        with (
            patch.object(client, "_get_json", return_value={"values": [{"name": "Task"}]}),
            patch.object(
                client,
                "_request_json",
                return_value={
                    "issues": [],
                    "errors": [
                        {
                            "failedElementNumber": 0,
                            "elementErrors": {
                                "errorMessages": [],
                                "errors": {"issuetype": "Specify a valid issue type"},
                            },
                        }
                    ],
                },
            ),
        ):
            result = client.create_issues_bulk(
                access_token="token",
                cloud_id="cloud-id",
                project_key="MAB",
                issues=[
                    JiraIssueCreateInput(
                        summary="Seed issue",
                        description="Description",
                        labels=["discord-seeded"],
                        issue_type="Task",
                    )
                ],
            )
        self.assertEqual(result.created, [])
        self.assertEqual(result.errors, ["Item 0: issuetype: Specify a valid issue type"])

    def test_to_adf_description_returns_adf_doc_unchanged(self) -> None:
        adf_doc = {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 3},
                    "content": [{"type": "text", "text": "Objective"}],
                }
            ],
        }
        self.assertEqual(_to_adf_description(adf_doc), adf_doc)

    def test_client_delegates_to_callback_issue_and_webhook_services(self) -> None:
        client = JiraOAuthClient(
            JiraOAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://example.test/callback",
            )
        )

        with (
            patch.object(client._callback_flow, "exchange_code", return_value="tokens") as exchange_mock,
            patch.object(client._callback_flow, "refresh_tokens", return_value="refreshed") as refresh_mock,
            patch.object(client._callback_flow, "list_accessible_resources", return_value=["resource"]) as resources_mock,
            patch.object(client._issue_service, "list_projects", return_value=["project"]) as list_projects_mock,
            patch.object(client._issue_service, "get_issue_detail", return_value="detail") as issue_detail_mock,
            patch.object(client._issue_service, "update_issue_fields") as update_mock,
            patch.object(client._issue_service, "add_issue_labels") as add_labels_mock,
            patch.object(client._issue_service, "add_issue_comment", return_value={"id": "c1"}) as comment_mock,
            patch.object(client._webhook_manager, "register_webhook", return_value=[1]) as register_mock,
            patch.object(client._webhook_manager, "list_webhooks", return_value=[{"id": 1}]) as list_webhooks_mock,
            patch.object(client._webhook_manager, "delete_webhooks") as delete_mock,
        ):
            self.assertEqual(client.exchange_code(code="abc"), "tokens")
            self.assertEqual(client.refresh_tokens(refresh_token="r1"), "refreshed")
            self.assertEqual(client.list_accessible_resources(access_token="tok"), ["resource"])
            self.assertEqual(client.list_projects(access_token="tok", cloud_id="cloud"), ["project"])
            self.assertEqual(client.get_issue_detail(access_token="tok", cloud_id="cloud", issue_id_or_key="MAB-1"), "detail")
            client.update_issue_fields(
                access_token="tok",
                cloud_id="cloud",
                issue_id_or_key="MAB-1",
                summary="Summary",
                description="Desc",
                labels=["a"],
            )
            client.add_issue_labels(
                access_token="tok",
                cloud_id="cloud",
                issue_id_or_key="MAB-1",
                labels=["worker:linux"],
            )
            self.assertEqual(
                client.add_issue_comment(access_token="tok", cloud_id="cloud", issue_id_or_key="MAB-1", comment="hi"),
                {"id": "c1"},
            )
            self.assertEqual(
                client.register_webhook(
                    access_token="tok",
                    cloud_id="cloud",
                    callback_url="https://callback",
                    jql_filter="project = MAB",
                    events=["jira:issue_updated"],
                ),
                [1],
            )
            self.assertEqual(client.list_webhooks(access_token="tok", cloud_id="cloud"), [{"id": 1}])
            client.delete_webhooks(access_token="tok", cloud_id="cloud", webhook_ids=[1])

        exchange_mock.assert_called_once()
        refresh_mock.assert_called_once()
        resources_mock.assert_called_once()
        list_projects_mock.assert_called_once()
        issue_detail_mock.assert_called_once()
        update_mock.assert_called_once()
        add_labels_mock.assert_called_once()
        comment_mock.assert_called_once()
        register_mock.assert_called_once()
        list_webhooks_mock.assert_called_once()
        delete_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
