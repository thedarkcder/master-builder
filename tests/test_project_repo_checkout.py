from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.tools.project_repo_checkout import (
    ProjectRepoCheckoutError,
    collect_local_repo_context,
    ensure_project_checkout,
    project_repo_dir,
)


def test_ensure_project_checkout_clones_when_repo_missing() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        calls: list[tuple[tuple[str, ...], str, dict[str, str] | None]] = []

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            calls.append((tuple(args), str(cwd), env))
            if args[:2] == ["clone", "--origin"]:
                (Path(tmpdir) / "tenant-a" / "project-1" / "repo" / ".git").mkdir(parents=True, exist_ok=True)
            return ""

        with patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git):
            repo_dir = ensure_project_checkout(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                github_installation_token="token-123",
            )

        assert repo_dir == Path(tmpdir) / "tenant-a" / "project-1" / "repo"
        assert len(calls) == 2
        assert calls[0][0][0] == "clone"
        assert calls[0][0][3] == "https://github.com/example/repo.git"
        assert "token-123" not in " ".join(calls[0][0])
        assert calls[0][2] is not None
        assert calls[0][2]["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer token-123"
        assert calls[1][0][:3] == ("remote", "set-url", "origin")


def test_collect_local_repo_context_returns_not_cloned_when_missing() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        context = collect_local_repo_context(
            base_dir=tmpdir,
            tenant_id="tenant-a",
            project=project,
            issue_key="TP-123",
        )
    assert context.available is False
    assert context.reason == "repository_not_cloned"


def test_ensure_project_checkout_cleans_up_directory_when_clone_fails() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "tenant-a" / "project-1" / "repo"

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            _ = cwd
            _ = env
            if args[:2] == ["clone", "--origin"]:
                repo_dir.mkdir(parents=True, exist_ok=True)
                raise ProjectRepoCheckoutError("clone failed")
            return ""

        with patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git):
            try:
                ensure_project_checkout(
                    base_dir=tmpdir,
                    tenant_id="tenant-a",
                    project=project,
                    github_installation_token="token-123",
                )
                assert False, "expected ProjectRepoCheckoutError"
            except ProjectRepoCheckoutError:
                pass

        assert not repo_dir.exists()


def test_collect_local_repo_context_reads_existing_repo_metadata() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        repo_dir = project_repo_dir(base_dir=tmpdir, tenant_id="tenant-a", project_id="project-1")
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)

        command_outputs = {
            ("rev-parse", "--abbrev-ref", "HEAD"): "staging\n",
            ("rev-parse", "HEAD"): "abc123\n",
            (
                "for-each-ref",
                "--format=%(refname:short)",
                "--sort=-committerdate",
                "refs/heads",
                "refs/remotes/origin",
            ): "staging\norigin/staging\njira/TP-123-work\n",
            ("log", "--oneline", "--decorate", "-n", "25", "--all"): "abc123 TP-123: work done\n",
            ("log", "--oneline", "--decorate", "--all", "--grep", "TP-123", "-n", "10"): "abc123 TP-123: work done\n",
        }

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            _ = env
            return command_outputs[tuple(args)]

        with patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git):
            context = collect_local_repo_context(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                issue_key="TP-123",
            )

    assert context.available is True
    assert context.current_branch == "staging"
    assert context.head_sha == "abc123"
    assert context.issue_related_commits == ["abc123 TP-123: work done"]
