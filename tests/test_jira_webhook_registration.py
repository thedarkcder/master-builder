import unittest
from unittest.mock import patch

from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthClientConfig, JiraOAuthError


class JiraWebhookRegistrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = JiraOAuthClient(
            JiraOAuthClientConfig(
                client_id="client-id",
                client_secret="client-secret",
                redirect_uri="https://example.test/callback",
            )
        )

    def test_register_webhook_accepts_created_webhook_id_list(self) -> None:
        with patch.object(self.client, "_request_json", return_value={"createdWebhookId": [1001]}):
            result = self.client.register_webhook(
                access_token="token",
                cloud_id="cloud",
                callback_url="https://example.test/jira/webhook/tenant-a",
                jql_filter='project = "MAB"',
                events=["jira:issue_created"],
            )
        self.assertEqual(result, [1001])

    def test_register_webhook_accepts_single_created_webhook_id(self) -> None:
        with patch.object(self.client, "_request_json", return_value={"createdWebhookId": 1002}):
            result = self.client.register_webhook(
                access_token="token",
                cloud_id="cloud",
                callback_url="https://example.test/jira/webhook/tenant-a",
                jql_filter='project = "MAB"',
                events=["jira:issue_created"],
            )
        self.assertEqual(result, [1002])

    def test_register_webhook_accepts_nested_registration_result(self) -> None:
        payload = {
            "webhookRegistrationResult": [
                {"createdWebhookId": [2001]},
                {"createdWebhookId": "2002"},
            ]
        }
        with patch.object(self.client, "_request_json", return_value=payload):
            result = self.client.register_webhook(
                access_token="token",
                cloud_id="cloud",
                callback_url="https://example.test/jira/webhook/tenant-a",
                jql_filter='project = "MAB"',
                events=["jira:issue_created"],
            )
        self.assertEqual(result, [2001, 2002])

    def test_register_webhook_raises_when_no_ids_returned(self) -> None:
        with patch.object(self.client, "_request_json", return_value={"failedWebhookRegistration": []}):
            with self.assertRaisesRegex(JiraOAuthError, "did not return any webhook IDs"):
                self.client.register_webhook(
                    access_token="token",
                    cloud_id="cloud",
                    callback_url="https://example.test/jira/webhook/tenant-a",
                    jql_filter='project = "MAB"',
                    events=["jira:issue_created"],
                )

    def test_list_webhooks_returns_values_array(self) -> None:
        payload = {
            "values": [
                {"id": 1001, "url": "https://example.test/jira/webhook/a"},
                {"id": "1002", "url": "https://example.test/jira/webhook/b"},
            ]
        }
        with patch.object(self.client, "_request_json", return_value=payload):
            result = self.client.list_webhooks(
                access_token="token",
                cloud_id="cloud",
            )
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], 1001)

    def test_register_webhook_retries_on_transient_gateway_error(self) -> None:
        with patch.object(
            self.client,
            "_request_json",
            side_effect=[
                JiraOAuthError("Jira API request failed (502): Bad Gateway"),
                {"createdWebhookId": 1003},
            ],
        ) as request_mock:
            result = self.client.register_webhook(
                access_token="token",
                cloud_id="cloud",
                callback_url="https://example.test/jira/webhook/tenant-a",
                jql_filter='project = "MAB"',
                events=["jira:issue_created"],
            )

        self.assertEqual(result, [1003])
        self.assertEqual(request_mock.call_count, 2)

    def test_register_webhook_retries_on_transient_gateway_error_colon_format(self) -> None:
        with patch.object(
            self.client,
            "_request_json",
            side_effect=[
                JiraOAuthError("Unable to provision Jira webhook: 502: Bad Gateway"),
                {"createdWebhookId": 1004},
            ],
        ) as request_mock:
            result = self.client.register_webhook(
                access_token="token",
                cloud_id="cloud",
                callback_url="https://example.test/jira/webhook/tenant-a",
                jql_filter='project = "MAB"',
                events=["jira:issue_created"],
            )

        self.assertEqual(result, [1004])
        self.assertEqual(request_mock.call_count, 2)

    def test_register_webhook_does_not_retry_non_transient_error(self) -> None:
        with patch.object(
            self.client,
            "_request_json",
            side_effect=JiraOAuthError("Jira API request failed (400): Invalid payload"),
        ) as request_mock:
            with self.assertRaisesRegex(JiraOAuthError, "Invalid payload"):
                self.client.register_webhook(
                    access_token="token",
                    cloud_id="cloud",
                    callback_url="https://example.test/jira/webhook/tenant-a",
                    jql_filter='project = "MAB"',
                    events=["jira:issue_created"],
                )

        self.assertEqual(request_mock.call_count, 1)


if __name__ == "__main__":
    unittest.main()
