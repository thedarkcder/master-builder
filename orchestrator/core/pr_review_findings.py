from __future__ import annotations

from dataclasses import dataclass
import json

from orchestrator.core.codex_invocation import AgentInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.config import get_settings
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.tools.github_app import PullRequestFileChange, WorkflowCheckSuite


@dataclass(frozen=True)
class ReviewFinding:
    severity: str
    message: str
    path: str | None = None
    line: int | None = None
    suggestion: str | None = None


@dataclass(frozen=True)
class PrReviewFindingsResult:
    state: str
    summary: str
    findings: tuple[ReviewFinding, ...]


def _workflow_checks_payload(workflow_checks: list[WorkflowCheckSuite]) -> list[dict[str, str | None]]:
    return [
        {
            "name": check.name,
            "status": check.status,
            "conclusion": check.conclusion,
        }
        for check in workflow_checks
    ]


def _file_changes_payload(changed_files: list[PullRequestFileChange]) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for change in changed_files[:40]:
        filename = str(change.filename or "").strip()
        if not filename:
            continue
        patch = str(change.patch or "")
        payload.append(
            {
                "filename": filename,
                "patch": patch[:4000],
            }
        )
    return payload


def evaluate_pr_review_findings(
    *,
    repo_full_name: str,
    pr_number: int,
    pr_title: str,
    pr_body: str | None,
    workflow_checks: list[WorkflowCheckSuite],
    changed_files: list[PullRequestFileChange],
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> PrReviewFindingsResult:
    settings = get_settings()
    runtime = build_codex_runtime(session=None, settings=settings)
    try:
        payload = invoke_codex_json(
            runtime=runtime,
            context=AgentInvocationContext(
                channel="system",
                tenant_id=tenant_id,
                project_id=project_id,
                command="policy",
                stage="pr_review_findings",
                working_dir=".",
                issue_description_chars=len(pr_body or ""),
            ),
            system_prompt=render_prompt("policy/pr_review_findings_system.j2"),
            user_prompt=render_prompt(
                "policy/pr_review_findings_user.j2",
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                pr_title=pr_title,
                pr_body=pr_body or "",
                workflow_checks_json=json.dumps(_workflow_checks_payload(workflow_checks)),
                changed_files_json=json.dumps(_file_changes_payload(changed_files)),
            ),
        )
    except CodexRuntimeError as exc:
        raise RuntimeError(f"Codex PR findings evaluation failed: {exc}") from exc

    state = str(payload.get("state") or "").strip() or "reviewed"
    summary = str(payload.get("summary") or "").strip() or "Codex review completed."

    findings_raw = payload.get("findings")
    findings: list[ReviewFinding] = []
    if isinstance(findings_raw, list):
        for item in findings_raw:
            if not isinstance(item, dict):
                continue
            message = str(item.get("message") or "").strip()
            if not message:
                continue
            severity = str(item.get("severity") or "").strip() or "medium"
            path_raw = str(item.get("path") or "").strip()
            line_raw = item.get("line")
            line = line_raw if isinstance(line_raw, int) and line_raw > 0 else None
            suggestion_raw = str(item.get("suggestion") or "").strip()
            findings.append(
                ReviewFinding(
                    severity=severity,
                    message=message,
                    path=path_raw or None,
                    line=line,
                    suggestion=suggestion_raw or None,
                )
            )

    return PrReviewFindingsResult(
        state=state,
        summary=summary,
        findings=tuple(findings),
    )
