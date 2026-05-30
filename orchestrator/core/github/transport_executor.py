from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from orchestrator.api.webhooks.pr_review_comment_service import (
    publish_inline_review_batch,
    upsert_manual_fix_issue_comment_reply,
    upsert_manual_fix_review_thread_reply,
    upsert_sticky_review_comment,
)
from orchestrator.core.communications import (
    GitHubInlineReviewBatchAction,
    GitHubIssueCommentReactionAction,
    GitHubManualFixIssueCommentReplyAction,
    GitHubManualFixReviewThreadReplyAction,
    GitHubPullRequestCheckRunAction,
    GitHubPullRequestMergeAction,
    GitHubPullRequestReactionAction,
    GitHubPullRequestReviewCommentReactionAction,
    GitHubStickyReviewCommentAction,
    TransportAction,
)

logger = logging.getLogger(__name__)


class GitHubTransportExecutor:
    def __init__(  # noqa: ANN001
        self,
        *,
        github_client,
        session: Session,
        logger_override=None,
        raise_on_error: bool = False,
    ) -> None:
        self._github_client = github_client
        self._session = session
        self._logger = logger_override or logger
        self._raise_on_error = bool(raise_on_error)

    def execute(self, *, action: TransportAction) -> None:
        if isinstance(action, GitHubIssueCommentReactionAction):
            self._execute_issue_comment_reaction(action=action)
            return
        if isinstance(action, GitHubPullRequestReviewCommentReactionAction):
            self._execute_pull_request_review_comment_reaction(action=action)
            return
        if isinstance(action, GitHubPullRequestReactionAction):
            self._execute_pull_request_reaction(action=action)
            return
        if isinstance(action, GitHubPullRequestCheckRunAction):
            self._execute_pull_request_check_run(action=action)
            return
        if isinstance(action, GitHubStickyReviewCommentAction):
            self._execute_sticky_review_comment(action=action)
            return
        if isinstance(action, GitHubInlineReviewBatchAction):
            self._execute_inline_review_batch(action=action)
            return
        if isinstance(action, GitHubManualFixReviewThreadReplyAction):
            self._execute_manual_fix_review_thread_reply(action=action)
            return
        if isinstance(action, GitHubManualFixIssueCommentReplyAction):
            self._execute_manual_fix_issue_comment_reply(action=action)
            return
        if isinstance(action, GitHubPullRequestMergeAction):
            self._execute_pull_request_merge(action=action)
            return
        raise RuntimeError(f"Unsupported GitHub transport action: {type(action).__name__}")

    def _execute_issue_comment_reaction(self, *, action: GitHubIssueCommentReactionAction) -> None:
        try:
            self._github_client.add_issue_comment_reaction(
                repo_full_name=action.repo_full_name,
                comment_id=action.comment_id,
                content=action.content,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=issue_comment_reaction repo=%s comment_id=%s error=%s",
                action.repo_full_name,
                action.comment_id,
                exc,
            )
            if self._raise_on_error:
                raise

    def _execute_pull_request_review_comment_reaction(
        self,
        *,
        action: GitHubPullRequestReviewCommentReactionAction,
    ) -> None:
        try:
            self._github_client.add_pull_request_review_comment_reaction(
                repo_full_name=action.repo_full_name,
                comment_id=action.comment_id,
                content=action.content,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=review_comment_reaction repo=%s comment_id=%s error=%s",
                action.repo_full_name,
                action.comment_id,
                exc,
            )
            if self._raise_on_error:
                raise

    def _execute_pull_request_reaction(self, *, action: GitHubPullRequestReactionAction) -> None:
        try:
            self._github_client.sync_pull_request_reaction(
                repo_full_name=action.repo_full_name,
                pr_number=action.pr_number,
                content=action.content,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=pull_request_reaction repo=%s pr_number=%s error=%s",
                action.repo_full_name,
                action.pr_number,
                exc,
            )
            if self._raise_on_error:
                raise

    def _execute_pull_request_check_run(self, *, action: GitHubPullRequestCheckRunAction) -> None:
        try:
            self._github_client.create_check_run(
                repo_full_name=action.repo_full_name,
                head_sha=action.head_sha,
                name=action.name,
                status=action.status,
                conclusion=action.conclusion,
                title=action.title,
                summary=action.summary,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=check_run repo=%s head_sha=%s name=%s error=%s",
                action.repo_full_name,
                action.head_sha,
                action.name,
                exc,
            )
            if self._raise_on_error:
                raise

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
            if self._raise_on_error:
                raise

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
            if self._raise_on_error:
                raise

    def _execute_manual_fix_review_thread_reply(self, *, action: GitHubManualFixReviewThreadReplyAction) -> None:
        try:
            upsert_manual_fix_review_thread_reply(
                github_client=self._github_client,
                repo_full_name=action.repo_full_name,
                pr_number=action.pr_number,
                tenant_id=action.tenant_id,
                project_id=action.project_id,
                triggering_comment_id=action.triggering_comment_id,
                requested_by=action.requested_by,
                triggering_comment_url=action.triggering_comment_url,
                instruction_text=action.instruction_text,
                issue_key=action.issue_key,
                issue_url=action.issue_url,
                enqueued=action.enqueued,
                run_id=action.run_id,
                reason=action.reason,
                status_label=action.status_label,
                pr_url=action.pr_url,
                change_summary=action.change_summary,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=manual_fix_followup repo=%s pr_number=%s error=%s",
                action.repo_full_name,
                action.pr_number,
                exc,
            )
            if self._raise_on_error:
                raise

    def _execute_manual_fix_issue_comment_reply(self, *, action: GitHubManualFixIssueCommentReplyAction) -> None:
        try:
            upsert_manual_fix_issue_comment_reply(
                github_client=self._github_client,
                repo_full_name=action.repo_full_name,
                pr_number=action.pr_number,
                tenant_id=action.tenant_id,
                project_id=action.project_id,
                triggering_comment_id=action.triggering_comment_id,
                requested_by=action.requested_by,
                triggering_comment_url=action.triggering_comment_url,
                instruction_text=action.instruction_text,
                issue_key=action.issue_key,
                issue_url=action.issue_url,
                enqueued=action.enqueued,
                run_id=action.run_id,
                reason=action.reason,
                status_label=action.status_label,
                pr_url=action.pr_url,
                change_summary=action.change_summary,
            )
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(
                "github_transport_action_failed kind=manual_fix_issue_comment_reply repo=%s pr_number=%s error=%s",
                action.repo_full_name,
                action.pr_number,
                exc,
            )
            if self._raise_on_error:
                raise

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
            if self._raise_on_error:
                raise
