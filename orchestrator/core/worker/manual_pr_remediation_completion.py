from __future__ import annotations

import logging

from orchestrator.core.communications import (
    GitHubManualFixIssueCommentReplyAction,
    GitHubManualFixReviewThreadReplyAction,
    TransportAction,
)
from orchestrator.core.github.transport_executor import GitHubTransportExecutor
from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.tools.github_app import github_client_from_tenant_config
from orchestrator.tools.repo_allowlist import normalize_repo_identifier

logger = logging.getLogger(__name__)


def publish_manual_pr_remediation_completion(
    *,
    session,
    tenant,
    project,
    run,
    workflow_result,
    settings,
    issue_url: str | None,
    logger_override=None,
    terminal_status: str | None = None,
) -> None:  # noqa: ANN001
    log = logger_override or logger
    actions = build_manual_pr_remediation_completion_actions(
        project=project,
        run=run,
        workflow_result=workflow_result,
        issue_url=issue_url,
        terminal_status=terminal_status,
    )
    if not actions:
        return

    github_config_raw = getattr(tenant, "github_config", {})
    github_config = github_config_raw if isinstance(github_config_raw, dict) else {}
    if not github_config:
        raise RuntimeError("Manual PR remediation completion requires tenant GitHub configuration")

    try:
        github_client = github_client_from_tenant_config(
            github_config,
            tenant_secret_lookup=lambda secret_ref: resolve_scoped_secret_ref(
                session,
                secret_ref=secret_ref,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                encryption_key=settings.secrets_encryption_key,
            ),
            platform_secret_lookup=lambda secret_ref: resolve_platform_secret_ref(
                session,
                secret_ref=secret_ref,
                encryption_key=settings.secrets_encryption_key,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        message = (
            "Failed to initialize GitHub client for manual PR remediation completion: "
            f"{type(exc).__name__}: {exc}"
        )
        log.warning(
            "manual_pr_remediation_completion_github_client_failed tenant_id=%s project_id=%s run_id=%s error=%s",
            getattr(tenant, "tenant_id", ""),
            getattr(project, "project_id", ""),
            getattr(run, "run_id", ""),
            exc,
        )
        raise RuntimeError(message) from exc

    executor = GitHubTransportExecutor(
        github_client=github_client,
        session=session,
        logger_override=log,
        raise_on_error=True,
    )
    for action in actions:
        executor.execute(action=action)


def build_manual_pr_remediation_completion_actions(
    *,
    project,
    run,
    workflow_result,
    issue_url: str | None,
    terminal_status: str | None = None,
) -> tuple[TransportAction, ...]:  # noqa: ANN001
    trigger_context = _trigger_context(getattr(run, "plan", None))
    manual_fix_request = trigger_context.get("manual_fix_request")
    requested_comment = trigger_context.get("requested_comment")
    if not isinstance(manual_fix_request, dict) or not isinstance(requested_comment, dict):
        return ()

    repo_full_name = _repo_full_name(getattr(project, "github_repository", None))
    if repo_full_name is None:
        return ()

    requested_comment_type = str(requested_comment.get("type") or "").strip().lower()
    triggering_comment_id = requested_comment.get("id")
    pr_number = _as_int(trigger_context.get("pr_number"))
    if not isinstance(triggering_comment_id, int) or pr_number is None:
        return ()

    status_label = _status_label(terminal_status or getattr(run, "status", None))
    triggering_comment_url = str(requested_comment.get("url") or "").strip() or None
    requested_by = str(manual_fix_request.get("requested_by") or "").strip() or None
    instruction_text = str(manual_fix_request.get("instruction_text") or "").strip() or None
    reason = _completion_reason(run=run, workflow_result=workflow_result)
    pr_url = str(getattr(run, "pr_url", None) or getattr(workflow_result, "pr_url", None) or "").strip() or None
    change_summary = _change_summary(workflow_result=workflow_result)

    common_kwargs = dict(
        repo_full_name=repo_full_name,
        pr_number=pr_number,
        tenant_id=str(getattr(run, "tenant_id", "") or "").strip(),
        project_id=str(getattr(project, "project_id", "") or "").strip(),
        requested_by=requested_by,
        triggering_comment_url=triggering_comment_url,
        instruction_text=instruction_text,
        issue_key=str(getattr(run, "issue_key", "") or "").strip() or None,
        issue_url=issue_url,
        enqueued=True,
        run_id=str(getattr(run, "run_id", "") or "").strip() or None,
        reason=reason,
        status_label=status_label,
        pr_url=pr_url,
        change_summary=change_summary,
    )

    if requested_comment_type == "review_comment":
        return (
            GitHubManualFixReviewThreadReplyAction(
                triggering_comment_id=triggering_comment_id,
                **common_kwargs,
            ),
        )

    if requested_comment_type == "issue_comment":
        return (
            GitHubManualFixIssueCommentReplyAction(
                triggering_comment_id=triggering_comment_id,
                **common_kwargs,
            ),
        )

    return ()


def _trigger_context(plan: object) -> dict[str, object]:
    if not isinstance(plan, dict):
        return {}
    trigger_context = plan.get("trigger_context")
    return dict(trigger_context) if isinstance(trigger_context, dict) else {}


def _repo_full_name(repository_url: object) -> str | None:
    if not isinstance(repository_url, str) or not repository_url.strip():
        return None
    normalized = normalize_repo_identifier(repository_url)
    prefix = "github.com/"
    if not normalized.startswith(prefix):
        return None
    full_name = normalized[len(prefix) :].strip("/")
    return full_name if full_name.count("/") == 1 else None


def _status_label(status: object) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"succeeded", "success", "completed"}:
        return "SUCCEEDED"
    if normalized in {"failed", "failure"}:
        return "FAILED"
    if normalized == "blocked":
        return "BLOCKED"
    if normalized == "cancelled":
        return "CANCELLED"
    return "FAILED"


def _completion_reason(*, run, workflow_result) -> str | None:  # noqa: ANN001
    diagnostics = getattr(workflow_result, "diagnostics", None)
    if diagnostics is not None:
        message = str(getattr(diagnostics, "message", "") or "").strip()
        if message:
            return message
    last_error = str(getattr(run, "last_error", "") or "").strip()
    return last_error or None


def _change_summary(*, workflow_result) -> tuple[str, ...]:  # noqa: ANN001
    for candidate in (
        getattr(workflow_result, "review_summary", None),
        getattr(workflow_result, "summary", None),
        getattr(workflow_result, "dev_rationale", None),
    ):
        if not isinstance(candidate, (list, tuple)):
            continue
        items = tuple(str(item).strip() for item in candidate if str(item).strip())
        if items:
            return items[:3]
    if str(getattr(workflow_result, "outcome", "") or "").strip().lower() == "success":
        return ("Implemented the requested change.",)
    return ()


def _as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None
