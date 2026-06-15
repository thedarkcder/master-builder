from __future__ import annotations

import json
import unittest
from io import BytesIO
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from jwt.exceptions import InvalidKeyError

from orchestrator.tools.github_app import (
    CheckRunResult,
    GitHubBranch,
    GitHubApiError,
    GitHubAppClient,
    GitHubAppConfig,
    CommentReactionResult,
    InstallationRepository,
    PullRequestDetails,
    PullRequestFileChange,
    PullRequestInlineCommentDraft,
    PullRequestReviewComment,
    PullRequestResult,
    PullRequestSummary,
    ReactionSummary,
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
    def test_create_app_jwt_normalizes_escaped_newlines(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----\\n",
        )
        client = GitHubAppClient(config)

        with patch("orchestrator.tools.github_app.jwt.encode", return_value="token") as mock_encode:
            token = client.create_app_jwt()

        self.assertEqual(token, "token")
        self.assertEqual(
            mock_encode.call_args.args[1],
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        )

    def test_create_app_jwt_normalizes_quoted_escaped_newlines(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem='"-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----\\n"',
        )
        client = GitHubAppClient(config)

        with patch("orchestrator.tools.github_app.jwt.encode", return_value="token") as mock_encode:
            token = client.create_app_jwt()

        self.assertEqual(token, "token")
        self.assertEqual(
            mock_encode.call_args.args[1],
            "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        )

    def test_create_app_jwt_rejects_client_or_pat_token_values(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="gho_example_token",
        )
        client = GitHubAppClient(config)

        with self.assertRaisesRegex(ValueError, "received OAuth/PAT token"):
            client.create_app_jwt()

    def test_create_app_jwt_raises_value_error_for_invalid_private_key(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="not-a-private-key",
        )
        client = GitHubAppClient(config)

        with patch("orchestrator.tools.github_app.jwt.encode", side_effect=InvalidKeyError("invalid key")):
            with self.assertRaisesRegex(ValueError, "Invalid GitHub App private key secret"):
                client.create_app_jwt()

    def test_create_app_jwt_invalid_key_error_from_jwt_encode(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        )
        client = GitHubAppClient(config)
        with patch("orchestrator.tools.github_app.jwt.encode", side_effect=InvalidKeyError("bad key")):
            with self.assertRaisesRegex(ValueError, "expected PEM"):
                client.create_app_jwt()

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
                github_repository="https://github.com/example/repo",
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
        self.assertNotIn("draft", payload)
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer inst_token_2")

    def test_create_pull_request_can_create_draft_pr(self) -> None:
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
            client.create_pull_request(
                repo_full_name="example/repo",
                github_repository="https://github.com/example/repo",
                title="MAB-8: add github client",
                head_branch="jira/MAB-8-add-github-client",
                base_branch="main",
                body="PR body",
                draft=True,
            )

        payload = json.loads(requests[1].data.decode("utf-8"))
        self.assertIs(payload["draft"], True)

    def test_mark_pull_request_ready_for_review_uses_graphql_mutation_for_drafts(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)
        draft_details = PullRequestDetails(
            number=42,
            html_url="https://github.com/example/repo/pull/42",
            head_sha="abc123",
            title="MAB-8: add github client",
            state="open",
            node_id="PR_kwDOExample",
            draft=True,
        )
        ready_details = PullRequestDetails(
            number=42,
            html_url="https://github.com/example/repo/pull/42",
            head_sha="abc123",
            title="MAB-8: add github client",
            state="open",
            node_id="PR_kwDOExample",
            draft=False,
        )

        with (
            patch.object(client, "get_installation_token", return_value="token"),
            patch.object(
                client,
                "get_pull_request_details",
                side_effect=[draft_details, ready_details],
            ) as details_mock,
            patch.object(
                client,
                "_request_json",
                return_value={"data": {"markPullRequestReadyForReview": {}}},
            ) as request_mock,
        ):
            result = client.mark_pull_request_ready_for_review(repo_full_name="example/repo", pr_number=42)

        self.assertIs(result, ready_details)
        self.assertEqual(details_mock.call_count, 2)
        self.assertEqual(request_mock.call_args.kwargs["path"], "/graphql")
        self.assertEqual(
            request_mock.call_args.kwargs["payload"]["variables"],
            {"pullRequestId": "PR_kwDOExample"},
        )

    def test_mark_pull_request_ready_for_review_noops_for_non_draft_pr(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)
        details = PullRequestDetails(
            number=42,
            html_url="https://github.com/example/repo/pull/42",
            head_sha="abc123",
            title="MAB-8: add github client",
            state="open",
            draft=False,
        )

        with (
            patch.object(client, "get_pull_request_details", return_value=details),
            patch.object(client, "_request_json") as request_mock,
        ):
            result = client.mark_pull_request_ready_for_review(repo_full_name="example/repo", pr_number=42)

        self.assertIs(result, details)
        request_mock.assert_not_called()

    def test_mark_pull_request_ready_for_review_requires_graphql_node_id(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)
        details = PullRequestDetails(
            number=42,
            html_url="https://github.com/example/repo/pull/42",
            head_sha="abc123",
            title="MAB-8: add github client",
            state="open",
            draft=True,
        )

        with (
            patch.object(client, "get_pull_request_details", return_value=details),
            patch.object(client, "_request_json") as request_mock,
        ):
            with self.assertRaisesRegex(GitHubApiError, "did not include node_id"):
                client.mark_pull_request_ready_for_review(repo_full_name="example/repo", pr_number=42)

        request_mock.assert_not_called()

    def test_mark_pull_request_ready_for_review_raises_on_graphql_errors(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)
        details = PullRequestDetails(
            number=42,
            html_url="https://github.com/example/repo/pull/42",
            head_sha="abc123",
            title="MAB-8: add github client",
            state="open",
            node_id="PR_kwDOExample",
            draft=True,
        )

        with (
            patch.object(client, "get_installation_token", return_value="token"),
            patch.object(client, "get_pull_request_details", return_value=details),
            patch.object(client, "_request_json", return_value={"errors": [{"message": "not authorized"}]}),
        ):
            with self.assertRaisesRegex(GitHubApiError, "ready-for-review mutation failed"):
                client.mark_pull_request_ready_for_review(repo_full_name="example/repo", pr_number=42)

    def test_mark_pull_request_ready_for_review_requires_refreshed_pr_to_leave_draft(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)
        details = PullRequestDetails(
            number=42,
            html_url="https://github.com/example/repo/pull/42",
            head_sha="abc123",
            title="MAB-8: add github client",
            state="open",
            node_id="PR_kwDOExample",
            draft=True,
        )

        with (
            patch.object(client, "get_installation_token", return_value="token"),
            patch.object(client, "get_pull_request_details", side_effect=[details, details]),
            patch.object(client, "_request_json", return_value={"data": {"markPullRequestReadyForReview": {}}}),
        ):
            with self.assertRaisesRegex(GitHubApiError, "PR remained draft"):
                client.mark_pull_request_ready_for_review(repo_full_name="example/repo", pr_number=42)

    def test_create_check_run_uses_installation_token_and_payload(self) -> None:
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
                "id": 71,
                "html_url": "https://github.com/example/repo/runs/71",
            },
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            requests.append(request)
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            result = client.create_check_run(
                repo_full_name="example/repo",
                head_sha="abc123",
                name="MB Staging Merge Check",
                status="completed",
                conclusion="success",
                title="Staging merge check",
                summary="Validated against staging@abcdef0.",
            )

        self.assertEqual(
            result,
            CheckRunResult(check_run_id=71, html_url="https://github.com/example/repo/runs/71"),
        )
        payload = json.loads(requests[1].data.decode("utf-8"))
        self.assertEqual(payload["name"], "MB Staging Merge Check")
        self.assertEqual(payload["head_sha"], "abc123")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["conclusion"], "success")
        self.assertEqual(payload["output"]["title"], "Staging merge check")
        self.assertEqual(payload["output"]["summary"], "Validated against staging@abcdef0.")
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer inst_token_2")

    def test_create_pull_request_rejects_repo_outside_tenant_repository(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        with self.assertRaises(PermissionError):
            client.create_pull_request(
                repo_full_name="example/repo",
                github_repository="https://github.com/example/other-repo",
                title="MAB-11: enforce repo guardrails",
                head_branch="jira/MAB-11-guardrails",
                base_branch="main",
                body="PR body",
            )

    def test_github_client_from_tenant_config_requires_resolved_secrets(self) -> None:
        config = {
            "mode": "github_app",
            "app_id_ref": "TEST_GH_APP_ID",
            "private_key_ref": "TEST_GH_PRIVATE_KEY",
            "installation_id": "101",
        }

        tenant_secret_lookup = MagicMock(return_value=None)
        platform_secret_lookup = MagicMock(return_value=None)

        with self.assertRaises(ValueError):
            github_client_from_tenant_config(config, tenant_secret_lookup=tenant_secret_lookup, platform_secret_lookup=platform_secret_lookup)

        tenant_secret_lookup = MagicMock(return_value="tenant-app-id")
        platform_secret_lookup = MagicMock(return_value="fake-private-key")
        client = github_client_from_tenant_config(config, tenant_secret_lookup=tenant_secret_lookup, platform_secret_lookup=platform_secret_lookup)
        self.assertIsInstance(client, GitHubAppClient)

    def test_github_client_from_tenant_config_resolves_tenant_and_platform_refs(self) -> None:
        tenant_secret_lookup = MagicMock(side_effect=lambda secret_ref: "tenant-app-id" if secret_ref.endswith("GITHUB_APP_ID") else "tenant-private-key")
        platform_secret_lookup = MagicMock(return_value="platform-mismatch")
        config = {
            "mode": "github_app",
            "app_id_ref": "tenant/route25/GITHUB_APP_ID",
            "private_key_ref": "tenant/route25/GITHUB_APP_PRIVATE_KEY",
            "installation_id": "101",
        }
        client = github_client_from_tenant_config(
            config,
            tenant_secret_lookup=tenant_secret_lookup,
            platform_secret_lookup=platform_secret_lookup,
        )
        self.assertEqual(client._config.app_id, "tenant-app-id")
        self.assertEqual(client._config.private_key_pem, "tenant-private-key")
        self.assertEqual(tenant_secret_lookup.call_count, 2)
        self.assertEqual(platform_secret_lookup.call_count, 0)

    def test_github_client_from_tenant_config_uses_platform_resolver_for_unscoped_refs(self) -> None:
        tenant_secret_lookup = MagicMock(return_value=None)
        platform_secret_lookup = MagicMock(side_effect=lambda secret_ref: "platform-app-id" if "APP_ID" in secret_ref else "platform-private-key")
        config = {
            "mode": "github_app",
            "app_id_ref": "GITHUB_APP_ID",
            "private_key_ref": "GITHUB_APP_PRIVATE_KEY",
            "installation_id": "101",
        }
        client = github_client_from_tenant_config(
            config,
            tenant_secret_lookup=tenant_secret_lookup,
            platform_secret_lookup=platform_secret_lookup,
        )
        self.assertEqual(client._config.app_id, "platform-app-id")
        self.assertEqual(client._config.private_key_pem, "platform-private-key")
        self.assertEqual(tenant_secret_lookup.call_count, 0)
        self.assertEqual(platform_secret_lookup.call_count, 2)

    def test_github_client_from_tenant_config_returns_error_if_scoped_lookup_missing(self) -> None:
        tenant_secret_lookup = MagicMock(return_value=None)
        platform_secret_lookup = MagicMock(return_value="platform-private-key")
        config = {
            "mode": "github_app",
            "app_id_ref": "tenant/route25/GITHUB_APP_ID",
            "private_key_ref": "GITHUB_APP_PRIVATE_KEY",
            "installation_id": "101",
        }
        with self.assertRaisesRegex(
            ValueError,
            "Missing GitHub App ID secret for ref 'tenant/route25/GITHUB_APP_ID'",
        ):
            github_client_from_tenant_config(
                config,
                tenant_secret_lookup=tenant_secret_lookup,
                platform_secret_lookup=platform_secret_lookup,
            )

    def test_github_client_from_tenant_config_returns_error_if_platform_lookup_missing(self) -> None:
        tenant_secret_lookup = MagicMock(return_value="tenant-private-key")
        platform_secret_lookup = MagicMock(return_value=None)
        config = {
            "mode": "github_app",
            "app_id_ref": "GITHUB_APP_ID",
            "private_key_ref": "GITHUB_APP_PRIVATE_KEY",
            "installation_id": "101",
        }
        with self.assertRaisesRegex(
            ValueError,
            "Missing GitHub App ID secret for ref 'GITHUB_APP_ID'",
        ):
            github_client_from_tenant_config(
                config,
                tenant_secret_lookup=tenant_secret_lookup,
                platform_secret_lookup=platform_secret_lookup,
            )
        self.assertEqual(tenant_secret_lookup.call_count, 0)
        self.assertEqual(platform_secret_lookup.call_count, 1)

    def test_github_client_from_tenant_config_requires_installation(self) -> None:
        with self.assertRaisesRegex(ValueError, "required config fields"):
            github_client_from_tenant_config(
                {"installation_id": ""},
                tenant_secret_lookup=lambda ref: None,
                platform_secret_lookup=lambda ref: None,
            )

    def test_request_json_http_error_and_empty_body_paths(self) -> None:
        config = GitHubAppConfig(app_id="1", installation_id="2", private_key_pem="pem")
        client = GitHubAppClient(config)
        error = HTTPError(
            url="https://api.github.com",
            code=500,
            msg="boom",
            hdrs=None,
            fp=BytesIO(b'{"message":"failed"}'),
        )
        with patch("orchestrator.tools.github_app.urlopen", side_effect=error):
            with self.assertRaisesRegex(GitHubApiError, "request failed"):
                client._request_json(method="GET", path="/x", bearer_token="t")

        class _EmptyResponse:
            def read(self) -> bytes:
                return b""

            def __enter__(self):  # noqa: ANN204
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        with patch("orchestrator.tools.github_app.urlopen", return_value=_EmptyResponse()):
            result = client._request_json(method="GET", path="/x", bearer_token="t")
        self.assertEqual(result, {})

    def test_get_installation_token_validates_required_fields(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)

        with patch.object(client, "create_app_jwt", return_value="jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            return_value=_FakeHTTPResponse({"expires_at": "2099-01-01T00:00:00Z"}),
        ):
            with self.assertRaisesRegex(GitHubApiError, "include token"):
                client.get_installation_token()

        with patch.object(client, "create_app_jwt", return_value="jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            return_value=_FakeHTTPResponse({"token": "abc"}),
        ):
            with self.assertRaisesRegex(GitHubApiError, "include expires_at"):
                client.get_installation_token()

    def test_create_pull_request_validates_response_shape(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client, "_request_json", return_value={"html_url": "https://example/pull/1"}
        ):
            with self.assertRaisesRegex(GitHubApiError, "numeric PR number"):
                client.create_pull_request(
                    repo_full_name="example/repo",
                    github_repository="https://github.com/example/repo",
                    title="title",
                    head_branch="h",
                    base_branch="b",
                    body="body",
                )
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client, "_request_json", return_value={"number": 1}
        ):
            with self.assertRaisesRegex(GitHubApiError, "html_url"):
                client.create_pull_request(
                    repo_full_name="example/repo",
                    github_repository="https://github.com/example/repo",
                    title="title",
                    head_branch="h",
                    base_branch="b",
                    body="body",
                )

    def test_get_pull_request_details_validates_fields(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client, "_request_json", return_value={"number": "x", "html_url": "https://example"}
        ):
            with self.assertRaisesRegex(GitHubApiError, "numeric PR number"):
                client.get_pull_request_details(repo_full_name="example/repo", pr_number=1)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client, "_request_json", return_value={"number": 1, "head": {"sha": "abc"}}
        ):
            with self.assertRaisesRegex(GitHubApiError, "html_url"):
                client.get_pull_request_details(repo_full_name="example/repo", pr_number=1)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client, "_request_json", return_value={"number": 1, "html_url": "https://example", "head": {}}
        ):
            with self.assertRaisesRegex(GitHubApiError, "head SHA"):
                client.get_pull_request_details(repo_full_name="example/repo", pr_number=1)

    def test_list_check_suites_validates_payload_and_sanitizes_conclusion(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client, "_request_json", return_value={}
        ):
            with self.assertRaisesRegex(GitHubApiError, "check_suites"):
                client.list_check_suites(repo_full_name="example/repo", ref="abc")

        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            return_value={
                "check_suites": [
                    {"name": "CI", "status": "queued", "conclusion": 123, "app": {"slug": "github-actions"}},
                    {"name": "", "status": "queued", "app": {"slug": "github-actions"}},
                ]
            },
        ):
            suites = client.list_check_suites(repo_full_name="example/repo", ref="abc")
        self.assertEqual(suites, [WorkflowCheckSuite(name="CI", status="queued", conclusion=None)])

    def test_list_pull_request_files_validates_response_type_and_patch_type(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client, "_request_json", return_value={}
        ):
            with self.assertRaisesRegex(GitHubApiError, "was not a list"):
                client.list_pull_request_files(repo_full_name="example/repo", pr_number=1)

        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client, "_request_json", return_value=[{"filename": "a.txt", "patch": {"bad": True}}]
        ):
            files = client.list_pull_request_files(repo_full_name="example/repo", pr_number=1)
        self.assertEqual(files, [PullRequestFileChange(filename="a.txt", patch=None)])

    def test_list_installation_repositories_validates_response_and_defaults(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client, "_request_json", return_value={}
        ):
            with self.assertRaisesRegex(GitHubApiError, "include repositories"):
                client.list_installation_repositories()

        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            return_value={
                "repositories": [
                    {
                        "full_name": "example/repo",
                        "html_url": "https://github.com/example/repo",
                        "default_branch": "",
                        "private": "no",
                    }
                ]
            },
        ):
            repos = client.list_installation_repositories()
        self.assertEqual(
            repos,
            [
                InstallationRepository(
                    full_name="example/repo",
                    html_url="https://github.com/example/repo",
                    default_branch="main",
                    private=False,
                )
            ],
        )

    def test_get_repository_default_branch_reads_repository_contract(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with (
            patch.object(client, "get_installation_token", return_value="token"),
            patch.object(client, "_request_json", return_value={"default_branch": "master"}) as request_mock,
        ):
            default_branch = client.get_repository_default_branch(
                repo_full_name="example/repo",
                github_repository="https://github.com/example/repo",
            )

        self.assertEqual(default_branch, "master")
        request_mock.assert_called_once_with(
            method="GET",
            path="/repos/example/repo",
            bearer_token="token",
        )

    def test_get_repository_default_branch_rejects_missing_provider_value(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with (
            patch.object(client, "get_installation_token", return_value="token"),
            patch.object(client, "_request_json", return_value={"default_branch": ""}),
        ):
            with self.assertRaisesRegex(GitHubApiError, "default_branch"):
                client.get_repository_default_branch(
                    repo_full_name="example/repo",
                    github_repository="https://github.com/example/repo",
                )

    def test_list_repository_branches_parses_branch_names(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            return_value=[
                {"name": "main", "protected": True},
                {"name": "develop", "protected": False},
                {"name": "", "protected": True},
            ],
        ):
            branches = client.list_repository_branches(
                repo_full_name="example/repo",
                github_repository="https://github.com/example/repo",
            )

        self.assertEqual(
            branches,
            [
                GitHubBranch(name="develop", protected=False),
                GitHubBranch(name="main", protected=True),
            ],
        )

    def test_list_repository_branches_validates_response_type(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            return_value={},
        ):
            with self.assertRaisesRegex(GitHubApiError, "branch list response was not a list"):
                client.list_repository_branches(
                    repo_full_name="example/repo",
                    github_repository="https://github.com/example/repo",
                )

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
                "base": {"ref": "main"},
                "title": "MAB-12: Update",
                "state": "open",
                "node_id": "PR_kwDOExample",
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
                title="MAB-12: Update",
                state="open",
                node_id="PR_kwDOExample",
                base_ref="main",
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

    def test_list_installation_repositories_parses_response(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        responses = [
            {
                "token": "inst_token_5",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            {
                "repositories": [
                    {
                        "full_name": "example/repo-b",
                        "html_url": "https://github.com/example/repo-b",
                        "default_branch": "develop",
                        "private": True,
                    },
                    {
                        "full_name": "example/repo-a",
                        "html_url": "https://github.com/example/repo-a",
                        "default_branch": "main",
                        "private": False,
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
            repositories = client.list_installation_repositories()

        self.assertEqual(
            repositories,
            [
                InstallationRepository(
                    full_name="example/repo-a",
                    html_url="https://github.com/example/repo-a",
                    default_branch="main",
                    private=False,
                ),
                InstallationRepository(
                    full_name="example/repo-b",
                    html_url="https://github.com/example/repo-b",
                    default_branch="develop",
                    private=True,
                ),
            ],
        )

    def test_list_pull_request_files_parses_patch_payload(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        responses = [
            {
                "token": "inst_token_6",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            [
                {
                    "filename": "src/app.ts",
                    "patch": "+ setTimeout(() => {}, 1000)",
                },
                {
                    "filename": "README.md",
                },
            ],
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            files = client.list_pull_request_files(repo_full_name="example/repo", pr_number=99)

        self.assertEqual(
            files,
            [
                PullRequestFileChange(
                    filename="src/app.ts",
                    patch="+ setTimeout(() => {}, 1000)",
                ),
                PullRequestFileChange(
                    filename="README.md",
                    patch=None,
                ),
            ],
        )

    def test_list_pull_request_files_paginates_until_final_page(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        first_page = [
            {
                "filename": f"src/file-{index}.ts",
                "patch": "+ const x = 1;",
            }
            for index in range(100)
        ]
        second_page = [
            {
                "filename": "README.md",
            }
        ]
        responses = [
            {
                "token": "inst_token_7",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            first_page,
            second_page,
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            files = client.list_pull_request_files(repo_full_name="example/repo", pr_number=100)

        self.assertEqual(len(files), 101)
        self.assertEqual(files[0].filename, "src/file-0.ts")
        self.assertEqual(files[-1].filename, "README.md")

    def test_get_file_text_at_ref_decodes_base64_content(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            return_value={"encoding": "base64", "content": "aGVsbG8gd29ybGQ=\n"},
        ):
            content = client.get_file_text_at_ref(
                repo_full_name="example/repo",
                path="src/app.ts",
                ref="abc123",
            )
        self.assertEqual(content, "hello world")

    def test_get_file_text_at_ref_validates_base64_payload(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            return_value={"encoding": "utf-8", "content": "hello"},
        ):
            with self.assertRaisesRegex(GitHubApiError, "base64 content"):
                client.get_file_text_at_ref(
                    repo_full_name="example/repo",
                    path="src/app.ts",
                    ref="abc123",
                )

    def test_list_open_pull_requests_parses_pr_summary(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        responses = [
            {
                "token": "inst_token_8",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            [
                {
                    "number": 123,
                    "title": "Sync staging before release",
                    "state": "open",
                    "html_url": "https://github.com/example/repo/pull/123",
                    "updated_at": "2026-02-12T17:00:00Z",
                    "head": {"ref": "jira/MAB-118-agent-lifecycle-heartbeat"},
                    "base": {"ref": "staging"},
                }
            ],
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            pull_requests = client.list_open_pull_requests(repo_full_name="example/repo", limit=10)

        self.assertEqual(
            pull_requests,
            [
                PullRequestSummary(
                    number=123,
                    title="Sync staging before release",
                    state="open",
                    html_url="https://github.com/example/repo/pull/123",
                    head_ref="jira/MAB-118-agent-lifecycle-heartbeat",
                    base_ref="staging",
                    updated_at="2026-02-12T17:00:00Z",
                )
            ],
        )

    def test_list_open_pull_requests_rejects_non_list_payload(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        responses = [
            {
                "token": "inst_token_9",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            {"unexpected": True},
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            with self.assertRaisesRegex(GitHubApiError, "response was not a list"):
                client.list_open_pull_requests(repo_full_name="example/repo")

    def test_list_pull_requests_supports_all_states_and_parses_lifecycle_timestamps(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)

        responses = [
            {
                "token": "inst_token_9b",
                "expires_at": "2099-01-01T00:00:00Z",
            },
            [
                {
                    "number": 124,
                    "title": "Release cleanup",
                    "state": "closed",
                    "html_url": "https://github.com/example/repo/pull/124",
                    "created_at": "2026-02-12T09:00:00Z",
                    "updated_at": "2026-02-14T17:00:00Z",
                    "closed_at": "2026-02-14T17:00:00Z",
                    "merged_at": "2026-02-14T16:58:00Z",
                    "head": {"ref": "release/cleanup"},
                    "base": {"ref": "main"},
                }
            ],
        ]

        def fake_urlopen(request, timeout=30):  # noqa: ANN001
            return _FakeHTTPResponse(responses.pop(0))

        with patch.object(client, "create_app_jwt", return_value="app.jwt"), patch(
            "orchestrator.tools.github_app.urlopen",
            side_effect=fake_urlopen,
        ):
            pull_requests = client.list_pull_requests(repo_full_name="example/repo", state="all", limit=10)

        self.assertEqual(
            pull_requests,
            [
                PullRequestSummary(
                    number=124,
                    title="Release cleanup",
                    state="closed",
                    html_url="https://github.com/example/repo/pull/124",
                    head_ref="release/cleanup",
                    base_ref="main",
                    created_at="2026-02-12T09:00:00Z",
                    updated_at="2026-02-14T17:00:00Z",
                    closed_at="2026-02-14T17:00:00Z",
                    merged_at="2026-02-14T16:58:00Z",
                )
            ],
        )

    def test_find_open_pull_request_matches_head_and_base(self) -> None:
        config = GitHubAppConfig(
            app_id="12345",
            installation_id="999",
            private_key_pem="unused",
        )
        client = GitHubAppClient(config)
        with patch.object(
            client,
            "list_open_pull_requests",
            return_value=[
                PullRequestSummary(
                    number=123,
                    title="MAB-123: branch",
                    state="open",
                    html_url="https://github.com/example/repo/pull/123",
                    head_ref="feature/MAB-123",
                    base_ref="main",
                    updated_at="2026-03-15T11:00:00Z",
                ),
                PullRequestSummary(
                    number=124,
                    title="MAB-123: staging",
                    state="open",
                    html_url="https://github.com/example/repo/pull/124",
                    head_ref="feature/MAB-123",
                    base_ref="staging",
                    updated_at="2026-03-15T10:00:00Z",
                ),
            ],
        ):
            pull_request = client.find_open_pull_request(
                repo_full_name="example/repo",
                head_branch="feature/MAB-123",
                base_branch="staging",
            )

        self.assertIsNotNone(pull_request)
        self.assertEqual(pull_request.number, 124)

    def test_create_and_update_issue_comment(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            side_effect=[
                {"id": 1001, "body": "first", "created_at": "2026-03-04T10:00:00Z", "user": {"login": "bot"}},
                {"id": 1001, "body": "second", "created_at": "2026-03-04T10:01:00Z", "user": {"login": "bot"}},
            ],
        ) as request_json:
            created = client.create_pull_request_issue_comment(
                repo_full_name="example/repo",
                pr_number=10,
                body="first",
            )
            updated = client.update_issue_comment(
                repo_full_name="example/repo",
                comment_id=1001,
                body="second",
            )

        self.assertEqual(created.comment_id, 1001)
        self.assertEqual(created.body, "first")
        self.assertEqual(updated.body, "second")
        self.assertEqual(request_json.call_count, 2)

    def test_create_and_update_pull_request_review_comment_reply(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            side_effect=[
                {"id": 2001, "body": "reply", "path": "src/a.py", "line": 10, "user": {"login": "bot"}},
                {"id": 2001, "body": "updated reply", "path": "src/a.py", "line": 10, "user": {"login": "bot"}},
            ],
        ) as request_json:
            created = client.create_pull_request_review_comment_reply(
                repo_full_name="example/repo",
                pr_number=10,
                in_reply_to=1001,
                body="reply",
            )
            updated = client.update_pull_request_review_comment(
                repo_full_name="example/repo",
                comment_id=2001,
                body="updated reply",
            )

        self.assertEqual(created.comment_id, 2001)
        self.assertEqual(created.body, "reply")
        self.assertEqual(updated.body, "updated reply")
        self.assertEqual(request_json.call_count, 2)

    def test_list_pull_request_review_comments_parses_created_at(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            return_value=[
                {
                    "id": 3001,
                    "body": "Consider renaming this",
                    "path": "src/app.py",
                    "line": 42,
                    "state": "commented",
                    "created_at": "2026-03-27T12:30:00Z",
                    "user": {"login": "reviewer"},
                }
            ],
        ):
            comments = client.list_pull_request_review_comments(repo_full_name="example/repo", pr_number=21)

        self.assertEqual(
            comments[0],
            PullRequestReviewComment(
                comment_id=3001,
                body="Consider renaming this",
                path="src/app.py",
                line=42,
                state="commented",
                created_at="2026-03-27T12:30:00Z",
                user_login="reviewer",
            ),
        )

    def test_add_issue_and_review_comment_reactions(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            side_effect=[
                {"id": 901, "content": "eyes"},
                {"id": 902, "content": "eyes"},
            ],
        ) as request_json:
            issue_reaction = client.add_issue_comment_reaction(
                repo_full_name="example/repo",
                comment_id=1001,
            )
            review_reaction = client.add_pull_request_review_comment_reaction(
                repo_full_name="example/repo",
                comment_id=2002,
            )

        self.assertEqual(issue_reaction.reaction_id, 901)
        self.assertEqual(issue_reaction.content, "eyes")
        self.assertEqual(review_reaction.reaction_id, 902)
        self.assertEqual(review_reaction.content, "eyes")
        self.assertEqual(request_json.call_count, 2)

    def test_sync_pull_request_reaction_clears_existing_app_status_reactions_first(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with (
            patch.object(client, "get_app_bot_login", return_value="master-builder[bot]") as get_app_bot_login,
            patch.object(
                client,
                "list_pull_request_reactions",
                return_value=[
                    ReactionSummary(reaction_id=301, content="eyes", user_login="master-builder[bot]"),
                    ReactionSummary(reaction_id=302, content="confused", user_login="master-builder[bot]"),
                    ReactionSummary(reaction_id=303, content="+1", user_login="human-reviewer"),
                ],
            ) as list_reactions,
            patch.object(client, "delete_issue_reaction") as delete_reaction,
            patch.object(
                client,
                "add_pull_request_reaction",
                return_value=CommentReactionResult(reaction_id=401, content="+1"),
            ) as add_reaction,
        ):
            result = client.sync_pull_request_reaction(
                repo_full_name="example/repo",
                pr_number=10,
                content="+1",
            )

        get_app_bot_login.assert_called_once_with()
        list_reactions.assert_called_once_with(repo_full_name="example/repo", pr_number=10)
        self.assertEqual(
            [call.kwargs for call in delete_reaction.call_args_list],
            [
                {"repo_full_name": "example/repo", "reaction_id": 301},
                {"repo_full_name": "example/repo", "reaction_id": 302},
            ],
        )
        add_reaction.assert_called_once_with(
            repo_full_name="example/repo",
            pr_number=10,
            content="+1",
        )
        self.assertEqual(result.reaction_id, 401)
        self.assertEqual(result.content, "+1")

    def test_submit_pull_request_review_and_merge(self) -> None:
        config = GitHubAppConfig(app_id="12345", installation_id="999", private_key_pem="unused")
        client = GitHubAppClient(config)
        with patch.object(client, "get_installation_token", return_value="token"), patch.object(
            client,
            "_request_json",
            side_effect=[
                {"id": 9001, "state": "COMMENTED"},
                {"merged": True, "message": "Pull Request successfully merged", "sha": "abc123"},
            ],
        ) as request_json:
            review = client.submit_pull_request_review(
                repo_full_name="example/repo",
                pr_number=10,
                commit_id="abc123",
                body="Codex inline findings",
                comments=[PullRequestInlineCommentDraft(path="src/main.py", line=42, body="Fix this.")],
            )
            merge_result = client.merge_pull_request(
                repo_full_name="example/repo",
                pr_number=10,
                head_sha="abc123",
            )

        self.assertEqual(review.review_id, 9001)
        self.assertEqual(review.state, "COMMENTED")
        self.assertTrue(merge_result.merged)
        self.assertEqual(merge_result.sha, "abc123")
        self.assertEqual(request_json.call_count, 2)
