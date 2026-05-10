from __future__ import annotations

import re

from orchestrator.api.discord.bug.gap_analysis import (
    extract_acceptance_criteria_from_description as _extract_acceptance_criteria_from_description_impl,
)
from orchestrator.api.discord.bug.gap_analysis import gap_confidence as _gap_confidence_impl
from orchestrator.api.discord.bug.gap_analysis import run_gap_analysis as _run_gap_analysis_impl
from orchestrator.api.discord.bug.gap_analysis import tenant_repo_url as _tenant_repo_url_impl
from orchestrator.api.discord.ingress.github_context import collect_project_repo_context_for_issue
from orchestrator.core.projects.routing import find_active_project_for_issue_key

ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def tenant_repo_url(tenant) -> str | None:  # noqa: ANN001
    return _tenant_repo_url_impl(tenant)



def extract_acceptance_criteria_from_description(description: str) -> list[str]:
    return _extract_acceptance_criteria_from_description_impl(description)



def gap_confidence(*, has_acceptance: bool, has_successful_run: bool, has_pr: bool) -> str:
    return _gap_confidence_impl(
        has_acceptance=has_acceptance,
        has_successful_run=has_successful_run,
        has_pr=has_pr,
    )



def run_gap_analysis(*, session, tenant, issue_key: str) -> tuple[str, dict]:  # noqa: ANN001
    return _run_gap_analysis_impl(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        issue_key_pattern=ISSUE_KEY_PATTERN,
        collect_project_repo_context_fn=lambda **kwargs: collect_project_repo_context_for_issue(
            **kwargs,
            find_active_project_for_issue_key_fn=find_active_project_for_issue_key,
        ),
    )
