from __future__ import annotations

from orchestrator.api.webhooks.pr_remediation_policy import parse_manual_pr_fix_request
from orchestrator.core.communications import (
    GitHubIssueCommentReactionAction,
    GitHubPullRequestReviewCommentReactionAction,
    TransportAction,
)


def plan_manual_fix_reaction_actions(
    *,
    github_event: str,
    payload: dict,
    repo_full_name: str,
) -> tuple[TransportAction, ...]:
    normalized_event = str(github_event or "").strip().lower()
    if normalized_event not in {"issue_comment", "pull_request_review_comment"}:
        return ()
    if parse_manual_pr_fix_request(payload=payload) is None:
        return ()
    comment = payload.get("comment")
    comment_id = comment.get("id") if isinstance(comment, dict) else None
    if not isinstance(comment_id, int) or comment_id <= 0:
        return ()
    if normalized_event == "issue_comment":
        return (
            GitHubIssueCommentReactionAction(
                repo_full_name=repo_full_name,
                comment_id=comment_id,
                content="eyes",
            ),
        )
    return (
        GitHubPullRequestReviewCommentReactionAction(
            repo_full_name=repo_full_name,
            comment_id=comment_id,
            content="eyes",
        ),
    )
