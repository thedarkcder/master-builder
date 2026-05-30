from __future__ import annotations

from dataclasses import dataclass

from orchestrator.api.webhooks.pr_remediation_policy import parse_manual_pr_fix_request
from orchestrator.core.projects.policy import resolve_effective_policy


@dataclass(frozen=True)
class GitHubPolicyState:
    allow_code_reviews: bool
    allow_auto_merge: bool
    allow_pr_remediation: bool
    allow_manual_pr_fix_requests: bool
    manual_fix_requested: bool
    max_pr_auto_remediation_loops: int


@dataclass(frozen=True)
class GitHubTriggerState:
    full_review_trigger: bool
    remediation_trigger: bool
    manual_fix_requested: bool
    ignored_reason: str | None
    sender_login: str | None


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


def classify_github_trigger_state(
    *,
    github_event: str,
    normalized_action: str | None,
    payload: dict,
) -> GitHubTriggerState:
    normalized_event = str(github_event or "").strip().lower()
    normalized_pr_action = str(normalized_action or "").strip().lower()
    return GitHubTriggerState(
        full_review_trigger=(
            normalized_event == "pull_request"
            and normalized_pr_action in {"opened", "reopened", "ready_for_review", "synchronize"}
        ),
        remediation_trigger=_is_remediation_trigger(
            github_event=normalized_event,
            normalized_action=normalized_action,
            payload=payload,
        ),
        manual_fix_requested=(
            normalized_event in {"issue_comment", "pull_request_review_comment"}
            and parse_manual_pr_fix_request(payload=payload) is not None
        ),
        ignored_reason=_resolve_ignored_review_reason(github_event=normalized_event, payload=payload),
        sender_login=_resolve_primary_sender_login(payload=payload),
    )


def empty_github_review_summary(
    *,
    allow_auto_merge: bool,
    allow_pr_remediation: bool,
    allow_manual_pr_fix_requests: bool,
    full_review_trigger: bool,
    ignored_reason: str,
) -> dict[str, object]:
    return {
        "signals": [],
        "review_comments": [],
        "inline_reviews": [],
        "pr_review": {
            "enabled": True,
            "triggered": full_review_trigger,
            "ignored_reason": ignored_reason,
        },
        "auto_merge": {
            "enabled": allow_auto_merge,
            "results": [],
        },
        "pr_remediation": {
            "enabled": allow_pr_remediation,
            "manual_fix_requests_enabled": allow_manual_pr_fix_requests,
        },
        "remediation": [],
        "remediation_comments": [],
    }


def _coerce_positive_int(value: object | None, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _resolve_primary_sender_login(*, payload: dict) -> str | None:
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return None
    login = str(sender.get("login") or "").strip()
    return login or None


def _resolve_primary_sender_type(*, payload: dict) -> str | None:
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return None
    sender_type = str(sender.get("type") or "").strip()
    return sender_type or None


def _is_bot_sender(*, payload: dict) -> bool:
    sender_type = (_resolve_primary_sender_type(payload=payload) or "").lower()
    if sender_type == "bot":
        return True
    login = (_resolve_primary_sender_login(payload=payload) or "").lower()
    return login.endswith("[bot]")


def _is_bot_review_author(*, payload: dict) -> bool:
    for key in ("review", "comment"):
        value = payload.get(key)
        if not isinstance(value, dict):
            continue
        user = value.get("user")
        if not isinstance(user, dict):
            continue
        user_type = str(user.get("type") or "").strip().lower()
        login = str(user.get("login") or "").strip().lower()
        if user_type == "bot" or login.endswith("[bot]"):
            return True
    return False


def _resolve_ignored_review_reason(
    *,
    github_event: str,
    payload: dict,
) -> str | None:
    if github_event in {"pull_request_review", "pull_request_review_comment"} and (
        _is_bot_sender(payload=payload) or _is_bot_review_author(payload=payload)
    ):
        return "bot_authored"
    return None


def _is_remediation_trigger(
    *,
    github_event: str,
    normalized_action: str | None,
    payload: dict,
) -> bool:
    from orchestrator.api.webhooks.pr_remediation_policy import is_remediation_trigger

    return is_remediation_trigger(
        event=github_event,
        action=normalized_action or "",
        payload=payload,
    )
