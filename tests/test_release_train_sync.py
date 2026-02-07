from __future__ import annotations

import importlib.util
import sys
import unittest
from unittest import mock
from pathlib import Path
from typing import Any


def _load_module() -> Any:
    module_path = Path(__file__).resolve().parents[1] / "scripts" / "release_train_sync.py"
    spec = importlib.util.spec_from_file_location("release_train_sync", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load release_train_sync module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ReleaseTrainSyncSearchTests(unittest.TestCase):
    def test_parse_args_ready_status_default(self) -> None:
        module = _load_module()
        with mock.patch.object(
            sys,
            "argv",
            [
                "release_train_sync.py",
                "assign",
                "--jira-base-url",
                "https://example.atlassian.net",
                "--jira-email",
                "bot@example.com",
                "--jira-api-token",
                "token",
                "--project-key",
                "MAB",
            ],
        ):
            args = module.parse_args()
        self.assertEqual(args.ready_status, "READY TO RELEASE")

    def test_search_uses_search_jql_endpoint_and_paginates(self) -> None:
        module = _load_module()

        class FakeJiraClient(module.JiraClient):
            def __init__(self, responses: list[dict[str, Any]]) -> None:
                super().__init__(base_url="https://example.atlassian.net", email="e", api_token="t")
                self._responses = responses
                self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

            def _request_json(  # type: ignore[override]
                self,
                *,
                method: str,
                path: str,
                payload: dict[str, Any] | None = None,
                query: dict[str, str] | None = None,
            ) -> dict[str, Any]:
                self.calls.append((method, path, payload))
                return self._responses.pop(0)

        client = FakeJiraClient(
            responses=[
                {
                    "issues": [
                        {"key": "MAB-1", "fields": {"labels": ["a"], "status": {"name": "Ready to Release"}}}
                    ],
                    "nextPageToken": "token-1",
                },
                {
                    "issues": [
                        {"key": "MAB-2", "fields": {"labels": ["b"], "status": {"name": "Ready to Release"}}}
                    ]
                },
            ]
        )

        issues = client.search_issues(jql='project = "MAB"')
        self.assertEqual([issue.key for issue in issues], ["MAB-1", "MAB-2"])
        self.assertEqual(client.calls[0][1], "/rest/api/3/search/jql")
        self.assertEqual(client.calls[1][1], "/rest/api/3/search/jql")
        self.assertNotIn("nextPageToken", client.calls[0][2] or {})
        self.assertEqual((client.calls[1][2] or {}).get("nextPageToken"), "token-1")

    def test_search_raises_on_repeated_page_token(self) -> None:
        module = _load_module()

        class FakeJiraClient(module.JiraClient):
            def __init__(self, responses: list[dict[str, Any]]) -> None:
                super().__init__(base_url="https://example.atlassian.net", email="e", api_token="t")
                self._responses = responses

            def _request_json(  # type: ignore[override]
                self,
                *,
                method: str,
                path: str,
                payload: dict[str, Any] | None = None,
                query: dict[str, str] | None = None,
            ) -> dict[str, Any]:
                return self._responses.pop(0)

        client = FakeJiraClient(
            responses=[
                {
                    "issues": [{"key": "MAB-1", "fields": {"labels": [], "status": {"name": "Ready"}}}],
                    "nextPageToken": "token-1",
                },
                {
                    "issues": [{"key": "MAB-2", "fields": {"labels": [], "status": {"name": "Ready"}}}],
                    "nextPageToken": "token-1",
                },
            ]
        )

        with self.assertRaises(RuntimeError):
            client.search_issues(jql='project = "MAB"')


if __name__ == "__main__":
    unittest.main()
