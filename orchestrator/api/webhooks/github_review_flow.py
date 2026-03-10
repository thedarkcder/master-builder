from __future__ import annotations


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
    github_api_error_type,
) -> dict[str, object]:
    signals: list[dict[str, object]] = []
    remediation: list[dict[str, object]] = []
    remediation_comments: list[dict[str, object]] = []
    review_comments: list[dict[str, object]] = []
    inline_reviews: list[dict[str, object]] = []
    merge_results: list[dict[str, object]] = []
    for pr_number, _review_summary_present in pr_targets:
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
                github_client=github_client,
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                signal=signal,
                findings_result=findings_result,
                event=github_event,
                action=normalized_action,
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
                    github_client=github_client,
                    repo_full_name=repo_full_name,
                    pr_number=pr_number,
                    head_sha=pr_details.head_sha,
                    findings=findings_result.findings,
                    changed_paths=changed_paths,
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

        if green and pr_details is not None:
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
        elif signal.ready:
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
            if not green and allow_pr_remediation:
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
        if not green and not allow_pr_remediation:
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
        if remediation_result is not None and remediation_result.triggered:
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
        "auto_merge": {
            "enabled": allow_auto_merge,
            "results": merge_results,
        },
        "pr_remediation": {
            "enabled": allow_pr_remediation,
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
