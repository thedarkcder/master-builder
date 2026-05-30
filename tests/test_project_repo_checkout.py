from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.tools.project_repo_checkout import (
    ProjectRepoCheckoutError,
    _restrict_external_tool_surfaces_in_project_codex,
    _github_git_extraheader,
    cleanup_run_workspaces,
    check_run_snapshot_freshness,
    collect_local_repo_context,
    ensure_project_checkout,
    ensure_run_worktree,
    project_repo_dir,
    project_run_repo_dir,
    validate_run_worktree,
)


def test_ensure_project_checkout_clones_when_repo_missing() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        calls: list[tuple[tuple[str, ...], str, dict[str, str] | None]] = []

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            calls.append((tuple(args), str(cwd), env))
            if args[:2] == ["clone", "--origin"]:
                (Path(tmpdir) / "tenant-a" / "project-1" / "repo" / ".git").mkdir(parents=True, exist_ok=True)
            if args == ["rev-parse", "--git-path", "info/exclude"]:
                return str(Path(tmpdir) / "tenant-a" / "project-1" / "repo" / ".git" / "info" / "exclude") + "\n"
            return ""

        with patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git):
            repo_dir = ensure_project_checkout(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                github_installation_token="token-123",
            )

        assert repo_dir == Path(tmpdir) / "tenant-a" / "project-1" / "repo"
        assert len(calls) == 3
        assert calls[0][0][0] == "clone"
        assert calls[0][0][3] == "https://github.com/example/repo.git"
        assert "token-123" not in " ".join(calls[0][0])
        assert calls[0][2] is not None
        assert calls[0][2]["GIT_CONFIG_VALUE_0"].startswith("AUTHORIZATION: basic ")
        encoded_credential = calls[0][2]["GIT_CONFIG_VALUE_0"].split(" ", 2)[2]
        decoded_credential = base64.b64decode(encoded_credential).decode("utf-8")
        assert decoded_credential == "x-access-token:token-123"
        assert calls[1][0][:3] == ("remote", "set-url", "origin")
        assert calls[2][0] == ("rev-parse", "--git-path", "info/exclude")
        assert (repo_dir / "AGENTS.md").exists()
        assert (repo_dir / ".codex").is_dir()
        copied_codex_config = (repo_dir / ".codex" / "config.toml").read_text(encoding="utf-8")
        assert "[mcp_servers.jira_master_builder]" in copied_codex_config
        assert "[mcp_servers.jira_bsktpay]" in copied_codex_config
        assert "enabled = true" not in copied_codex_config
        assert copied_codex_config.count("enabled = false") >= 2
        assert "[features]" in copied_codex_config
        assert "apps = false" in copied_codex_config
        assert "plugins = false" in copied_codex_config
        assert (repo_dir / ".gitignore").exists()
        gitignore_content = (repo_dir / ".gitignore").read_text(encoding="utf-8")
        assert "Seeded by Master Builder" in gitignore_content
        exclude_lines = (repo_dir / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        assert "AGENTS.md" in exclude_lines
        assert ".codex/" in exclude_lines
        assert ".master-builder-run.json" in exclude_lines
        assert ".gitignore" in exclude_lines


def test_check_run_snapshot_freshness_fetches_with_github_app_auth() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "tenant-a" / "project-1" / "repo"
        repo_dir.mkdir(parents=True)
        calls: list[tuple[tuple[str, ...], dict[str, str] | None]] = []

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            assert cwd == repo_dir
            calls.append((tuple(args), env))
            if args == ["rev-parse", "origin/main"]:
                return "abc123\n"
            return ""

        with patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git):
            freshness = check_run_snapshot_freshness(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                start_point_ref="origin/main",
                start_point_sha="abc123",
                github_installation_token="token-123",
            )

        assert freshness.stale is False
        assert calls[0][0] == ("fetch", "origin", "--prune")
        assert calls[0][1] is not None
        assert calls[0][1]["GIT_TERMINAL_PROMPT"] == "0"
        encoded_credential = calls[0][1]["GIT_CONFIG_VALUE_0"].split(" ", 2)[2]
        decoded_credential = base64.b64decode(encoded_credential).decode("utf-8")
        assert decoded_credential == "x-access-token:token-123"


def test_github_git_extraheader_uses_basic_auth_with_x_access_token() -> None:
    header = _github_git_extraheader("token-xyz")
    assert header.startswith("AUTHORIZATION: basic ")
    encoded = header.split(" ", 2)[2]
    decoded = base64.b64decode(encoded).decode("utf-8")
    assert decoded == "x-access-token:token-xyz"
    assert "Bearer" not in header


def test_existing_project_checkout_preserves_tracked_agent_workspace_files() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        repo_dir = project_repo_dir(base_dir=tmpdir, tenant_id="tenant-a", project_id="project-1")
        repo_dir.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
        (repo_dir / "AGENTS.md").write_text("project-owned agents\n", encoding="utf-8")
        (repo_dir / ".codex").mkdir()
        (repo_dir / ".codex" / "config.toml").write_text(
            "[features]\napps = true\nplugins = true\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "add", "AGENTS.md", ".codex/config.toml"], cwd=repo_dir, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.email=test@example.com",
                "-c",
                "user.name=Test",
                "commit",
                "-m",
                "initial",
            ],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        assert ensure_project_checkout(
            base_dir=tmpdir,
            tenant_id="tenant-a",
            project=project,
            github_installation_token="unused",
        ) == repo_dir

        assert (repo_dir / "AGENTS.md").read_text(encoding="utf-8") == "project-owned agents\n"
        assert (repo_dir / ".codex" / "config.toml").read_text(encoding="utf-8") == (
            "[features]\napps = true\nplugins = true\n"
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout == ""


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

        with patch(
            "orchestrator.tools.project_repo_checkout._run_git",
            return_value=str(repo_dir / ".git" / "info" / "exclude") + "\n",
        ) as run_git_mock:
            resolved = ensure_project_checkout(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                github_installation_token="token-123",
            )

        assert resolved == repo_dir
        assert (repo_dir / ".gitignore").read_text(encoding="utf-8") == "custom-ignore\n"
        run_git_mock.assert_called_once_with(
            ["rev-parse", "--git-path", "info/exclude"],
            cwd=repo_dir,
        )


def test_ensure_project_checkout_seeds_stack_specific_gitignore_defaults() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "tenant-a" / "project-1" / "repo"
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        (repo_dir / "Package.swift").write_text("import PackageDescription\n", encoding="utf-8")
        (repo_dir / "next.config.js").write_text("module.exports = {}\n", encoding="utf-8")
        (repo_dir / "pom.xml").write_text("<project></project>\n", encoding="utf-8")

        with patch(
            "orchestrator.tools.project_repo_checkout._run_git",
            return_value=str(repo_dir / ".git" / "info" / "exclude") + "\n",
        ) as run_git_mock:
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
        run_git_mock.assert_called_once_with(
            ["rev-parse", "--git-path", "info/exclude"],
            cwd=repo_dir,
        )


def test_restrict_external_tool_surfaces_preserves_section_boundaries() -> None:
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
                "enabled = true\n"
                "[features]\n"
                "apps = true\n"
            ),
            encoding="utf-8",
        )

        _restrict_external_tool_surfaces_in_project_codex(repo_dir=repo_dir)

        updated = config_path.read_text(encoding="utf-8")
        assert "[mcp_servers.jira_master_builder]" in updated
        assert "[mcp_servers.jira_bsktpay]" in updated
        assert "enabled = true" not in updated
        assert updated.count("enabled = false") == 2
        assert "enabled = false[mcp_servers.jira_bsktpay]" not in updated
        assert "enabled = false\n[mcp_servers.jira_bsktpay]" in updated
        assert "[features]" in updated
        assert "apps = false" in updated
        assert "plugins = false" in updated


def test_restrict_external_tool_surfaces_adds_features_section_when_missing() -> None:
    with TemporaryDirectory() as tmpdir:
        repo_dir = Path(tmpdir) / "repo"
        codex_dir = repo_dir / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        config_path = codex_dir / "config.toml"
        config_path.write_text(
            (
                'model = "gpt-5.4"\n\n'
                "[mcp_servers.github]\n"
                'url = "https://example.invalid/mcp"\n'
            ),
            encoding="utf-8",
        )

        _restrict_external_tool_surfaces_in_project_codex(repo_dir=repo_dir)

        updated = config_path.read_text(encoding="utf-8")
        assert "[mcp_servers.github]" in updated
        assert "enabled = false" in updated
        assert "[features]" in updated
        assert "apps = false" in updated
        assert "plugins = false" in updated


def test_ensure_run_worktree_creates_run_scoped_repo_and_metadata() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    workspace_key = "worker-a"
    with TemporaryDirectory() as tmpdir:
        shared_repo_dir = project_repo_dir(base_dir=tmpdir, tenant_id="tenant-a", project_id="project-1")
        (shared_repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        calls: list[tuple[tuple[str, ...], str]] = []

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            _ = env
            calls.append((tuple(args), str(cwd)))
            if args == ["fetch", "origin", "--prune"]:
                return ""
            if args[:2] == ["worktree", "add"]:
                run_repo_dir = project_run_repo_dir(
                    base_dir=tmpdir,
                    tenant_id="tenant-a",
                    project_id="project-1",
                    run_id="run-1",
                    workspace_key=workspace_key,
                )
                run_repo_dir.mkdir(parents=True, exist_ok=True)
                git_dir = shared_repo_dir / ".git" / "worktrees" / "run-1"
                git_dir.mkdir(parents=True, exist_ok=True)
                (run_repo_dir / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
            if args[:3] == ["rev-parse", "--verify", "--quiet"]:
                return "abc123\n"
            if args == ["rev-parse", "HEAD"]:
                return "abc123def456\n"
            if args == ["rev-parse", "--git-path", "info/exclude"]:
                return str(shared_repo_dir / ".git" / "worktrees" / "run-1" / "info" / "exclude") + "\n"
            return ""

        with (
            patch("orchestrator.tools.project_repo_checkout._git_ref_exists", return_value=False),
            patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git),
        ):
            run_repo_dir, execution_branch = ensure_run_worktree(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                run_id="run-1",
                issue_key="TP-99",
                base_branch="main",
                integration_branch="feature/TP-99",
                workspace_key=workspace_key,
            )

        assert run_repo_dir == project_run_repo_dir(
            base_dir=tmpdir,
            tenant_id="tenant-a",
            project_id="project-1",
            run_id="run-1",
            workspace_key=workspace_key,
        )
        assert execution_branch == "run/tp-99/run-1"
        assert any(call[0][:3] == ("fetch", "origin", "--prune") for call in calls)
        assert any(call[0][:2] == ("worktree", "add") for call in calls)
        metadata = json.loads((run_repo_dir / ".master-builder-run.json").read_text(encoding="utf-8"))
        assert metadata["run_id"] == "run-1"
        assert metadata["execution_branch"] == "run/tp-99/run-1"
        assert metadata["workspace_key"] == workspace_key
        assert metadata["start_point_ref"] == "HEAD"
        assert metadata["start_point_sha"] == "abc123def456"
        exclude_lines = (shared_repo_dir / ".git" / "worktrees" / "run-1" / "info" / "exclude").read_text(encoding="utf-8")
        assert "AGENTS.md" in exclude_lines
        assert ".codex/" in exclude_lines
        assert ".master-builder-run.json" in exclude_lines
        assert ".gitignore" in exclude_lines


def test_ensure_run_worktree_preserves_full_remote_branch_path() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    workspace_key = "worker-a"
    with TemporaryDirectory() as tmpdir:
        shared_repo_dir = project_repo_dir(base_dir=tmpdir, tenant_id="tenant-a", project_id="project-1")
        (shared_repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        calls: list[tuple[tuple[str, ...], str]] = []

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            _ = env
            calls.append((tuple(args), str(cwd)))
            if args == ["fetch", "origin", "--prune"]:
                return ""
            if args[:2] == ["worktree", "add"]:
                run_repo_dir = project_run_repo_dir(
                    base_dir=tmpdir,
                    tenant_id="tenant-a",
                    project_id="project-1",
                    run_id="run-1",
                    workspace_key=workspace_key,
                )
                run_repo_dir.mkdir(parents=True, exist_ok=True)
                git_dir = shared_repo_dir / ".git" / "worktrees" / "run-1"
                git_dir.mkdir(parents=True, exist_ok=True)
                (run_repo_dir / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
                return ""
            if args == ["rev-parse", "origin/feature/team/TP-99"]:
                return "branchsha987654\n"
            if args == ["rev-parse", "--git-path", "info/exclude"]:
                return str(shared_repo_dir / ".git" / "worktrees" / "run-1" / "info" / "exclude") + "\n"
            raise AssertionError(f"unexpected git args: {args}")

        def _fake_ref_exists(*, cwd: Path, ref: str) -> bool:
            _ = cwd
            return ref == "refs/remotes/origin/feature/team/TP-99"

        with (
            patch("orchestrator.tools.project_repo_checkout._git_ref_exists", side_effect=_fake_ref_exists),
            patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git),
        ):
            run_repo_dir, execution_branch = ensure_run_worktree(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                run_id="run-1",
                issue_key="TP-99",
                base_branch="main",
                integration_branch="feature/team/TP-99",
                workspace_key=workspace_key,
            )

        assert run_repo_dir == project_run_repo_dir(
            base_dir=tmpdir,
            tenant_id="tenant-a",
            project_id="project-1",
            run_id="run-1",
            workspace_key=workspace_key,
        )
        assert execution_branch == "run/tp-99/run-1"
        metadata = json.loads((run_repo_dir / ".master-builder-run.json").read_text(encoding="utf-8"))
        assert metadata["start_point_ref"] == "origin/feature/team/TP-99"
        assert metadata["start_point_sha"] == "branchsha987654"
        assert any(
            call[0] == ("worktree", "add", "--force", "-B", "run/tp-99/run-1", str(run_repo_dir), "branchsha987654")
            for call in calls
        )


def test_ensure_run_worktree_recreates_unusable_existing_checkout() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    workspace_key = "worker-a"
    with TemporaryDirectory() as tmpdir:
        shared_repo_dir = project_repo_dir(base_dir=tmpdir, tenant_id="tenant-a", project_id="project-1")
        (shared_repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        run_repo_dir = project_run_repo_dir(
            base_dir=tmpdir,
            tenant_id="tenant-a",
            project_id="project-1",
            run_id="run-1",
            workspace_key=workspace_key,
        )
        run_repo_dir.mkdir(parents=True, exist_ok=True)
        (run_repo_dir / ".git").write_text("gitdir: /tmp/other-root/repo/.git/worktrees/run-1\n", encoding="utf-8")
        (run_repo_dir / ".master-builder-run.json").write_text(
            json.dumps(
                {
                    "run_id": "run-1",
                    "issue_key": "TP-99",
                    "execution_branch": "run/tp-99/run-1",
                    "base_branch": "main",
                    "integration_branch": "feature/TP-99",
                    "workspace_key": workspace_key,
                    "start_point_ref": "HEAD",
                    "start_point_sha": "abc123def456",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        calls: list[tuple[tuple[str, ...], str]] = []

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            _ = env
            calls.append((tuple(args), str(cwd)))
            if args == ["fetch", "origin", "--prune"]:
                return ""
            if args == ["worktree", "remove", "--force", str(run_repo_dir)]:
                shutil.rmtree(run_repo_dir.parent, ignore_errors=True)
                return ""
            if args == ["worktree", "prune"]:
                return ""
            if args[:2] == ["worktree", "add"]:
                run_repo_dir.mkdir(parents=True, exist_ok=True)
                git_dir = shared_repo_dir / ".git" / "worktrees" / "run-1"
                git_dir.mkdir(parents=True, exist_ok=True)
                (run_repo_dir / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
                return ""
            if args == ["rev-parse", "HEAD"]:
                return "abc123def456\n"
            if args == ["rev-parse", "--git-path", "info/exclude"]:
                return str(shared_repo_dir / ".git" / "worktrees" / "run-1" / "info" / "exclude") + "\n"
            raise AssertionError(f"unexpected git args: {args}")

        with (
            patch("orchestrator.tools.project_repo_checkout._git_ref_exists", return_value=False),
            patch("orchestrator.tools.project_repo_checkout._is_worktree_checkout_usable", return_value=False),
            patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git),
        ):
            result_repo_dir, execution_branch = ensure_run_worktree(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                run_id="run-1",
                issue_key="TP-99",
                base_branch="main",
                integration_branch="feature/TP-99",
                workspace_key=workspace_key,
            )

        assert result_repo_dir == run_repo_dir
        assert execution_branch == "run/tp-99/run-1"
        assert any(call[0] == ("worktree", "remove", "--force", str(run_repo_dir)) for call in calls)
        assert any(call[0] == ("worktree", "prune") for call in calls)
        assert any(call[0][:2] == ("worktree", "add") for call in calls)


def test_check_run_snapshot_freshness_detects_ref_drift() -> None:
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/example/repo")
    with TemporaryDirectory() as tmpdir:
        shared_repo_dir = project_repo_dir(base_dir=tmpdir, tenant_id="tenant-a", project_id="project-1")
        (shared_repo_dir / ".git").mkdir(parents=True, exist_ok=True)

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            _ = (cwd, env)
            if args == ["fetch", "origin", "--prune"]:
                return ""
            if args == ["rev-parse", "origin/main"]:
                return "newsha987654321\n"
            raise AssertionError(f"unexpected git args: {args}")

        with patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git):
            freshness = check_run_snapshot_freshness(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project=project,
                start_point_ref="origin/main",
                start_point_sha="oldsha123456789",
            )

        assert freshness.stale is True
        assert freshness.current_start_point_sha == "newsha987654321"
        assert "origin/main moved" in str(freshness.message or "")


def test_validate_run_worktree_rejects_branch_mismatch() -> None:
    workspace_key = "worker-a"
    with TemporaryDirectory() as tmpdir:
        run_repo_dir = project_run_repo_dir(
            base_dir=tmpdir,
            tenant_id="tenant-a",
            project_id="project-1",
            run_id="run-1",
            workspace_key=workspace_key,
        )
        (run_repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        (run_repo_dir / ".master-builder-run.json").write_text(
            (
                '{"run_id":"run-1","issue_key":"TP-99","execution_branch":"run/tp-99/run-1",'
                '"base_branch":"main","integration_branch":"feature/TP-99","workspace_key":"worker-a"}\n'
            ),
            encoding="utf-8",
        )

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            _ = (cwd, env)
            if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return "feature/other\n"
            if args == ["status", "--porcelain"]:
                return ""
            raise AssertionError(f"unexpected git args: {args}")

        with patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git):
            error = validate_run_worktree(
                repo_dir=run_repo_dir,
                run_id="run-1",
                execution_branch="run/tp-99/run-1",
                workspace_key=workspace_key,
            )

        assert error == "run worktree branch mismatch: expected run/tp-99/run-1, found feature/other"


def test_validate_run_worktree_allows_seeded_gitignore_without_reporting_dirtiness() -> None:
    workspace_key = "worker-a"
    with TemporaryDirectory() as tmpdir:
        run_repo_dir = project_run_repo_dir(
            base_dir=tmpdir,
            tenant_id="tenant-a",
            project_id="project-1",
            run_id="run-1",
            workspace_key=workspace_key,
        )
        (run_repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        (run_repo_dir / ".master-builder-run.json").write_text(
            (
                '{"run_id":"run-1","issue_key":"TP-99","execution_branch":"run/tp-99/run-1",'
                '"base_branch":"main","integration_branch":"feature/TP-99","workspace_key":"worker-a"}\n'
            ),
            encoding="utf-8",
        )

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            _ = (cwd, env)
            if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
                return "run/tp-99/run-1\n"
            if args == ["status", "--porcelain"]:
                return ""
            raise AssertionError(f"unexpected git args: {args}")

        with patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git):
            error = validate_run_worktree(
                repo_dir=run_repo_dir,
                run_id="run-1",
                execution_branch="run/tp-99/run-1",
                workspace_key=workspace_key,
            )

        assert error is None


def test_cleanup_run_workspaces_removes_workspace_dir_and_prunes() -> None:
    with TemporaryDirectory() as tmpdir:
        repo_dir = project_repo_dir(base_dir=tmpdir, tenant_id="tenant-a", project_id="project-1")
        (repo_dir / ".git").mkdir(parents=True, exist_ok=True)
        workspace_root = (
            Path(tmpdir)
            / "tenant-a"
            / "project-1"
            / "runs"
            / "run-1"
            / "workspaces"
            / "worker-a"
        )
        (workspace_root / "repo").mkdir(parents=True, exist_ok=True)
        calls: list[tuple[str, ...]] = []

        def _fake_run_git(args: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> str:
            _ = (cwd, env)
            calls.append(tuple(args))
            return ""

        with patch("orchestrator.tools.project_repo_checkout._run_git", side_effect=_fake_run_git):
            cleanup_run_workspaces(
                base_dir=tmpdir,
                tenant_id="tenant-a",
                project_id="project-1",
                run_id="run-1",
                workspace_key="worker-a",
            )

        assert not workspace_root.exists()
        assert ("worktree", "prune") in calls
