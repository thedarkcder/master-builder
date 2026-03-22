from __future__ import annotations

from dataclasses import dataclass

from orchestrator.api.webhooks.pr_remediation_policy import parse_manual_pr_fix_request
from orchestrator.core.project_policy import resolve_effective_policy


@dataclass(frozen=True)
class GitHubPolicyState:
    allow_code_reviews: bool
    allow_auto_merge: bool
    allow_pr_remediation: bool
    allow_manual_pr_fix_requests: bool
    manual_fix_requested: bool
    max_pr_auto_remediation_loops: int


def resolve_github_policy_state(
    *,
    tenant_policy: dict,
    project_overrides: dict,
    github_event: str,
    payload: dict,
) -> GitHubPolicyState:
    effective_policy = resolve_effective_policy(
        tenant_policy=tenant_policy or {},
        project_overrides=project_overrides or {},
    )
    allow_code_reviews = bool(effective_policy.get("allow_code_reviews", True))
    allow_auto_merge = bool(effective_policy.get("allow_auto_merge"))
    allow_pr_remediation = allow_code_reviews and bool(effective_policy.get("allow_pr_remediation", True))
    allow_manual_pr_fix_requests = bool(effective_policy.get("allow_manual_pr_fix_requests", True))
    manual_fix_requested = (
        github_event in {"issue_comment", "pull_request_review_comment"}
        and parse_manual_pr_fix_request(payload=payload) is not None
    )
    return GitHubPolicyState(
        allow_code_reviews=allow_code_reviews,
        allow_auto_merge=allow_auto_merge,
        allow_pr_remediation=allow_pr_remediation,
        allow_manual_pr_fix_requests=allow_manual_pr_fix_requests,
        manual_fix_requested=manual_fix_requested,
        max_pr_auto_remediation_loops=_coerce_positive_int(
            effective_policy.get("max_pr_auto_remediation_loops"),
            default=5,
        ),
    )


def _coerce_positive_int(value: object | None, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)
