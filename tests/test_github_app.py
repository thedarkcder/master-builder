from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from orchestrator.tools.github_app import (
    GitHubAppClient,
    GitHubAppConfig,
    PullRequestDetails,
    PullRequestResult,
    WorkflowCheckSuite,
    github_client_from_tenant_config,
)


class _FakeHTTPResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class GitHubAppClientTests(unittest.TestCase):
    def test_installation_token_is_cached_until_expiry(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        requests: list = []
        responses = [
            {
                "token": "inst_token_1",
                "expires_at": "2099-01-01T00:00:00Z",
            }
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            requests.append(request)
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            first = client.get_installation_token()
            second = client.get_installation_token()

        self.assertEqual(first, "inst_token_1")
        self.assertEqual(second, "inst_token_1")
        self.assertEqual(len(requests), 1)
        self.assertIn("/app/installations/999/access_tokens", requests[0].full_url)

    def test_create_pull_request_uses_installation_token_and_payload(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        requests: list = []
        responses = [
            {
                "token": "inst_token_2",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            {
                "number": 42,
                "html_url": "https://github.com/example/repo/pull/42",
            },
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            requests.append(request)
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            result = client.create_pull_request(
                repo_full_name="example/repo",
                title="MAB-8: add github client",
                head_branch="jira/MAB-8-add-github-client",
                base_branch="main",
                body="PR body",
            )

        self.assertEqual(
            result,
            PullRequestResult(number=42, html_url="https://github.com/example/repo/pull/42"),
        )
        self.assertEqual(len(requests), 2)
        self.assertIn("/repos/example/repo/pulls", requests[1].full_url)

        payload = json.loads(requests[1].data.decode("utf-8"))
        self.assertEqual(payload["title"], "MAB-8: add github client")
        self.assertEqual(payload["head"], "jira/MAB-8-add-github-client")
        self.assertEqual(payload["base"], "main")
        self.assertEqual(payload["body"], "PR body")
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer inst_token_2")

    def test_github_client_from_tenant_config_requires_resolved_secrets(self) -> None:
        config = {
            "mode": "github_app",
            "app_id_ref": "TEST_GH_APP_ID",
            "private_key_ref": "TEST_GH_PRIVATE_KEY",
            "installation_id": "101",
        }

        os.environ.pop("TEST_GH_APP_ID", None)
        os.environ.pop("TEST_GH_PRIVATE_KEY", None)
        with self.assertRaises(ValueError):
            github_client_from_tenant_config(config)

        os.environ["TEST_GH_APP_ID"] = "777"
        os.environ["TEST_GH_PRIVATE_KEY"] = "fake-private-key"
        try:
            client = github_client_from_tenant_config(config)
            self.assertIsInstance(client, GitHubAppClient)
        finally:
            os.environ.pop("TEST_GH_APP_ID", None)
            os.environ.pop("TEST_GH_PRIVATE_KEY", None)

    def test_get_pull_request_details_extracts_head_sha(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        responses = [
            {
                "token": "inst_token_3",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            {
                "number": 12,
                "html_url": "https://github.com/example/repo/pull/12",
                "head": {"sha": "abc123sha"},
            },
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            details = client.get_pull_request_details(repo_full_name="example/repo", pr_number=12)

        self.assertEqual(
            details,
            PullRequestDetails(
                number=12,
                html_url="https://github.com/example/repo/pull/12",
                head_sha="abc123sha",
            ),
        )

    def test_list_check_suites_filters_to_github_actions_workflows(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        responses = [
            {
                "token": "inst_token_4",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            {
                "check_suites": [
                    {
                        "name": "CI",
                        "status": "completed",
                        "conclusion": "success",
                        "app": {"slug": "github-actions"},
                    },
                    {
                        "name": "Security",
                        "status": "in_progress",
                        "conclusion": None,
                        "app": {"slug": "github-actions"},
                    },
                    {
                        "name": "third-party",
                        "status": "completed",
                        "conclusion": "failure",
                        "app": {"slug": "some-other-app"},
                    },
                ]
            },
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            suites = client.list_check_suites(repo_full_name="example/repo", ref="abc123")

        self.assertEqual(
            suites,
            [
                WorkflowCheckSuite(name="CI", status="completed", conclusion="success"),
                WorkflowCheckSuite(name="Security", status="in_progress", conclusion=None),
            ],
        )
