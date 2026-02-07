from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FollowUpDraft:
    summary: str
    description: str
    labels: tuple[str, ...]
    issue_type: str = "Task"
    target_status: str = "Backlog"
    auto_promote: bool = False

    def to_payload(self) -> dict[str, object]:
        return {
            "issue_type": self.issue_type,
            "target_status": self.target_status,
            "auto_promote": self.auto_promote,
            "summary": self.summary,
            "description": self.description,
            "labels": list(self.labels),
        }


def build_backlog_follow_up_draft(
    *,
    title: str,
    why_it_matters: str,
    impact: str,
    suggested_approach: str,
    origin_issue_key: str,
    origin_pr_url: str | None = None,
    additional_labels: tuple[str, ...] = (),
) -> FollowUpDraft:
    clean_title = " ".join(title.strip().split())
    clean_why = " ".join(why_it_matters.strip().split())
    clean_impact = " ".join(impact.strip().split())
    clean_approach = " ".join(suggested_approach.strip().split())
    if not clean_title:
        raise ValueError("Follow-up title is required")
    if not clean_why:
        raise ValueError("Follow-up 'why it matters' is required")
    if not clean_impact:
        raise ValueError("Follow-up impact is required")
    if not clean_approach:
        raise ValueError("Follow-up suggested approach is required")
    if not origin_issue_key.strip():
        raise ValueError("origin_issue_key is required")

    links = [f"- Origin issue: {origin_issue_key.strip()}"]
    if origin_pr_url:
        links.append(f"- Origin PR: {origin_pr_url}")

    description = "\n".join(
        [
            "## Why it matters",
            f"- {clean_why}",
            "",
            "## Impact",
            f"- {clean_impact}",
            "",
            "## Suggested approach",
            f"- {clean_approach}",
            "",
            "## Origin links",
            *links,
            "",
            "## Execution policy",
            "- This follow-up must remain in Backlog until explicitly promoted by a human.",
        ]
    )
    labels = ("follow-up", "backlog-only", *additional_labels)
    return FollowUpDraft(summary=clean_title, description=description, labels=labels)
