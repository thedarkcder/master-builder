from __future__ import annotations

from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from orchestrator.api.main import create_app
from tests.production_path_support import (
    clear_runtime_environment,
    configure_runtime_environment,
    load_json_fixture,
    seed_core_runtime_state,
    session_factory_for,
)

pytestmark = pytest.mark.production_path


class _FakeGitHubClient:
    def __init__(self) -> None:
        self.review_comment_reactions: list[dict[str, object]] = []
        self.issue_comment_reactions: list[dict[str, object]] = []
        self.review_thread_replies: list[dict[str, object]] = []
        self.issue_comments: list[dict[str, object]] = []

    def get_pull_request_details(self, *, repo_full_name: str, pr_number: int):
        return SimpleNamespace(
            head_sha="abc123",
            title="GP-123: example",
            body="desc",
            head_ref="feature/GP-123",
            base_ref="main",
            html_url=f"https://github.com/{repo_full_name}/pull/{pr_number}",
        )

    def list_check_suites(self, *, repo_full_name: str, ref: str):  # noqa: ARG002
        return []

    def list_pull_request_files(self, *, repo_full_name: str, pr_number: int):  # noqa: ARG002
        return []

    def add_pull_request_review_comment_reaction(self, *, repo_full_name: str, comment_id: int, content: str) -> None:
        self.review_comment_reactions.append(
            {"repo_full_name": repo_full_name, "comment_id": comment_id, "content": content}
        )

    def add_issue_comment_reaction(self, *, repo_full_name: str, comment_id: int, content: str) -> None:
        self.issue_comment_reactions.append(
            {"repo_full_name": repo_full_name, "comment_id": comment_id, "content": content}
        )

    def list_pull_request_review_comments(self, *, repo_full_name: str, pr_number: int):  # noqa: ARG002
        return []

    def create_pull_request_review_comment_reply(
        self,
        *,
        repo_full_name: str,
        pull_request_number: int | None = None,
        pr_number: int | None = None,
        in_reply_to: int,
        body: str,
    ):
        self.review_thread_replies.append(
            {
                "repo_full_name": repo_full_name,
                "pull_request_number": pull_request_number if pull_request_number is not None else pr_number,
                "in_reply_to": in_reply_to,
                "body": body,
            }
        )
        return SimpleNamespace(comment_id=300 + len(self.review_thread_replies))

    def update_pull_request_review_comment(self, *, repo_full_name: str, comment_id: int, body: str):
        self.review_thread_replies.append(
            {
                "repo_full_name": repo_full_name,
                "comment_id": comment_id,
                "body": body,
                "updated": True,
            }
        )
        return SimpleNamespace(comment_id=comment_id)

    def list_pull_request_issue_comments(self, *, repo_full_name: str, pr_number: int):  # noqa: ARG002
        return []

    def create_pull_request_issue_comment(self, *, repo_full_name: str, pr_number: int, body: str):
        self.issue_comments.append(
            {"repo_full_name": repo_full_name, "pr_number": pr_number, "body": body}
        )
        return SimpleNamespace(comment_id=500 + len(self.issue_comments))

    def update_issue_comment(self, *, repo_full_name: str, comment_id: int, body: str):
        self.issue_comments.append(
            {"repo_full_name": repo_full_name, "comment_id": comment_id, "body": body, "updated": True}
        )
        return SimpleNamespace(comment_id=comment_id)


class GitHubWebhookProductionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url, _ = configure_runtime_environment(
            temp_dir=self.temp_dir,
            database_name="github_webhook_production.db",
        )
        self.session_factory = session_factory_for(self.database_url)
        seed_core_runtime_state(self.session_factory)
        self.client = TestClient(create_app())

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()
        clear_runtime_environment()

    def test_ignored_event_runs_through_real_route(self) -> None:
        response = self.client.post(
            "/github/webhook",
            json=load_json_fixture("github", "webhooks", "issues_opened.json"),
            headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "delivery-1"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["reason"], "ignored_event")
        self.assertTrue(response.json()["accepted"])

    def test_manual_fix_review_comment_executes_real_publication_path(self) -> None:
        fake_client = _FakeGitHubClient()
        remediation_result = SimpleNamespace(
            triggered=True,
            issue_key="GP-900",
            issue_created=False,
            enqueued=True,
            reason=None,
            run=SimpleNamespace(run_id="run-900"),
            head_sha="abc123",
        )
        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch(
                "orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate",
                return_value=SimpleNamespace(
                    evaluate_pr=lambda **kwargs: SimpleNamespace(ready=False, state="pending_checks", message="pending")
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.enqueue_pr_remediation_if_needed",
                return_value=remediation_result,
            ),
            patch(
                "orchestrator.api.webhooks.github_application.tenant_jira_issue_url",
                return_value="https://jira.example.com/browse/GP-900",
            ),
        ):
            response = self.client.post(
                "/github/webhook",
                json=load_json_fixture("github", "webhooks", "pull_request_review_comment_created.json"),
                headers={
                    "X-GitHub-Event": "pull_request_review_comment",
                    "X-GitHub-Delivery": "delivery-2",
                },
            )

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertEqual(body["remediation"][0]["issue_key"], "GP-900")
        self.assertEqual(fake_client.review_comment_reactions[0]["comment_id"], 901)
        self.assertEqual(fake_client.review_comment_reactions[0]["content"], "eyes")
        self.assertTrue(fake_client.review_thread_replies)
        reply_bodies = [str(reply["body"]) for reply in fake_client.review_thread_replies]
        self.assertTrue(any("Codex Manual Fix" in body for body in reply_bodies))
        self.assertTrue(any("Codex PR Remediation" in body for body in reply_bodies))
