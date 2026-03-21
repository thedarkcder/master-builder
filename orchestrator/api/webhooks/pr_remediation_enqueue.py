from __future__ import annotations

from orchestrator.core.decision_engine import resolve_enqueue_precheck_outcome
from orchestrator.core.runs import RUN_DEDUPE_SCOPE_PR_REMEDIATION, enqueue_run


def enqueue_pr_remediation_run(
    *,
    session,
    tenant,
    project,
    issue_key: str,
    issue_created: bool,
    pr_number: int,
    details,
    normalized_event: str,
    normalized_action: str,
    checks,
    reviews,
    review_comments,
    issue_comments,
    manual_fix_request: dict[str, object] | None = None,
    max_concurrent_runs: int | None = None,
):
    manual_requested_comment = (
        dict(manual_fix_request.get("requested_comment"))
        if isinstance(manual_fix_request, dict) and isinstance(manual_fix_request.get("requested_comment"), dict)
        else None
    )
    manual_requested_by = (
        str(manual_fix_request.get("requested_by") or "").strip()
        if isinstance(manual_fix_request, dict)
        else ""
    )
    manual_context_lines: list[str] = []
    if manual_requested_comment is not None:
        manual_context_lines.append("Manual request: yes")
        if manual_requested_by:
            manual_context_lines.append(f"Requested by: {manual_requested_by}")
        comment_url = str(manual_requested_comment.get("url") or "").strip()
        if comment_url:
            manual_context_lines.append(f"Requested comment: {comment_url}")
        comment_body = str(manual_requested_comment.get("body") or "").strip()
        if comment_body:
            manual_context_lines.append(f"Requested comment body: {comment_body}")
    manual_context_suffix = ""
    if manual_context_lines:
        manual_context_suffix = "\n" + "\n".join(manual_context_lines)

    trigger_context = {
        "source": "github_pr_review_feedback",
        "event": normalized_event,
        "action": normalized_action,
        "pr_number": pr_number,
        "pr_url": details.html_url,
        "head_sha": details.head_sha,
        "head_ref": details.head_ref or "",
        "base_ref": details.base_ref or "",
        "issue_key": issue_key,
        "issue_created": issue_created,
        "failing_checks": [
            {"name": check.name, "status": check.status, "conclusion": check.conclusion}
            for check in checks
            if check.conclusion not in {None, "success"}
        ],
        "changes_requested": [
            {
                "id": review.review_id,
                "state": review.state,
                "body": review.body,
                "user_login": review.user_login,
            }
            for review in reviews
            if review.state.strip().upper() == "CHANGES_REQUESTED"
        ],
        "review_comments": [
            {"id": comment.comment_id, "body": comment.body, "path": comment.path, "line": comment.line}
            for comment in review_comments
        ],
        "issue_comments": [
            {"id": comment.comment_id, "body": comment.body}
            for comment in issue_comments
        ],
        "manual_fix_request": dict(manual_fix_request) if isinstance(manual_fix_request, dict) else None,
        "requested_comment": manual_requested_comment,
    }

    enqueue_result = enqueue_run(
        session,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        issue_key=issue_key,
        issue_summary=f"{issue_key}: PR remediation for #{pr_number}",
        issue_description=(
            f"Automated remediation run triggered from GitHub PR #{pr_number} ({details.html_url}).\n"
            f"Event: {normalized_event}/{normalized_action}\n"
            f"Head SHA: {details.head_sha}"
            + manual_context_suffix
        ),
        repo_url=project.github_repository,
        delivery_id=None,
        precheck_outcome=resolve_enqueue_precheck_outcome(source="github_pr_remediation"),
        max_concurrent_runs=max_concurrent_runs,
        dedupe_scope=RUN_DEDUPE_SCOPE_PR_REMEDIATION,
    )
    run = enqueue_result.run
    if enqueue_result.enqueued:
        existing_plan = run.plan if isinstance(run.plan, dict) else {}
        normalized_head_ref = str(details.head_ref or "").strip()
        normalized_pr_url = str(details.html_url or "").strip()
        if normalized_head_ref:
            run.branch = normalized_head_ref
        if normalized_pr_url:
            run.pr_url = normalized_pr_url
        run.plan = {
            **existing_plan,
            "trigger_context": trigger_context,
            "orchestration_mode": "orchestrated_subagents",
        }
        session.commit()
        session.refresh(run)
    return enqueue_result
