import unittest
from unittest.mock import patch

from orchestrator.tools.jira_oauth import JiraOAuthClient, JiraOAuthClientConfig, JiraOAuthError
from orchestrator.tools.jira_oauth_webhook_manager import (
    _extract_created_webhook_ids,
    _is_transient_webhook_error,
    _summarize_webhook_registration_failure,
)


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

    def test_list_webhooks_handles_non_dict_payload(self) -> None:
        with patch.object(self.client, "_request_json", return_value=[]):
            result = self.client.list_webhooks(access_token="token", cloud_id="cloud")
        self.assertEqual(result, [])

    def test_delete_webhooks_noop_for_empty_ids(self) -> None:
        with patch.object(self.client, "_request_json") as request_mock:
            self.client.delete_webhooks(access_token="token", cloud_id="cloud", webhook_ids=[])
        request_mock.assert_not_called()

    def test_delete_webhooks_calls_api_when_ids_present(self) -> None:
        with patch.object(self.client, "_request_json", return_value={}) as request_mock:
            self.client.delete_webhooks(access_token="token", cloud_id="cloud", webhook_ids=[1001, 1002])
        request_mock.assert_called_once()

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

    def test_helper_extract_ids_and_summaries(self) -> None:
        ids = _extract_created_webhook_ids(
            {
                "createdWebhookIds": ["1001", 1002],
                "webhookRegistrationResult": [{"createdWebhookId": "1003"}],
            }
        )
        self.assertEqual(ids, [1001, 1002, 1003])

        self.assertTrue(_is_transient_webhook_error(JiraOAuthError("failed 503 upstream")))
        self.assertFalse(_is_transient_webhook_error(JiraOAuthError("failed 400 bad request")))

        self.assertIn(
            "errorMessages",
            _summarize_webhook_registration_failure({"errorMessages": ["bad", "config"]}),
        )
        self.assertIn(
            "errors",
            _summarize_webhook_registration_failure({"errors": {"field": "missing"}}),
        )
        self.assertIn(
            "webhookRegistrationResult errors",
            _summarize_webhook_registration_failure({"webhookRegistrationResult": [{"errors": ["Only one URL"]}]}),
        )
        self.assertIn("response keys", _summarize_webhook_registration_failure({"foo": "bar"}))


if __name__ == "__main__":
    unittest.main()
