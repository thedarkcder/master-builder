from __future__ import annotations

from dataclasses import dataclass

from orchestrator.api.webhooks.pr_remediation_policy import (
    is_remediation_trigger,
    parse_manual_pr_fix_request,
)
from orchestrator.core.communications import (
    DiscordTenantNotificationAction,
    GitHubInlineReviewBatchAction,
    GitHubManualFixFollowupCommentAction,
    GitHubPullRequestMergeAction,
    GitHubStickyRemediationCommentAction,
    GitHubStickyReviewCommentAction,
    TransportAction,
)

_FULL_REVIEW_EVENTS = {"pull_request", "check_run", "check_suite"}
_BOT_AUTHORED_REVIEW_EVENTS = {"pull_request_review", "pull_request_review_comment"}


@dataclass(frozen=True)
class GitHubReviewPlan:
    summary: dict[str, object]
    actions: tuple[TransportAction, ...] = ()


def plan_pull_request_targets(
    *,
    request_id: str,
    tenant,
    project,
    repo_full_name: str,
    pr_targets: list[tuple[int, bool]],
    payload: dict,
    github_event: str,
    normalized_action: str | None,
    github_client,
    reviewer_gate,
    allow_auto_merge: bool,
    allow_pr_remediation: bool,
    allow_manual_pr_fix_requests: bool,
    max_pr_auto_remediation_loops: int,
    session,
    settings,  # noqa: ANN001
    logger,
    tenant_jira_issue_url_fn,
    evaluate_pr_review_findings_fn,
    enqueue_pr_remediation_if_needed_fn,
) -> GitHubReviewPlan:
    signals: list[dict[str, object]] = []
    remediation: list[dict[str, object]] = []
    remediation_comments: list[dict[str, object]] = []
    review_comments: list[dict[str, object]] = []
    inline_reviews: list[dict[str, object]] = []
    merge_results: list[dict[str, object]] = []
    planned_actions: list[TransportAction] = []
    manual_fix_request = parse_manual_pr_fix_request(payload=payload)
    manual_fix_requested = manual_fix_request is not None
    full_review_trigger = github_event in _FULL_REVIEW_EVENTS
    remediation_trigger = is_remediation_trigger(
        event=github_event,
        action=normalized_action or "",
        payload=payload,
    )
    ignored_reason = _resolve_ignored_review_reason(
        github_event=github_event,
        payload=payload,
    )

    if ignored_reason is not None:
        logger.info(
            "github_review_trigger_ignored request_id=%s tenant_id=%s project_id=%s repo=%s event=%s action=%s reason=%s sender=%s",
            request_id,
            tenant.tenant_id,
            project.project_id,
            repo_full_name,
            github_event,
            normalized_action or "none",
            ignored_reason,
            _resolve_primary_sender_login(payload=payload) or "unknown",
        )
        return GitHubReviewPlan(
            summary=_empty_review_results(
                allow_auto_merge=allow_auto_merge,
                allow_pr_remediation=allow_pr_remediation,
                allow_manual_pr_fix_requests=allow_manual_pr_fix_requests,
                full_review_trigger=False,
                ignored_reason=ignored_reason,
            )
        )

    if not full_review_trigger and not remediation_trigger and not manual_fix_requested:
        logger.info(
            "github_review_trigger_ignored request_id=%s tenant_id=%s project_id=%s repo=%s event=%s action=%s reason=unsupported_event sender=%s",
            request_id,
            tenant.tenant_id,
            project.project_id,
            repo_full_name,
            github_event,
            normalized_action or "none",
            _resolve_primary_sender_login(payload=payload) or "unknown",
        )
        return GitHubReviewPlan(
            summary=_empty_review_results(
                allow_auto_merge=allow_auto_merge,
                allow_pr_remediation=allow_pr_remediation,
                allow_manual_pr_fix_requests=allow_manual_pr_fix_requests,
                full_review_trigger=False,
                ignored_reason="unsupported_event",
            )
        )

    for pr_number, _review_summary_present in pr_targets:
        signal = type("Signal", (), {"ready": False, "state": "not_triggered", "message": "review_not_triggered"})()
        if full_review_trigger:
            try:
                signal = reviewer_gate.evaluate_pr(
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "github_webhook_review_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                    request_id,
                    tenant.tenant_id,
                    pr_number,
                    exc,
                )
                signals.append(
                    {
                        "pr_number": pr_number,
                        "accepted": False,
                        "error": str(exc),
                    }
                )
                continue

        pr_details = None
        checks = []
        changed_files = []
        findings_result = type("FindingsResult", (), {"findings": (), "state": "review_failed", "summary": signal.message})()
        findings_evaluated = False
        if full_review_trigger:
            try:
                candidate_pr_details = github_client.get_pull_request_details(
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                )
                if valid_pr_details(candidate_pr_details):
                    pr_details = candidate_pr_details
                    checks = github_client.list_check_suites(
                        repo_full_name=repo_full_name,
                        ref=pr_details.head_sha,
                    )
                    changed_files = github_client.list_pull_request_files(
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                    )
                    findings_result = evaluate_pr_review_findings_fn(
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        pr_title=pr_details.title,
                        pr_body=pr_details.body,
                        workflow_checks=checks,
                        changed_files=changed_files,
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                    )
                    findings_evaluated = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "github_webhook_findings_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                    request_id,
                    tenant.tenant_id,
                    pr_number,
                    exc,
                )

            planned_actions.append(
                GitHubStickyReviewCommentAction(
                    request_id=request_id,
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    head_sha=str(getattr(pr_details, "head_sha", "") or "").strip() or "unknown",
                    signal=signal,
                    findings_result=findings_result,
                    event=github_event,
                    action_name=normalized_action,
                )
            )
            review_comments.append(
                {
                    "pr_number": pr_number,
                    "action": "planned",
                    "comment_id": None,
                }
            )

            if pr_details is not None:
                changed_paths = {
                    str(change.filename or "").strip()
                    for change in changed_files
                    if str(change.filename or "").strip()
                }
                planned_actions.append(
                    GitHubInlineReviewBatchAction(
                        request_id=request_id,
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        head_sha=pr_details.head_sha,
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                        findings=tuple(findings_result.findings),
                        changed_paths=changed_paths,
                    )
                )
                inline_reviews.append(
                    {
                        "pr_number": pr_number,
                        "submitted": bool(findings_result.findings),
                        "review_id": None,
                        "inline_count": len(findings_result.findings),
                    }
                )

        green = bool(signal.ready) and findings_evaluated and not findings_result.findings
        if full_review_trigger:
            signals.append(
                {
                    "pr_number": pr_number,
                    "accepted": True,
                    "gate": signal.ready,
                    "status": signal.state,
                    "summary": signal.message,
                    "findings_evaluated": findings_evaluated,
                    "findings_count": len(findings_result.findings),
                    "green": green,
                }
            )

        if full_review_trigger and green and pr_details is not None:
            if allow_auto_merge:
                planned_actions.append(
                    GitHubPullRequestMergeAction(
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        head_sha=pr_details.head_sha,
                    )
                )
                merge_results.append(
                    {
                        "pr_number": pr_number,
                        "attempted": True,
                        "merged": None,
                        "planned": True,
                    }
                )
            else:
                planned_actions.append(
                    DiscordTenantNotificationAction(
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                        event="pr_review_gate",
                        message=f"PR #{pr_number} is merge-ready. Required checks passed and Codex findings are clear.",
                    )
                )
        elif full_review_trigger and signal.ready:
            planned_actions.append(
                DiscordTenantNotificationAction(
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    event="pr_review_gate",
                    message=(
                        signal.message
                        if findings_evaluated
                        else f"PR #{pr_number} review gate passed, but findings evaluation is pending/failed. "
                        "Auto-merge is blocked until findings evaluate successfully."
                    ),
                )
            )

        remediation_result = None
        try:
            should_attempt_remediation = False
            if manual_fix_requested:
                should_attempt_remediation = allow_manual_pr_fix_requests
            elif remediation_trigger and allow_pr_remediation:
                should_attempt_remediation = True
            elif full_review_trigger and not green and allow_pr_remediation:
                should_attempt_remediation = True
            if should_attempt_remediation:
                remediation_result = enqueue_pr_remediation_if_needed_fn(
                    session=session,
                    tenant=tenant,
                    project=project,
                    github_client=github_client,
                    event=github_event,
                    action=normalized_action,
                    payload=payload,
                    pr_number=pr_number,
                    repo_full_name=repo_full_name,
                    settings=settings,
                    max_attempts_per_head=max_pr_auto_remediation_loops,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "github_webhook_remediation_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                request_id,
                tenant.tenant_id,
                pr_number,
                exc,
            )
            remediation.append(
                {
                    "pr_number": pr_number,
                    "enqueued": False,
                    "error": str(exc),
                }
            )
            continue

        if manual_fix_requested and not allow_manual_pr_fix_requests:
            remediation.append(
                {
                    "pr_number": pr_number,
                    "enqueued": False,
                    "reason": "manual_pr_fix_requests_disabled",
                    "run_id": None,
                    "issue_key": None,
                    "issue_url": None,
                    "issue_created": False,
                }
            )
            continue
        if not manual_fix_requested and not green and not allow_pr_remediation:
            remediation.append(
                {
                    "pr_number": pr_number,
                    "enqueued": False,
                    "reason": "pr_remediation_disabled",
                    "run_id": None,
                    "issue_key": None,
                    "issue_url": None,
                    "issue_created": False,
                }
            )
            continue
        if remediation_result is None or not remediation_result.triggered:
            continue

        issue_url = tenant_jira_issue_url_fn(
            session=session,
            tenant=tenant,
            issue_key=remediation_result.issue_key,
        )
        remediation_run_id = (
            str(getattr(remediation_result.run, "run_id", "")).strip() or None
            if remediation_result.run is not None
            else None
        )
        if manual_fix_requested:
            comment = payload.get("comment")
            comment_id = comment.get("id") if isinstance(comment, dict) else None
            comment_url = str(comment.get("html_url") or "").strip() if isinstance(comment, dict) else ""
            comment_user = comment.get("user") if isinstance(comment, dict) else None
            requested_by = (
                str(comment_user.get("login") or "").strip()
                if isinstance(comment_user, dict)
                else None
            )
            run_plan = getattr(remediation_result.run, "plan", None)
            trigger_context = run_plan.get("trigger_context") if isinstance(run_plan, dict) else None
            manual_context = (
                trigger_context.get("manual_fix_request")
                if isinstance(trigger_context, dict)
                else None
            )
            requested_comment = (
                manual_context.get("requested_comment")
                if isinstance(manual_context, dict)
                else None
            )
            requested_comment_url = (
                str(requested_comment.get("url") or "").strip()
                if isinstance(requested_comment, dict)
                else ""
            )
            if isinstance(comment_id, int) and comment_id > 0:
                planned_actions.append(
                    GitHubManualFixFollowupCommentAction(
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                        triggering_comment_id=comment_id,
                        requested_by=requested_by or None,
                        triggering_comment_url=comment_url or None,
                        requested_comment_url=requested_comment_url or comment_url or None,
                        issue_key=remediation_result.issue_key,
                        issue_url=issue_url,
                        enqueued=remediation_result.enqueued,
                        run_id=remediation_run_id,
                        reason=remediation_result.reason,
                    )
                )
                remediation_comments.append(
                    {
                        "pr_number": pr_number,
                        "action": "planned",
                        "comment_id": None,
                        "kind": "manual_fix_followup",
                    }
                )

        planned_actions.append(
            GitHubStickyRemediationCommentAction(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                issue_key=remediation_result.issue_key,
                issue_url=issue_url,
                issue_created=remediation_result.issue_created,
                enqueued=remediation_result.enqueued,
                reason=remediation_result.reason,
                run_id=remediation_run_id,
                head_sha=remediation_result.head_sha,
                event=github_event,
                action_name=normalized_action,
            )
        )
        remediation_comments.append(
            {
                "pr_number": pr_number,
                "action": "planned",
                "comment_id": None,
            }
        )
        remediation.append(
            {
                "pr_number": pr_number,
                "enqueued": remediation_result.enqueued,
                "reason": remediation_result.reason,
                "run_id": remediation_run_id,
                "issue_key": remediation_result.issue_key,
                "issue_url": issue_url,
                "issue_created": remediation_result.issue_created,
            }
        )

    return GitHubReviewPlan(
        summary={
            "signals": signals,
            "review_comments": review_comments,
            "inline_reviews": inline_reviews,
            "pr_review": {
                "enabled": True,
                "triggered": full_review_trigger,
                "ignored_reason": None,
            },
            "auto_merge": {
                "enabled": allow_auto_merge,
                "results": merge_results,
            },
            "pr_remediation": {
                "enabled": allow_pr_remediation,
                "manual_fix_requests_enabled": allow_manual_pr_fix_requests,
            },
            "remediation": remediation,
            "remediation_comments": remediation_comments,
        },
        actions=tuple(planned_actions),
    )


def valid_pr_details(details: object) -> bool:
    return (
        details is not None
        and hasattr(details, "head_sha")
        and bool(str(getattr(details, "head_sha", "") or "").strip())
    )


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
    normalized_event = str(github_event or "").strip().lower()
    if normalized_event in _BOT_AUTHORED_REVIEW_EVENTS and (_is_bot_sender(payload=payload) or _is_bot_review_author(payload=payload)):
        return "bot_authored"
    return None


def _empty_review_results(
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
