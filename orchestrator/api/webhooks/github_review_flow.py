from __future__ import annotations

from orchestrator.api.webhooks.pr_remediation_policy import (
    is_remediation_trigger,
    parse_manual_pr_fix_request,
)

_FULL_REVIEW_EVENTS = {"pull_request", "check_run", "check_suite"}
_BOT_AUTHORED_REVIEW_EVENTS = {"pull_request_review", "pull_request_review_comment"}


def process_pull_request_targets(
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
    send_tenant_discord_message_fn,
    tenant_jira_issue_url_fn,
    evaluate_pr_review_findings_fn,
    upsert_sticky_review_comment_fn,
    publish_inline_review_batch_fn,
    enqueue_pr_remediation_if_needed_fn,
    upsert_sticky_remediation_comment_fn,
    upsert_manual_fix_followup_comment_fn,
    github_api_error_type,
) -> dict[str, object]:
    signals: list[dict[str, object]] = []
    remediation: list[dict[str, object]] = []
    remediation_comments: list[dict[str, object]] = []
    review_comments: list[dict[str, object]] = []
    inline_reviews: list[dict[str, object]] = []
    merge_results: list[dict[str, object]] = []
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
        return _empty_review_results(
            allow_auto_merge=allow_auto_merge,
            allow_pr_remediation=allow_pr_remediation,
            allow_manual_pr_fix_requests=allow_manual_pr_fix_requests,
            full_review_trigger=False,
            ignored_reason=ignored_reason,
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
        return _empty_review_results(
            allow_auto_merge=allow_auto_merge,
            allow_pr_remediation=allow_pr_remediation,
            allow_manual_pr_fix_requests=allow_manual_pr_fix_requests,
            full_review_trigger=False,
            ignored_reason="unsupported_event",
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

            try:
                sticky_result = upsert_sticky_review_comment_fn(
                    session=session,
                    request_id=request_id,
                    github_client=github_client,
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    tenant_id=tenant.tenant_id,
                    project_id=project.project_id,
                    head_sha=str(getattr(pr_details, "head_sha", "") or "").strip() or "unknown",
                    signal=signal,
                    findings_result=findings_result,
                    event=github_event,
                    action=normalized_action,
                    logger=logger,
                )
                review_comments.append(
                    {
                        "pr_number": pr_number,
                        "action": sticky_result.action,
                        "comment_id": coerce_int_or_none(sticky_result.comment_id),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "github_webhook_review_comment_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                    request_id,
                    tenant.tenant_id,
                    pr_number,
                    exc,
                )
                review_comments.append(
                    {
                        "pr_number": pr_number,
                        "action": "failed",
                        "error": str(exc),
                    }
                )

            if pr_details is not None:
                changed_paths = {
                    str(change.filename or "").strip()
                    for change in changed_files
                    if str(change.filename or "").strip()
                }
                try:
                    inline_result = publish_inline_review_batch_fn(
                        session=session,
                        request_id=request_id,
                        github_client=github_client,
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        head_sha=pr_details.head_sha,
                        tenant_id=tenant.tenant_id,
                        project_id=project.project_id,
                        findings=findings_result.findings,
                        changed_paths=changed_paths,
                        logger=logger,
                    )
                    inline_reviews.append(
                        {
                            "pr_number": pr_number,
                            "submitted": inline_result.submitted,
                            "review_id": inline_result.review_id,
                            "inline_count": inline_result.inline_count,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "github_webhook_inline_review_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                        request_id,
                        tenant.tenant_id,
                        pr_number,
                        exc,
                    )
                    inline_reviews.append(
                        {
                            "pr_number": pr_number,
                            "submitted": False,
                            "error": str(exc),
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
                try:
                    merge_result = github_client.merge_pull_request(
                        repo_full_name=repo_full_name,
                        pr_number=pr_number,
                        head_sha=pr_details.head_sha,
                    )
                    merge_results.append(
                        {
                            "pr_number": pr_number,
                            "attempted": True,
                            "merged": merge_result.merged,
                            "sha": merge_result.sha,
                            "message": merge_result.message,
                        }
                    )
                except (github_api_error_type, ValueError) as exc:
                    logger.warning(
                        "github_webhook_auto_merge_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                        request_id,
                        tenant.tenant_id,
                        pr_number,
                        exc,
                    )
                    merge_results.append(
                        {
                            "pr_number": pr_number,
                            "attempted": True,
                            "merged": False,
                            "error": str(exc),
                        }
                    )
            else:
                send_tenant_discord_message_fn(
                    session,
                    tenant_id=tenant.tenant_id,
                    event="pr_review_gate",
                    message=f"PR #{pr_number} is merge-ready. Required checks passed and Codex findings are clear.",
                    issue_key=None,
                    run_id=None,
                    project_id=project.project_id,
                )
        elif full_review_trigger and signal.ready:
            send_tenant_discord_message_fn(
                session,
                tenant_id=tenant.tenant_id,
                event="pr_review_gate",
                message=(
                    signal.message
                    if findings_evaluated
                    else f"PR #{pr_number} review gate passed, but findings evaluation is pending/failed. "
                    "Auto-merge is blocked until findings evaluate successfully."
                ),
                issue_key=None,
                run_id=None,
                project_id=project.project_id,
            )
        try:
            remediation_result = None
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
        except (github_api_error_type, ValueError) as exc:
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
                try:
                    followup_result = upsert_manual_fix_followup_comment_fn(
                        github_client=github_client,
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
                    remediation_comments.append(
                        {
                            "pr_number": pr_number,
                            "action": followup_result.action,
                            "comment_id": coerce_int_or_none(followup_result.comment_id),
                            "kind": "manual_fix_followup",
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "github_webhook_manual_fix_followup_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                        request_id,
                        tenant.tenant_id,
                        pr_number,
                        exc,
                    )
                    remediation_comments.append(
                        {
                            "pr_number": pr_number,
                            "action": "failed",
                            "error": str(exc),
                            "kind": "manual_fix_followup",
                        }
                    )
        try:
            remediation_comment_result = upsert_sticky_remediation_comment_fn(
                github_client=github_client,
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
                action=normalized_action,
            )
            remediation_comments.append(
                {
                    "pr_number": pr_number,
                    "action": remediation_comment_result.action,
                    "comment_id": coerce_int_or_none(remediation_comment_result.comment_id),
                }
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "github_webhook_remediation_comment_failed request_id=%s tenant_id=%s pr_number=%s error=%s",
                request_id,
                tenant.tenant_id,
                pr_number,
                exc,
            )
            remediation_comments.append(
                {
                    "pr_number": pr_number,
                    "action": "failed",
                    "error": str(exc),
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

    return {
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
    }


def valid_pr_details(details: object) -> bool:
    return (
        details is not None
        and hasattr(details, "head_sha")
        and bool(str(getattr(details, "head_sha", "") or "").strip())
    )


def coerce_int_or_none(value: object | None) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_primary_sender_login(*, payload: dict) -> str | None:
    sender = payload.get("sender")
    if not isinstance(sender, dict):
        return None
    login = str(sender.get("login") or "").strip()
    return login or None


def _resolve_ignored_review_reason(*, github_event: str, payload: dict) -> str | None:
    normalized_event = str(github_event or "").strip().lower()
    if normalized_event not in _BOT_AUTHORED_REVIEW_EVENTS:
        return None
    for actor in _iter_review_event_actors(payload=payload):
        actor_type = str(actor.get("type") or "").strip().lower()
        actor_login = str(actor.get("login") or "").strip().lower()
        if actor_type == "bot" or actor_login.endswith("[bot]"):
            return "bot_authored"
    return None


def _iter_review_event_actors(*, payload: dict):
    sender = payload.get("sender")
    if isinstance(sender, dict):
        yield sender
    review = payload.get("review")
    if isinstance(review, dict):
        user = review.get("user")
        if isinstance(user, dict):
            yield user
    comment = payload.get("comment")
    if isinstance(comment, dict):
        user = comment.get("user")
        if isinstance(user, dict):
            yield user


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
