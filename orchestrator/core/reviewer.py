from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.pr_ready import PrReadinessResult, evaluate_pr_readiness
from orchestrator.core.signal_templates import format_discord_pr_ready_message
from orchestrator.tools.github_app import GitHubAppClient


@dataclass(frozen=True)
class ReviewerSignal:
    ready: bool
    state: str
    message: str
    readiness: PrReadinessResult


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
        review_summary_present: bool,
    ) -> ReviewerSignal:
        pr = self._github_client.get_pull_request_details(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        checks = self._github_client.list_check_suites(
            repo_full_name=repo_full_name,
            ref=pr.head_sha,
        )
        readiness = evaluate_pr_readiness(
            review_summary_present=review_summary_present,
            required_workflows=self._required_workflows,
            workflow_checks=checks,
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
            )

        if readiness.state == "pending_checks":
            details = ", ".join(readiness.pending_workflows)
            return ReviewerSignal(
                ready=False,
                state="pending_checks",
                message=f"PR opened, checks running: {details}",
                readiness=readiness,
            )

        if readiness.state == "failing_checks":
            failures = ", ".join(readiness.failing_workflows[:2])
            return ReviewerSignal(
                ready=False,
                state="failing_checks",
                message=f"PR checks failing: {failures}",
                readiness=readiness,
            )

        if readiness.state == "missing_checks":
            missing = ", ".join(readiness.missing_workflows)
            return ReviewerSignal(
                ready=False,
                state="missing_checks",
                message=f"PR checks missing: {missing}",
                readiness=readiness,
            )

        return ReviewerSignal(
            ready=False,
            state=readiness.state,
            message="PR review summary is required before PR Ready signal",
            readiness=readiness,
        )
