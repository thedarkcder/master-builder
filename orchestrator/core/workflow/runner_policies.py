from __future__ import annotations

import re

from orchestrator.core.followups import build_backlog_follow_up_draft

PLACEHOLDER_PATTERN = re.compile(r"\b(todo|fixme|tbd|placeholder|stub)\b", re.IGNORECASE)
ISSUE_KEY_PATTERN = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")
FILE_PATH_PATTERN = re.compile(r"\b[\w./-]+\.[A-Za-z0-9]+\b")


def evaluate_placeholder_policy(
    *,
    request,
    dev_result,
    test_result,
    review_result,
    pr_url: str,
    attempts: int,
    history: list[dict[str, str]],
    failure_factory,
):  # noqa: ANN001
    evidence_text = "\n".join(
        [
            *dev_result.change_summary,
            *test_result.guidance,
            *(review_result.summary or []),
            test_result.feedback or "",
            review_result.feedback or "",
        ]
    )

    if not PLACEHOLDER_PATTERN.search(evidence_text):
        return None

    detected_issue_keys = {
        key for key in ISSUE_KEY_PATTERN.findall(evidence_text) if key != request.issue_key
    }
    detected_paths = sorted(set(FILE_PATH_PATTERN.findall(evidence_text)))
    path_summary = ", ".join(detected_paths[:8]) if detected_paths else "not specified"

    if detected_issue_keys:
        key_summary = ", ".join(sorted(detected_issue_keys))
        history.append(
            {
                "stage": "review",
                "attempt": str(attempts),
                "event": f"placeholder_detected_tracked:{key_summary}",
            }
        )
        return failure_factory(
            plan=None,
            stage="review",
            message=(
                "Placeholder content detected; tracked follow-up issue(s) present "
                f"({key_summary}). Run must remain blocked until placeholders are removed."
            ),
            attempts=attempts,
            history=history,
            request=request,
            skip_auto_follow_up=True,
        )

    history.append(
        {"stage": "review", "attempt": str(attempts), "event": "placeholder_detected_untracked"}
    )
    draft = build_backlog_follow_up_draft(
        title=f"Follow-up for {request.issue_key}: remove placeholder implementation(s)",
        why_it_matters=(
            "Placeholder/TODO markers were detected in workflow output without tracked follow-up."
        ),
        impact=(
            f"Run {request.run_id} for {request.issue_key} cannot be completed while "
            "placeholder behavior remains."
        ),
        suggested_approach=(
            "Replace placeholder implementations and document exact remaining work by file path. "
            f"Detected paths: {path_summary}."
        ),
        origin_issue_key=request.issue_key,
        origin_pr_url=pr_url,
        additional_labels=("placeholder", "policy"),
    )
    return failure_factory(
        plan=None,
        stage="review",
        message=(
            "Placeholder content detected without tracked follow-up issue. "
            "Created backlog follow-up draft and blocked the run."
        ),
        attempts=attempts,
        history=history,
        request=request,
        follow_up_issue=draft.to_payload(),
    )
