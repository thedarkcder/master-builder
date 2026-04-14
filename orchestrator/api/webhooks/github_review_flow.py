from __future__ import annotations

from orchestrator.api.webhooks.github_review_planner import GitHubReviewPlan, plan_pull_request_targets


def process_pull_request_targets(**kwargs) -> dict[str, object]:  # noqa: ANN003
    return plan_pull_request_targets(**kwargs).summary


__all__ = ["GitHubReviewPlan", "plan_pull_request_targets", "process_pull_request_targets"]
