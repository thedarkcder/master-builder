from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from orchestrator.api.admin.github_helpers import (
    get_project_github_branch_head_sha,
    list_project_github_branches,
)
from orchestrator.storage.models import Project, Tenant
from orchestrator.tools.github_app import GitHubBranch


class _BranchClient:
    def list_repository_branches(
        self, *, repo_full_name: str, github_repository: str, limit: int = 100
    ) -> list[GitHubBranch]:
        assert repo_full_name == "example/align"
        assert github_repository == "https://github.com/example/align"
        assert limit == 100
        return [
            GitHubBranch(name="main", protected=True),
            GitHubBranch(name="develop", protected=False),
        ]


class _BranchHeadClient:
    def get_repository_branch_head_sha(
        self, *, repo_full_name: str, github_repository: str, branch: str
    ) -> str:
        assert repo_full_name == "example/align"
        assert github_repository == "https://github.com/example/align"
        assert branch == "main"
        return "abcdef1234567890"


def test_list_project_github_branches_uses_composition_root_github_ref_signature() -> (
    None
):
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        tenant_id="tenant-1",
        name="Tenant",
        jira_config={},
        github_config={"installation_id": "123"},
        repos_config={},
        policy_config={},
        discord_config={},
        experience_config={},
        setup_state={},
        deployment_plane_config={},
        created_at=now,
        updated_at=now,
    )
    project = Project(
        project_id="project-1",
        tenant_id="tenant-1",
        name="Align",
        github_repository="https://github.com/example/align",
        jira_project_key="ALN",
        policy_overrides={},
        architecture_docs_config={},
        environment={},
        secret_refs={},
        discord_config={},
        deployment_config={},
        created_at=now,
        updated_at=now,
    )
    refs_calls: list[dict] = []

    def with_refs(raw_github_config: dict) -> dict:
        refs_calls.append(raw_github_config)
        return {**raw_github_config, "app_id_ref": "platform/GITHUB_APP_ID"}

    branches = list_project_github_branches(
        tenant=tenant,
        project=project,
        tenant_id="tenant-1",
        session=object(),
        settings=SimpleNamespace(secrets_encryption_key="test-key"),
        with_managed_github_refs_fn=with_refs,
        resolve_scoped_secret_ref_fn=lambda *_args, **_kwargs: "unused",
        resolve_platform_secret_ref_fn=lambda *_args, **_kwargs: "unused",
        github_client_from_tenant_config_fn=lambda *_args, **_kwargs: _BranchClient(),
    )

    assert refs_calls == [{"installation_id": "123"}]
    assert [branch.model_dump() for branch in branches] == [
        {"name": "main", "protected": True},
        {"name": "develop", "protected": False},
    ]


def test_get_project_github_branch_head_sha_uses_project_repo_scope() -> None:
    now = datetime.now(timezone.utc)
    tenant = Tenant(
        tenant_id="tenant-1",
        name="Tenant",
        jira_config={},
        github_config={"installation_id": "123"},
        repos_config={},
        policy_config={},
        discord_config={},
        experience_config={},
        setup_state={},
        deployment_plane_config={},
        created_at=now,
        updated_at=now,
    )
    project = Project(
        project_id="project-1",
        tenant_id="tenant-1",
        name="Align",
        github_repository="https://github.com/example/align",
        jira_project_key="ALN",
        policy_overrides={},
        architecture_docs_config={},
        environment={},
        secret_refs={},
        discord_config={},
        deployment_config={},
        created_at=now,
        updated_at=now,
    )

    head_sha = get_project_github_branch_head_sha(
        tenant=tenant,
        project=project,
        tenant_id="tenant-1",
        branch="main",
        session=object(),
        settings=SimpleNamespace(secrets_encryption_key="test-key"),
        with_managed_github_refs_fn=lambda github: dict(github),
        resolve_scoped_secret_ref_fn=lambda *_args, **_kwargs: "unused",
        resolve_platform_secret_ref_fn=lambda *_args, **_kwargs: "unused",
        github_client_from_tenant_config_fn=lambda *_args, **_kwargs: (
            _BranchHeadClient()
        ),
    )

    assert head_sha == "abcdef1234567890"
