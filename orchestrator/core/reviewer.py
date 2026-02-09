from __future__ import annotations

from dataclasses import dataclass
import logging

from orchestrator.core.policy_pack import find_banned_pattern_violations, select_policy_pack_for_files
from orchestrator.core.pr_ready import PrReadinessResult, evaluate_pr_readiness
from orchestrator.core.signal_templates import format_discord_pr_ready_message
from orchestrator.tools.github_app import GitHubAppClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReviewerSignal:
    ready: bool
    state: str
    message: str
    readiness: PrReadinessResult
    must_fix_findings: tuple[str, ...] = ()
    policy_pack: str | None = None


class ReviewAgentGate:
    def __init__(
        self,
        github_client: GitHubAppClient,
        *,
        required_workflows: tuple[str, ...] = ("CI", "Security"),
    ):
        self._github_client = github_client
        self._required_workflows = required_workflows

    def evaluate_pr(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> ReviewerSignal:
        pr = self._github_client.get_pull_request_details(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        checks = self._github_client.list_check_suites(
            repo_full_name=repo_full_name,
            ref=pr.head_sha,
        )
        changed_files = self._github_client.list_pull_request_files(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        selected_policy_pack = select_policy_pack_for_files(files=changed_files)
        must_fix_findings: list[str] = []
        selected_policy_pack_key: str | None = None
        if selected_policy_pack is not None:
            selected_policy_pack_key = selected_policy_pack.language_key
            must_fix_findings = find_banned_pattern_violations(
                policy_pack=selected_policy_pack,
                files=changed_files,
            )
        logger.info(
            "reviewer_policy_pack_selected repo=%s pr=%s policy_pack=%s changed_files=%s findings=%s",
            repo_full_name,
            pr_number,
            selected_policy_pack_key,
            len(changed_files),
            len(must_fix_findings),
        )
        readiness = evaluate_pr_readiness(
            review_summary_markdown=pr.body,
            required_workflows=self._required_workflows,
            workflow_checks=checks,
        )

        if must_fix_findings:
            summary = "; ".join(must_fix_findings[:2])
            return ReviewerSignal(
                ready=False,
                state="policy_violations",
                message=f"PR blocked by policy violations: {summary}",
                readiness=readiness,
                must_fix_findings=tuple(must_fix_findings),
                policy_pack=selected_policy_pack_key,
            )

        if _source_changes_present(changed_files) and not _test_changes_present(changed_files):
            return ReviewerSignal(
                ready=False,
                state="missing_test_coverage",
                message="PR blocked: source changes detected without test file updates",
                readiness=readiness,
                must_fix_findings=("Add or update tests that cover the changed behavior.",),
                policy_pack=selected_policy_pack_key,
            )

        if readiness.ready:
            ready_signal = format_discord_pr_ready_message(
                pr_url=pr.html_url,
                jira_url=None,
                run_id=None,
                what_changed=(f"Required checks passed: {', '.join(self._required_workflows)}",),
                risk_impact=("No failing required checks detected.",),
                how_to_test=("Open the PR checks tab and verify CI/Security are green.",),
                questions=(),
                next_action="Please review + merge",
            )
            return ReviewerSignal(
                ready=True,
                state="ready",
                message=ready_signal,
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        if readiness.state == "missing_review_sections":
            missing_sections = ", ".join(readiness.missing_review_sections)
            return ReviewerSignal(
                ready=False,
                state="missing_review_sections",
                message=f"PR review summary missing required sections: {missing_sections}",
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        if readiness.state == "pending_checks":
            details = ", ".join(readiness.pending_workflows)
            return ReviewerSignal(
                ready=False,
                state="pending_checks",
                message=f"PR opened, checks running: {details}",
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        if readiness.state == "failing_checks":
            failures = ", ".join(readiness.failing_workflows[:2])
            return ReviewerSignal(
                ready=False,
                state="failing_checks",
                message=f"PR checks failing: {failures}",
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        if readiness.state == "missing_checks":
            missing = ", ".join(readiness.missing_workflows)
            return ReviewerSignal(
                ready=False,
                state="missing_checks",
                message=f"PR checks missing: {missing}",
                readiness=readiness,
                policy_pack=selected_policy_pack_key,
            )

        return ReviewerSignal(
            ready=False,
            state=readiness.state,
            message="PR review summary is required before PR Ready signal",
            readiness=readiness,
            policy_pack=selected_policy_pack_key,
        )


def _source_changes_present(changed_files) -> bool:  # noqa: ANN001
    source_suffixes = {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".java",
        ".kt",
        ".swift",
        ".go",
        ".rb",
        ".rs",
        ".cs",
    }
    for change in changed_files:
        filename = str(getattr(change, "filename", "")).strip().lower()
        if not filename:
            continue
        if "/tests/" in filename or filename.startswith("tests/") or "__tests__/" in filename:
            continue
        if filename.endswith((".md", ".txt", ".json", ".yaml", ".yml")):
            continue
        if any(filename.endswith(ext) for ext in source_suffixes):
            return True
    return False


def _test_changes_present(changed_files) -> bool:  # noqa: ANN001
    for change in changed_files:
        filename = str(getattr(change, "filename", "")).strip().lower()
        if not filename:
            continue
        if filename.startswith("tests/") or "/tests/" in filename or "__tests__/" in filename:
            return True
        if filename.startswith("test_") or "/test_" in filename:
            return True
        if filename.endswith(("_test.py", ".test.ts", ".test.tsx", ".spec.ts", ".spec.tsx", ".spec.js")):
            return True
    return False
