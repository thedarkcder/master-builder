from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.storage.models import Tenant


@dataclass(frozen=True)
class GitHubPushDeploymentSource:
    branch: str
    commit_sha: str


def extract_installation_id(payload: dict) -> str | None:
    installation = payload.get("installation")
    if isinstance(installation, dict):
        installation_id = installation.get("id")
        if isinstance(installation_id, int):
            return str(installation_id)
        if isinstance(installation_id, str) and installation_id.strip():
            return installation_id.strip()

    installation_id_fallback = payload.get("installation_id")
    if isinstance(installation_id_fallback, int):
        return str(installation_id_fallback)
    if isinstance(installation_id_fallback, str) and installation_id_fallback.strip():
        return installation_id_fallback.strip()
    return None


def find_tenant_by_installation_id(session: Session, installation_id: str) -> Tenant | None:
    tenants = session.execute(select(Tenant)).scalars().all()
    for tenant in tenants:
        configured_installation_id = str(tenant.github_config.get("installation_id") or "").strip()
        if configured_installation_id and configured_installation_id == installation_id:
            return tenant
    return None


def extract_repository_full_name(payload: dict) -> str | None:
    repository = payload.get("repository")
    if isinstance(repository, dict):
        full_name = repository.get("full_name")
        if isinstance(full_name, str) and full_name.strip():
            return full_name.strip()
    return None


def extract_push_deployment_source(payload: dict) -> GitHubPushDeploymentSource | None:
    ref = payload.get("ref")
    after = payload.get("after")
    if not isinstance(ref, str) or not ref.startswith("refs/heads/"):
        return None
    if not isinstance(after, str) or not after.strip():
        return None
    commit_sha = after.strip()
    if set(commit_sha) == {"0"}:
        return None
    branch = ref[len("refs/heads/"):].strip()
    if not branch:
        return None
    return GitHubPushDeploymentSource(branch=branch, commit_sha=commit_sha)


def extract_pull_request_targets(payload: dict) -> list[tuple[int, bool]]:
    targets: list[tuple[int, bool]] = []
    seen: set[int] = set()

    pull_request = payload.get("pull_request")
    if isinstance(pull_request, dict):
        number = pull_request.get("number")
        body = pull_request.get("body")
        review_summary_present = isinstance(body, str) and bool(body.strip())
        if isinstance(number, int) and number > 0 and number not in seen:
            targets.append((number, review_summary_present))
            seen.add(number)

    for container_key in ("check_suite", "check_run"):
        container = payload.get(container_key)
        if not isinstance(container, dict):
            continue
        pull_requests = container.get("pull_requests")
        if not isinstance(pull_requests, list):
            continue
        for item in pull_requests:
            if not isinstance(item, dict):
                continue
            number = item.get("number")
            if isinstance(number, int) and number > 0 and number not in seen:
                targets.append((number, True))
                seen.add(number)

    issue = payload.get("issue")
    if isinstance(issue, dict):
        number = issue.get("number")
        pull_request = issue.get("pull_request")
        if isinstance(number, int) and number > 0 and isinstance(pull_request, dict) and number not in seen:
            targets.append((number, True))
            seen.add(number)

    return targets
