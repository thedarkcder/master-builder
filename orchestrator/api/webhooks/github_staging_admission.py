from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.communications import GitHubPullRequestCheckRunAction

STAGING_MERGE_CHECK_NAME = "MB Staging Merge Check"


@dataclass(frozen=True)
class StagingAdmissionConfig:
    enabled: bool
    branch: str


@dataclass(frozen=True)
class StagingAdmissionPlan:
    enabled: bool
    results: list[dict[str, object]]
    actions: tuple[GitHubPullRequestCheckRunAction, ...] = ()


def resolve_staging_admission_config(
    project_overrides: dict | None,
) -> StagingAdmissionConfig:
    raw = dict(project_overrides or {})
    enabled = bool(raw.get("staging_admission_enabled"))
    branch = str(raw.get("staging_branch") or "staging").strip() or "staging"
    return StagingAdmissionConfig(enabled=enabled, branch=branch)


def plan_staging_admission_actions(
    *,
    github_event: str,
    normalized_action: str | None,
    payload: dict,
    repo_full_name: str,
    project_overrides: dict | None,
    pr_targets: list[tuple[int, bool]],
    github_client,
) -> StagingAdmissionPlan:
    config = resolve_staging_admission_config(project_overrides)
    if not config.enabled:
        return StagingAdmissionPlan(enabled=False, results=[])

    candidate_pr_numbers = _candidate_pr_numbers(
        github_event=github_event,
        normalized_action=normalized_action,
        payload=payload,
        repo_full_name=repo_full_name,
        branch=config.branch,
        pr_targets=pr_targets,
        github_client=github_client,
    )
    if not candidate_pr_numbers:
        return StagingAdmissionPlan(enabled=True, results=[], actions=())

    staging_head_sha = github_client.get_branch_head_sha(
        repo_full_name=repo_full_name, branch=config.branch
    )
    results: list[dict[str, object]] = []
    actions: list[GitHubPullRequestCheckRunAction] = []
    for pr_number in candidate_pr_numbers:
        pr = github_client.get_pull_request_details(
            repo_full_name=repo_full_name, pr_number=pr_number
        )
        if str(getattr(pr, "base_ref", "") or "").strip() != config.branch:
            continue
        result = _evaluate_pull_request(
            pr=pr, branch=config.branch, staging_head_sha=staging_head_sha
        )
        results.append(
            {
                "pr_number": pr_number,
                "conclusion": result["conclusion"],
                "summary": result["summary"],
            }
        )
        actions.append(
            GitHubPullRequestCheckRunAction(
                repo_full_name=repo_full_name,
                head_sha=pr.head_sha,
                name=STAGING_MERGE_CHECK_NAME,
                status="completed",
                conclusion=str(result["conclusion"]),
                title="Staging merge check",
                summary=str(result["summary"]),
            )
        )
    return StagingAdmissionPlan(enabled=True, results=results, actions=tuple(actions))


def _candidate_pr_numbers(
    *,
    github_event: str,
    normalized_action: str | None,
    payload: dict,
    repo_full_name: str,
    branch: str,
    pr_targets: list[tuple[int, bool]],
    github_client,
) -> list[int]:
    normalized_event = str(github_event or "").strip().lower()
    if normalized_event == "pull_request":
        if str(normalized_action or "").strip().lower() == "closed":
            return []
        return [pr_number for pr_number, _ in pr_targets]
    if normalized_event != "push":
        return []
    ref_name = str(payload.get("ref") or "").strip()
    if ref_name != f"refs/heads/{branch}":
        return []
    pull_requests = github_client.list_open_pull_requests(
        repo_full_name=repo_full_name, limit=100
    )
    return [
        pr.number
        for pr in pull_requests
        if str(getattr(pr, "base_ref", "") or "").strip() == branch
    ]


def _evaluate_pull_request(*, pr, branch: str, staging_head_sha: str) -> dict[str, str]:
    if str(getattr(pr, "state", "") or "").strip().lower() != "open":
        return {
            "conclusion": "failure",
            "summary": f"PR is not open and cannot be admitted to {branch}.",
        }
    if bool(getattr(pr, "draft", False)):
        return {
            "conclusion": "failure",
            "summary": f"PR is still draft and cannot be admitted to {branch}.",
        }
    mergeable = getattr(pr, "mergeable", None)
    mergeable_state = str(getattr(pr, "mergeable_state", "") or "").strip() or "unknown"
    if mergeable is True:
        return {
            "conclusion": "success",
            "summary": (
                f"Validated against {branch}@{staging_head_sha[:7]}. "
                f"GitHub mergeability is clean ({mergeable_state})."
            ),
        }
    return {
        "conclusion": "failure",
        "summary": (
            f"PR is not mergeable into {branch}@{staging_head_sha[:7]} "
            f"(state={mergeable_state})."
        ),
    }
