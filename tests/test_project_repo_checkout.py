from __future__ import annotations

import base64
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.tools.project_repo_checkout import (
    ProjectRepoCheckoutError,
    _disable_jira_mcp_servers_in_project_codex,
    _github_git_extraheader,
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
        assert calls[0][2]["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
        encoded_credential = calls[0][2]["GIT_CONFIG_VALUE_0"].split(" ", 2)[2]
        decoded_credential = base64.b64decode(encoded_credential).decode("utf-8")
        assert decoded_credential == "x-access-token:token-123"
        assert calls[1][0][:3] == ("remote", "set-url", "origin")
        assert (repo_dir / "AGENTS.md").exists()
        assert (repo_dir / ".codex").is_dir()
        copied_codex_config = (repo_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert "[mcp_servers.jira_master_builder]" in copied_codex_config
        assert "[mcp_servers.jira_bsktpay]" in copied_codex_config
        assert "enabled = true" not in copied_codex_config
        assert copied_codex_config.count("enabled = false") >= 2
        assert (repo_dir / ".gitignore").exists()
        gitignore_content = (repo_dir / ".gitignore").read_text(encoding="utf-8")
        assert "Seeded by Master Builder" in gitignore_content
        exclude_lines = (repo_dir / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert "AGENTS.md" in exclude_lines
        assert ".codex/" in exclude_lines


def test_github_git_extraheader_uses_basic_auth_with_x_access_token() -> None:
    header = _github_git_extraheader("token-xyz")
    assert header.startswith("AUTHORIZATION: basic ")
    encoded = header.split(" ", 2)[2]
    decoded = base64.b64decode(encoded).decode("utf-8")
    assert decoded == "x-access-token:token-xyz"
    assert "Bearer" not in header


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


def test_ensure_project_checkout_preserves_existing_gitignore() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "tenant-a" / "project-1" / "repo"
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        (repo_dir / ".gitignore").write_text("custom-ignore\n", encoding="utf-8")

        with patch("orchestrator.tools.project_repo_checkout._run_git") as run_git_mock:
            resolved = ensure_project_checkout(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                github_installation_token="token-123",
            )

        assert resolved == repo_dir
        assert (repo_dir / ".gitignore").read_text(encoding="utf-8") == "custom-ignore\n"
        run_git_mock.assert_not_called()


def test_ensure_project_checkout_seeds_stack_specific_gitignore_defaults() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "tenant-a" / "project-1" / "repo"
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        (repo_dir / "Package.swift").write_text("import PackageDescription\n", encoding="utf-8")
        (repo_dir / "next.config.js").write_text("module.exports = {}\n", encoding="utf-8")
        (repo_dir / "pom.xml").write_text("<project></project>\n", encoding="utf-8")

        with patch("orchestrator.tools.project_repo_checkout._run_git") as run_git_mock:
            resolved = ensure_project_checkout(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                github_installation_token="token-123",
            )

        assert resolved == repo_dir
        gitignore = (repo_dir / ".gitignore").read_text(encoding="utf-8")
        assert "# Swift / Xcode" in gitignore
        assert "DerivedData/" in gitignore
        assert "# Next.js / Node" in gitignore
        assert "node_modules/" in gitignore
        assert "# Java" in gitignore
        assert "target/" in gitignore
        run_git_mock.assert_not_called()


def test_disable_jira_mcp_servers_preserves_section_boundaries() -> None:
    with TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        codex_dir = repo_dir / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text(
            (
                "[mcp_servers.jira_master_builder]\n"
                'url = "https://mcp.atlassian.com/v1/mcp"\n'
                "enabled = true\n"
                "[mcp_servers.jira_bsktpay]\n"
                'url = "https://mcp.atlassian.com/v1/mcp"\n'
                "enabled = true"
            ),
            encoding="utf-8",
        )

        _disable_jira_mcp_servers_in_project_codex(repo_dir=repo_dir)

        updated = config_path.read_text(encoding="utf-8")
        assert "[mcp_servers.jira_master_builder]" in updated
        assert "[mcp_servers.jira_bsktpay]" in updated
        assert "enabled = true" not in updated
        assert updated.count("enabled = false") == 2
        assert "enabled = false[mcp_servers.jira_bsktpay]" not in updated
        assert "enabled = false\n[mcp_servers.jira_bsktpay]" in updated
