from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

import pytest

from orchestrator.core.agent_tools import allowed_tools_for_stage, execute_agent_tool


def test_allowed_tools_for_stage_dev_contains_github_and_jira() -> None:
    tools = allowed_tools_for_stage("dev")
    assert "github.create_branch" in tools
    assert "github.open_pr" in tools
    assert "jira.comment" in tools


def test_execute_agent_tool_rejects_disallowed_stage_tool() -> None:
    class _FakeContext:
        stage = "pm"

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with pytest.raises(PermissionError):
            execute_agent_tool(
                session=None,  # type: ignore[arg-type]
                settings=None,
                tenant_id="route25",
                project_id="route25-default",
                run_id="run-1",
                issue_key="MAB-1",
                stage="pm",
                tool_name="github.open_pr",
                tool_args={},
            )


def test_github_create_branch_uses_remote_default_when_base_omitted() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    class _FakeGitHubClient:
        def get_installation_token(self) -> str:
            return "token-123"

    git_calls: list[tuple[list[str], str | None]] = []

    def _fake_run_git(_repo_dir: Path, args: list[str], *, token: str | None = None) -> str:
        git_calls.append((args, token))
        if args == ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]:
            return "origin/main\n"
        return ""

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with patch("orchestrator.core.agent_tools.github_client_from_tenant_config", return_value=_FakeGitHubClient()):
            with patch("orchestrator.core.agent_tools._run_git", side_effect=_fake_run_git):
                payload = execute_agent_tool(
                    session=None,  # type: ignore[arg-type]
                    settings=None,
                    tenant_id="route25",
                    project_id="route25-default",
                    run_id="run-1",
                    issue_key="MAB-1",
                    stage="dev",
                    tool_name="github.create_branch",
                    tool_args={"branch_name": "jira/MAB-1-test"},
                )

    assert payload == {"branch_name": "jira/MAB-1-test"}
    assert git_calls == [
        (["fetch", "origin", "--prune"], "token-123"),
        (["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], None),
        (["fetch", "origin", "main"], "token-123"),
        (["checkout", "-B", "main", "origin/main"], None),
        (["checkout", "-B", "jira/MAB-1-test"], None),
    ]


def test_github_create_branch_uses_supplied_base_branch_for_sync() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    class _FakeGitHubClient:
        def get_installation_token(self) -> str:
            return "token-123"

    git_calls: list[tuple[list[str], str | None]] = []

    def _fake_run_git(_repo_dir: Path, args: list[str], *, token: str | None = None) -> str:
        git_calls.append((args, token))
        return ""

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with patch("orchestrator.core.agent_tools.github_client_from_tenant_config", return_value=_FakeGitHubClient()):
            with patch("orchestrator.core.agent_tools._run_git", side_effect=_fake_run_git):
                payload = execute_agent_tool(
                    session=None,  # type: ignore[arg-type]
                    settings=None,
                    tenant_id="route25",
                    project_id="route25-default",
                    run_id="run-1",
                    issue_key="MAB-1",
                    stage="dev",
                    tool_name="github.create_branch",
                    tool_args={"branch_name": "jira/MAB-1-test", "base_branch": "develop"},
                )

    assert payload == {"branch_name": "jira/MAB-1-test"}
    assert git_calls == [
        (["fetch", "origin", "develop"], "token-123"),
        (["checkout", "-B", "develop", "origin/develop"], None),
        (["checkout", "-B", "jira/MAB-1-test"], None),
    ]


def test_repo_read_allows_read_only_git_status() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "test"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    fake_process = SimpleNamespace(returncode=0, stdout="ok", stderr="")
    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with patch("orchestrator.core.agent_tools.subprocess.run", return_value=fake_process) as run_mock:
            payload = execute_agent_tool(
                session=None,  # type: ignore[arg-type]
                settings=None,
                tenant_id="route25",
                project_id="route25-default",
                run_id="run-1",
                issue_key="MAB-1",
                stage="test",
                tool_name="repo.read",
                tool_args={"command": "git status -sb"},
            )

    assert payload == {"ok": True, "exit_code": 0, "stdout": "ok", "stderr": ""}
    run_mock.assert_called_once()


def test_repo_read_rejects_mutating_git_subcommand() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "pm"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with patch("orchestrator.core.agent_tools.subprocess.run") as run_mock:
            with pytest.raises(PermissionError, match="mutating git subcommand"):
                execute_agent_tool(
                    session=None,  # type: ignore[arg-type]
                    settings=None,
                    tenant_id="route25",
                    project_id="route25-default",
                    run_id="run-1",
                    issue_key="MAB-1",
                    stage="pm",
                    tool_name="repo.read",
                    tool_args={"command": "git checkout -b bad"},
                )
    run_mock.assert_not_called()


def test_repo_read_rejects_shell_operator_chaining() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "review"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with patch("orchestrator.core.agent_tools.subprocess.run") as run_mock:
            with pytest.raises(PermissionError, match="no shell operators"):
                execute_agent_tool(
                    session=None,  # type: ignore[arg-type]
                    settings=None,
                    tenant_id="route25",
                    project_id="route25-default",
                    run_id="run-1",
                    issue_key="MAB-1",
                    stage="review",
                    tool_name="repo.read",
                    tool_args={"command": "git status && git add -A"},
                )
    run_mock.assert_not_called()


def test_repo_read_allows_mutating_git_command_for_dev_stage() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    fake_process = SimpleNamespace(returncode=0, stdout="", stderr="")
    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with patch("orchestrator.core.agent_tools.subprocess.run", return_value=fake_process) as run_mock:
            payload = execute_agent_tool(
                session=None,  # type: ignore[arg-type]
                settings=None,
                tenant_id="route25",
                project_id="route25-default",
                run_id="run-1",
                issue_key="MAB-1",
                stage="dev",
                tool_name="repo.read",
                tool_args={"command": "git checkout -b jira/MAB-1-test"},
            )

    assert payload == {"ok": True, "exit_code": 0, "stdout": "", "stderr": ""}
    run_mock.assert_called_once()


def test_repo_read_rejects_git_push_for_dev_stage() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with patch("orchestrator.core.agent_tools.subprocess.run") as run_mock:
            with pytest.raises(PermissionError, match="use github.push_branch"):
                execute_agent_tool(
                    session=None,  # type: ignore[arg-type]
                    settings=None,
                    tenant_id="route25",
                    project_id="route25-default",
                    run_id="run-1",
                    issue_key="MAB-1",
                    stage="dev",
                    tool_name="repo.read",
                    tool_args={"command": "git push -u origin jira/MAB-1-test"},
                )
    run_mock.assert_not_called()
