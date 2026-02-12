from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.api.webhooks.github_payload_contracts import (
    extract_installation_id,
    extract_pull_request_targets,
    extract_repository_full_name,
    find_tenant_by_installation_id,
)


class GithubPayloadContractsTests(unittest.TestCase):
    def test_extract_installation_id(self) -> None:
        self.assertEqual(extract_installation_id({"installation": {"id": 123}}), "123")
        self.assertEqual(extract_installation_id({"installation": {"id": " 456 "}}), "456")
        self.assertEqual(extract_installation_id({"installation_id": 789}), "789")
        self.assertEqual(extract_installation_id({"installation_id": " 101 "}), "101")
        self.assertIsNone(extract_installation_id({"installation": {"id": ""}}))
        self.assertIsNone(extract_installation_id({}))

    def test_find_tenant_by_installation_id(self) -> None:
        tenants = [
            SimpleNamespace(github_config={"installation_id": "111"}),
            SimpleNamespace(github_config={"installation_id": "222"}),
        ]
        session = MagicMock()
        session.execute.return_value.scalars.return_value.all.return_value = tenants

        found = find_tenant_by_installation_id(session, "222")
        self.assertIs(found, tenants[1])
        self.assertIsNone(find_tenant_by_installation_id(session, "999"))

    def test_extract_repository_full_name(self) -> None:
        self.assertEqual(
            extract_repository_full_name({"repository": {"full_name": " org/repo "}}),
            "org/repo",
        )
        self.assertIsNone(extract_repository_full_name({"repository": {"full_name": ""}}))
        self.assertIsNone(extract_repository_full_name({"repository": {}}))
        self.assertIsNone(extract_repository_full_name({}))

    def test_extract_pull_request_targets(self) -> None:
        payload = {
            "pull_request": {"number": 12, "body": "summary"},
            "check_suite": {
                "pull_requests": [
                    {"number": 12},
                    {"number": 13},
                    {"number": -1},
                    {},
                ]
            },
            "check_run": {
                "pull_requests": [
                    {"number": 13},
                    {"number": 14},
                    "bad",
                ]
            },
        }

        targets = extract_pull_request_targets(payload)
        self.assertEqual(targets, [(12, True), (13, True), (14, True)])

        no_summary_targets = extract_pull_request_targets({"pull_request": {"number": 99, "body": "  "}})
        self.assertEqual(no_summary_targets, [(99, False)])

        self.assertEqual(extract_pull_request_targets({}), [])


if __name__ == "__main__":
    unittest.main()
