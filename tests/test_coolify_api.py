from __future__ import annotations

import unittest
from io import BytesIO
from urllib.error import URLError
from unittest.mock import patch

from orchestrator.tools.coolify_api import CoolifyApiClient, CoolifyApiConfig, CoolifyApiError


class _FakeResponse:
    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return BytesIO(self._body).read()


class CoolifyApiClientTests(unittest.TestCase):
    def test_connection_failures_are_reported_as_coolify_api_errors(self) -> None:
        client = CoolifyApiClient(CoolifyApiConfig(base_url="http://coolify.example/api/v1", bearer_token="token"))

        with patch("orchestrator.tools.coolify_api.urlopen", side_effect=URLError("Network is unreachable")):
            with self.assertRaisesRegex(
                CoolifyApiError,
                "Coolify API request failed for POST /applications/private-github-app: Network is unreachable",
            ):
                client.create_private_github_app_application(payload={"name": "preview"})

    def test_request_timeouts_are_reported_as_coolify_api_errors(self) -> None:
        client = CoolifyApiClient(CoolifyApiConfig(base_url="http://coolify.example/api/v1", bearer_token="token"))

        with patch("orchestrator.tools.coolify_api.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaisesRegex(
                CoolifyApiError,
                "Coolify API request timed out for GET /deployments/deployment-1",
            ):
                client.get_deployment(deployment_uuid="deployment-1")

    def test_list_application_deployments_accepts_deployments_collection_wrapper(self) -> None:
        client = CoolifyApiClient(CoolifyApiConfig(base_url="http://coolify.example/api/v1", bearer_token="token"))

        with patch(
            "orchestrator.tools.coolify_api.urlopen",
            return_value=_FakeResponse('{"deployments":[{"deployment_uuid":"deployment-1"}]}'),
        ):
            deployments = client.list_application_deployments(application_uuid="application-1")

        self.assertEqual(deployments, [{"deployment_uuid": "deployment-1"}])

    def test_list_application_deployments_accepts_data_collection_wrapper(self) -> None:
        client = CoolifyApiClient(CoolifyApiConfig(base_url="http://coolify.example/api/v1", bearer_token="token"))

        with patch(
            "orchestrator.tools.coolify_api.urlopen",
            return_value=_FakeResponse('{"data":[{"deployment_uuid":"deployment-1"}]}'),
        ):
            deployments = client.list_application_deployments(application_uuid="application-1")

        self.assertEqual(deployments, [{"deployment_uuid": "deployment-1"}])

    def test_list_application_deployments_rejects_unknown_response_shape(self) -> None:
        client = CoolifyApiClient(CoolifyApiConfig(base_url="http://coolify.example/api/v1", bearer_token="token"))

        with patch("orchestrator.tools.coolify_api.urlopen", return_value=_FakeResponse('{"items":[]}')):
            with self.assertRaisesRegex(CoolifyApiError, "response was not a list"):
                client.list_application_deployments(application_uuid="application-1")


if __name__ == "__main__":
    unittest.main()
