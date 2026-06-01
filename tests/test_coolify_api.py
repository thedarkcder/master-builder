from __future__ import annotations

import unittest
from urllib.error import URLError
from unittest.mock import patch

from orchestrator.tools.coolify_api import CoolifyApiClient, CoolifyApiConfig, CoolifyApiError


class CoolifyApiClientTests(unittest.TestCase):
    def test_connection_failures_are_reported_as_coolify_api_errors(self) -> None:
        client = CoolifyApiClient(CoolifyApiConfig(base_url="http://coolify.example/api/v1", bearer_token="token"))

        with patch("orchestrator.tools.coolify_api.urlopen", side_effect=URLError("Network is unreachable")):
            with self.assertRaisesRegex(
                CoolifyApiError,
                "Coolify API request failed for POST /applications/private-github-app: Network is unreachable",
            ):
                client.create_private_github_app_application(payload={"name": "preview"})


if __name__ == "__main__":
    unittest.main()
