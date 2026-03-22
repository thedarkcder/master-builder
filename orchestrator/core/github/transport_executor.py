from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from orchestrator.api.webhooks.pr_review_comment_service import (
    publish_inline_review_batch,
    upsert_manual_fix_followup_comment,
    upsert_sticky_remediation_comment,
    upsert_sticky_review_comment,
)
from orchestrator.core.communications import (
    GitHubInlineReviewBatchAction,
    GitHubIssueCommentReactionAction,
    GitHubManualFixFollowupCommentAction,
    GitHubPullRequestMergeAction,
    GitHubPullRequestReviewCommentReactionAction,
    GitHubStickyRemediationCommentAction,
    GitHubStickyReviewCommentAction,
    TransportAction,
)

logger = logging.getLogger(__name__)


class GitHubTransportExecutor:
    def __init__(self, *, github_client, session: Session, logger_override=None) -> None:  # noqa: ANN001
        self._github_client = github_client
        self._session = session
        self._logger = logger_override or logger

    def execute(self, *, action: TransportAction) -> None:
        if isinstance(action, GitHubIssueCommentReactionAction):
            self._github_client.add_issue_comment_reaction(
                repo_full_name=action.repo_full_name,
                comment_id=action.comment_id,
                content=action.content,
            )
            return
        if isinstance(action, GitHubPullRequestReviewCommentReactionAction):
            self._github_client.add_pull_request_review_comment_reaction(
                repo_full_name=action.repo_full_name,
                comment_id=action.comment_id,
                content=action.content,
            )
            return
        if isinstance(action, GitHubStickyReviewCommentAction):
            self._execute_sticky_review_comment(action=action)
            return
        if isinstance(action, GitHubInlineReviewBatchAction):
            self._execute_inline_review_batch(action=action)
            return
        if isinstance(action, GitHubStickyRemediationCommentAction):
            self._execute_sticky_remediation_comment(action=action)
            return
        if isinstance(action, GitHubManualFixFollowupCommentAction):
            self._execute_manual_fix_followup_comment(action=action)
            return
        if isinstance(action, GitHubPullRequestMergeAction):
            self._execute_pull_request_merge(action=action)
            return
        raise RuntimeError(f"Unsupported GitHub transport action: {type(action).__name__}")

    def _execute_sticky_review_comment(self, *, action: GitHubStickyReviewCommentAction) -> None:
        try:
            upsert_sticky_review_comment(
                session=self._session,
                request_id=action.request_id,
                github_client=self._github_client,
                repo_full_name=action.repo_full_name,
                pr_number=action.pr_number,
                tenant_id=action.tenant_id,
                project_id=action.project_id,
                head_sha=action.head_sha,
                signal=action.signal,
                findings_result=action.findings_result,
                event=action.event,
                action=action.action_name,
                logger=self._logger,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=sticky_review repo=%s pr_number=%s error=%s",
                action.repo_full_name,
                action.pr_number,
                exc,
            )

    def _execute_inline_review_batch(self, *, action: GitHubInlineReviewBatchAction) -> None:
        try:
            publish_inline_review_batch(
                session=self._session,
                request_id=action.request_id,
                github_client=self._github_client,
                repo_full_name=action.repo_full_name,
                pr_number=action.pr_number,
                head_sha=action.head_sha,
                tenant_id=action.tenant_id,
                project_id=action.project_id,
                findings=action.findings,
                changed_paths=set(action.changed_paths),
                logger=self._logger,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=inline_review repo=%s pr_number=%s error=%s",
                action.repo_full_name,
                action.pr_number,
                exc,
            )

    def _execute_sticky_remediation_comment(self, *, action: GitHubStickyRemediationCommentAction) -> None:
        try:
            upsert_sticky_remediation_comment(
                github_client=self._github_client,
                repo_full_name=action.repo_full_name,
                pr_number=action.pr_number,
                tenant_id=action.tenant_id,
                project_id=action.project_id,
                issue_key=action.issue_key,
                issue_url=action.issue_url,
                issue_created=action.issue_created,
                enqueued=action.enqueued,
                reason=action.reason,
                run_id=action.run_id,
                head_sha=action.head_sha,
                event=action.event,
                action=action.action_name,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=sticky_remediation repo=%s pr_number=%s error=%s",
                action.repo_full_name,
                action.pr_number,
                exc,
            )

    def _execute_manual_fix_followup_comment(self, *, action: GitHubManualFixFollowupCommentAction) -> None:
        try:
            upsert_manual_fix_followup_comment(
                github_client=self._github_client,
                repo_full_name=action.repo_full_name,
                pr_number=action.pr_number,
                tenant_id=action.tenant_id,
                project_id=action.project_id,
                triggering_comment_id=action.triggering_comment_id,
                requested_by=action.requested_by,
                triggering_comment_url=action.triggering_comment_url,
                requested_comment_url=action.requested_comment_url,
                issue_key=action.issue_key,
                issue_url=action.issue_url,
                enqueued=action.enqueued,
                run_id=action.run_id,
                reason=action.reason,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=manual_fix_followup repo=%s pr_number=%s error=%s",
                action.repo_full_name,
                action.pr_number,
                exc,
            )

    def _execute_pull_request_merge(self, *, action: GitHubPullRequestMergeAction) -> None:
        try:
            self._github_client.merge_pull_request(
                repo_full_name=action.repo_full_name,
                pr_number=action.pr_number,
                head_sha=action.head_sha,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=merge repo=%s pr_number=%s error=%s",
                action.repo_full_name,
                action.pr_number,
                exc,
            )
