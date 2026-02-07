import unittest
from urllib.parse import parse_qs, urlparse

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


if __name__ == "__main__":
    unittest.main()
