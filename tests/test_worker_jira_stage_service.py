from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.core.worker.jira_stage_service import send_stage_update_to_jira


class WorkerJiraStageServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = MagicMock()
        self.settings = SimpleNamespace()
        self.tenant = SimpleNamespace(
            tenant_id="tenant-a",
            jira_config={"connection_id": "conn-1"},
        )

    def test_skips_when_issue_or_message_missing(self) -> None:
        send_stage_update_to_jira(
            session=self.session,
            tenant=self.tenant,
            issue_key=None,
            stage="run_failed",
            message="x",
            settings=self.settings,
        )
        send_stage_update_to_jira(
            session=self.session,
            tenant=self.tenant,
            issue_key="TP-1",
            stage="run_failed",
            message="  ",
            settings=self.settings,
        )
        self.session.get.assert_not_called()

    def test_skips_for_non_comment_stage(self) -> None:
        send_stage_update_to_jira(
            session=self.session,
            tenant=self.tenant,
            issue_key="TP-1",
            stage="run_started",
            message="started",
            settings=self.settings,
        )
        self.session.get.assert_not_called()

    def test_skips_when_connection_missing(self) -> None:
        tenant = SimpleNamespace(tenant_id="tenant-a", jira_config={"connection_id": ""})
        send_stage_update_to_jira(
            session=self.session,
            tenant=tenant,
            issue_key="TP-1",
            stage="run_failed",
            message="failed",
            settings=self.settings,
        )
        self.session.get.assert_not_called()

    def test_skips_when_connection_not_found(self) -> None:
        self.session.get.return_value = None
        send_stage_update_to_jira(
            session=self.session,
            tenant=self.tenant,
            issue_key="TP-1",
            stage="run_failed",
            message="failed",
            settings=self.settings,
        )
        self.session.get.assert_called_once()

    def test_posts_comment_when_connection_exists(self) -> None:
        connection = SimpleNamespace(cloud_id="cloud-1")
        client = MagicMock()
        self.session.get.return_value = connection
        with (
            patch("orchestrator.core.worker.jira_stage_service.refresh_atlassian_connection_tokens", return_value="token"),
            patch("orchestrator.core.worker.jira_stage_service.atlassian_oauth_client", return_value=client),
        ):
            send_stage_update_to_jira(
                session=self.session,
                tenant=self.tenant,
                issue_key="TP-1",
                stage="run_failed",
                message="failed",
                settings=self.settings,
            )
        client.add_issue_comment.assert_called_once()

    def test_posts_comment_for_review_feedback_stage(self) -> None:
        connection = SimpleNamespace(cloud_id="cloud-1")
        client = MagicMock()
        self.session.get.return_value = connection
        with (
            patch("orchestrator.core.worker.jira_stage_service.refresh_atlassian_connection_tokens", return_value="token"),
            patch("orchestrator.core.worker.jira_stage_service.atlassian_oauth_client", return_value=client),
        ):
            send_stage_update_to_jira(
                session=self.session,
                tenant=self.tenant,
                issue_key="TP-1",
                stage="review_feedback",
                message="review says rewrite",
                settings=self.settings,
            )
        client.add_issue_comment.assert_called_once()

    def test_catches_refresh_or_client_errors(self) -> None:
        from orchestrator.tools.atlassian_oauth import AtlassianOAuthError

        connection = SimpleNamespace(cloud_id="cloud-1")
        self.session.get.return_value = connection
        with patch(
            "orchestrator.core.worker.jira_stage_service.refresh_atlassian_connection_tokens",
            side_effect=AtlassianOAuthError("boom"),
        ):
            send_stage_update_to_jira(
                session=self.session,
                tenant=self.tenant,
                issue_key="TP-1",
                stage="run_failed",
                message="failed",
                settings=self.settings,
            )
