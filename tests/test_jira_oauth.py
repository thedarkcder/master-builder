import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthClientConfig


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

    def test_search_issues_uses_search_jql_endpoint(self) -> None:
        client = JiraOAuthClient(
            JiraOAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://example.test/callback",
            )
        )

        with patch.object(client, "_post_json_with_access_token", return_value={"issues": []}) as mocked:
            issues = client.search_issues_by_jql(
                access_token="token",
                cloud_id="cloud-id",
                jql='project = "MAB"',
                max_results=25,
            )

        self.assertEqual(issues, [])
        mocked.assert_called_once_with(
            "https://api.atlassian.com/ex/jira/cloud-id/rest/api/3/search/jql",
            access_token="token",
            payload={
                "jql": 'project = "MAB"',
                "maxResults": 25,
                "fields": ["summary", "status"],
            },
        )


if __name__ == "__main__":
    unittest.main()
