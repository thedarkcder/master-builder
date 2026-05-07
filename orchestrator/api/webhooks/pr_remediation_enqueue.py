from __future__ import annotations

from orchestrator.core.runs.service import (
    RUN_DEDUPE_SCOPE_PR_REMEDIATION,
    RunBootstrap,
    enqueue_run,
    resolve_enqueue_precheck_outcome,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot


def _build_manual_fix_trigger_context(
    *,
    issue_key: str,
    issue_created: bool,
    pr_number: int,
    details,
    normalized_event: str,
    normalized_action: str,
    manual_fix_request: dict[str, object],
    requested_comment: dict[str, object] | None,
) -> dict[str, object]:
    code_context = (
        dict(manual_fix_request.get("code_context"))
        if isinstance(manual_fix_request.get("code_context"), dict)
        else None
    )
    return {
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
        "manual_fix_request": dict(manual_fix_request),
        "requested_comment": requested_comment,
        "code_context": code_context,
    }


def _bootstrap_plan_for_pr_remediation(*, trigger_context: dict[str, object]) -> dict[str, object]:
    snapshot = ExecutionSnapshot.empty(trigger_context=trigger_context)
    snapshot.context.execution_context["orchestration_mode"] = "orchestrated_subagents"
    return snapshot.dump()


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
            manual_context_lines.append(f"Command comment: {comment_url}")
        comment_body = str(manual_requested_comment.get("body") or "").strip()
        if comment_body:
            manual_context_lines.append(f"Command comment body: {comment_body}")
    instruction_text = (
        str(manual_fix_request.get("instruction_text") or "").strip()
        if isinstance(manual_fix_request, dict)
        else ""
    )
    if instruction_text:
        manual_context_lines.append(f"Instruction: {instruction_text}")
    manual_context_suffix = ""
    if manual_context_lines:
        manual_context_suffix = "\n" + "\n".join(manual_context_lines)

    if isinstance(manual_fix_request, dict) and manual_requested_comment is not None:
        trigger_context = _build_manual_fix_trigger_context(
            issue_key=issue_key,
            issue_created=issue_created,
            pr_number=pr_number,
            details=details,
            normalized_event=normalized_event,
            normalized_action=normalized_action,
            manual_fix_request=manual_fix_request,
            requested_comment=manual_requested_comment,
        )
    else:
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
        bootstrap=RunBootstrap(
            branch=str(details.head_ref or "").strip() or None,
            pr_url=str(details.html_url or "").strip() or None,
            plan=_bootstrap_plan_for_pr_remediation(trigger_context=trigger_context),
        ),
    )
    return enqueue_result
