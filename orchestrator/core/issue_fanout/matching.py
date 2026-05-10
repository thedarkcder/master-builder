from __future__ import annotations

import re

from orchestrator.tools.atlassian_oauth import JiraIssuePreview


def normalized_summary_key(summary: str) -> str:
    return " ".join(part for part in re.split(r"[^a-z0-9]+", summary.lower()) if part)


def summary_similarity(left: str, right: str) -> float:
    left_tokens = {token for token in re.split(r"[^a-z0-9]+", left.lower()) if token}
    right_tokens = {token for token in re.split(r"[^a-z0-9]+", right.lower()) if token}
    if not left_tokens or not right_tokens:
        return 0.0
    intersection = left_tokens.intersection(right_tokens)
    union = left_tokens.union(right_tokens)
    return len(intersection) / max(1, len(union))


def select_seed_match(
    *,
    existing_issues: list[JiraIssuePreview],
    summary: str,
    requested_issue_key: str | None,
    matched_issue_keys: set[str],
) -> JiraIssuePreview | None:
    if requested_issue_key:
        for issue in existing_issues:
            if issue.key == requested_issue_key and issue.key not in matched_issue_keys:
                return issue

    normalized_target = normalized_summary_key(summary)
    if not normalized_target:
        return None

    for issue in existing_issues:
        if issue.key in matched_issue_keys:
            continue
        if normalized_summary_key(issue.summary) == normalized_target:
            return issue

    best_issue: JiraIssuePreview | None = None
    best_score = 0.0
    for issue in existing_issues:
        if issue.key in matched_issue_keys:
            continue
        score = summary_similarity(summary, issue.summary)
        if score > best_score:
            best_score = score
            best_issue = issue
    if best_issue is not None and best_score >= 0.66:
        return best_issue
    return None
