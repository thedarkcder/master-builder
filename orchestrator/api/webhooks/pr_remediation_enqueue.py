from __future__ import annotations

from orchestrator.core.decision_engine import resolve_enqueue_precheck_outcome
from orchestrator.core.runs import enqueue_run


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
    max_concurrent_runs: int | None = None,
):
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
        ),
        repo_url=project.github_repository,
        delivery_id=None,
        precheck_outcome=resolve_enqueue_precheck_outcome(source="github_pr_remediation"),
        max_concurrent_runs=max_concurrent_runs,
    )
    run = enqueue_result.run
    if enqueue_result.enqueued:
        existing_plan = run.plan if isinstance(run.plan, dict) else {}
        run.plan = {
            **existing_plan,
            "trigger_context": trigger_context,
            "orchestration_mode": "orchestrated_subagents",
        }
        session.commit()
        session.refresh(run)
    return enqueue_result
