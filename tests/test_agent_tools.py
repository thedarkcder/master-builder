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
    assert "project.get_runtime_values" in tools


def test_allowed_tools_for_stage_pm_contains_evidence_tools() -> None:
    tools = allowed_tools_for_stage("pm")
    assert "jira.get_issue" in tools
    assert "decision.read_state" in tools
    assert "knowledge.read" in tools
    assert "project.list_runtime_keys" in tools
    assert "project.get_runtime_values" in tools


def test_execute_agent_tool_rejects_disallowed_stage_tool() -> None:
    class _FakeContext:
        stage = "pm"

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with pytest.raises(PermissionError):
            execute_agent_tool(
                session=None,  # type: ignore[arg-type]
                settings=None,
                tenant_id="example",
                project_id="example-default",
                run_id="run-1",
                issue_key="MAB-1",
                stage="pm",
                tool_name="github.open_pr",
                tool_args={},
            )


def test_github_create_branch_uses_remote_default_when_base_omitted() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
            with patch("orchestrator.core.agent_tools.github_client_from_tenant_config", return_value=_FakeGitHubClient()):
                with patch("orchestrator.core.agent_tools._run_git", side_effect=_fake_run_git):
                    payload = execute_agent_tool(
                        session=None,  # type: ignore[arg-type]
                        settings=None,
                    tenant_id="example",
                    project_id="example-default",
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
        tenant_id = "example"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
            with patch("orchestrator.core.agent_tools.github_client_from_tenant_config", return_value=_FakeGitHubClient()):
                with patch("orchestrator.core.agent_tools._run_git", side_effect=_fake_run_git):
                    payload = execute_agent_tool(
                        session=None,  # type: ignore[arg-type]
                        settings=None,
                    tenant_id="example",
                    project_id="example-default",
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
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
            with patch("orchestrator.core.agent_tools.subprocess.run", return_value=fake_process) as run_mock:
                payload = execute_agent_tool(
                    session=None,  # type: ignore[arg-type]
                    settings=None,
                tenant_id="example",
                project_id="example-default",
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
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
            with patch("orchestrator.core.agent_tools.subprocess.run") as run_mock:
                with pytest.raises(PermissionError, match="mutating git subcommand"):
                    execute_agent_tool(
                        session=None,  # type: ignore[arg-type]
                    settings=None,
                    tenant_id="example",
                    project_id="example-default",
                    run_id="run-1",
                    issue_key="MAB-1",
                    stage="pm",
                    tool_name="repo.read",
                    tool_args={"command": "git checkout -b bad"},
                )
    run_mock.assert_not_called()


def test_repo_read_rejects_shell_operator_chaining() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
            with patch("orchestrator.core.agent_tools.subprocess.run") as run_mock:
                with pytest.raises(PermissionError, match="no shell operators"):
                    execute_agent_tool(
                        session=None,  # type: ignore[arg-type]
                    settings=None,
                    tenant_id="example",
                    project_id="example-default",
                    run_id="run-1",
                    issue_key="MAB-1",
                    stage="review",
                    tool_name="repo.read",
                    tool_args={"command": "git status && git add -A"},
                )
    run_mock.assert_not_called()


def test_repo_read_allows_mutating_git_command_for_dev_stage() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
            with patch("orchestrator.core.agent_tools.subprocess.run", return_value=fake_process) as run_mock:
                payload = execute_agent_tool(
                    session=None,  # type: ignore[arg-type]
                    settings=None,
                tenant_id="example",
                project_id="example-default",
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
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
            with patch("orchestrator.core.agent_tools.subprocess.run") as run_mock:
                with pytest.raises(PermissionError, match="use github.push_branch"):
                    execute_agent_tool(
                        session=None,  # type: ignore[arg-type]
                    settings=None,
                    tenant_id="example",
                    project_id="example-default",
                    run_id="run-1",
                    issue_key="MAB-1",
                    stage="dev",
                    tool_name="repo.read",
                    tool_args={"command": "git push -u origin jira/MAB-1-test"},
                )
    run_mock.assert_not_called()


def test_decision_read_state_does_not_require_repo_checkout() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "decision_planner"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/missing-repo")

    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools._execute_decision_tool", return_value={"ok": True}) as execute_mock,
    ):
        payload = execute_agent_tool(
            session=None,  # type: ignore[arg-type]
            settings=None,
            tenant_id="example",
            project_id="example-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="decision_planner",
            tool_name="decision.read_state",
            tool_args={},
        )

    assert payload == {"ok": True}
    execute_mock.assert_called_once()


def test_repo_read_requires_repo_checkout() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "test"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/missing-repo")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with pytest.raises(ValueError, match="Repository checkout missing"):
            execute_agent_tool(
                session=None,  # type: ignore[arg-type]
                settings=None,
                tenant_id="example",
                project_id="example-default",
                run_id="run-1",
                issue_key="MAB-1",
                stage="test",
                tool_name="repo.read",
                tool_args={"command": "git status -sb"},
            )


def test_project_get_runtime_values_returns_environment_value() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"SUPABASE_URL": "https://example.supabase.co"}
        secret_refs = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        payload = execute_agent_tool(
            session=None,  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="example",
            project_id="example-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="dev",
            tool_name="project.get_runtime_values",
            tool_args={"keys": ["SUPABASE_URL"]},
        )

    assert payload == {
        "values": {
            "SUPABASE_URL": {
                "source": "environment",
                "value": "https://example.supabase.co",
                "secret_ref": None,
            }
        }
    }


def test_project_get_runtime_values_resolves_scoped_secret_ref() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {}
        secret_refs = {"SUPABASE_ANON_KEY": "project/example-default/supabase_anon_key"}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools.resolve_scoped_secret_ref", return_value="anon-key-value") as scoped_mock,
    ):
        payload = execute_agent_tool(
            session=object(),  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key="enc-key"),
            tenant_id="example",
            project_id="example-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="dev",
            tool_name="project.get_runtime_values",
            tool_args={"keys": ["SUPABASE_ANON_KEY"]},
        )

    assert payload == {
        "values": {
            "SUPABASE_ANON_KEY": {
                "source": "secret_ref",
                "value": "anon-key-value",
                "secret_ref": "project/example-default/supabase_anon_key",
            }
        }
    }
    scoped_mock.assert_called_once()


def test_project_get_runtime_values_requires_keys_argument() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {}
        secret_refs = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "test"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with pytest.raises(ValueError, match="requires non-empty 'keys'"):
            execute_agent_tool(
                session=None,  # type: ignore[arg-type]
                settings=SimpleNamespace(secrets_encryption_key=""),
                tenant_id="example",
                project_id="example-default",
                run_id="run-1",
                issue_key="MAB-1",
                stage="test",
                tool_name="project.get_runtime_values",
                tool_args={},
            )


def test_project_list_runtime_keys_combines_environment_and_secret_refs() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"SUPABASE_URL": "https://example.supabase.co"}
        secret_refs = {"SUPABASE_ANON_KEY": "project/example-default/supabase_anon_key"}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        payload = execute_agent_tool(
            session=None,  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="example",
            project_id="example-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="dev",
            tool_name="project.list_runtime_keys",
            tool_args={},
        )

    assert payload == {
        "keys": ["SUPABASE_ANON_KEY", "SUPABASE_URL"],
        "environment_keys": ["SUPABASE_URL"],
        "secret_ref_keys": ["SUPABASE_ANON_KEY"],
    }


def test_project_request_runtime_values_sends_discord_notification() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {}
        secret_refs = {}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "test"
        issue_key = "MAB-1"
        run_id = "run-42"
        repo_dir = Path("/tmp/repo")

    fake_send_result = SimpleNamespace(sent=True, reason="sent", channel_id="123")
    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools.send_tenant_discord_message", return_value=fake_send_result) as send_mock,
    ):
        payload = execute_agent_tool(
            session=object(),  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="example",
            project_id="example-default",
            run_id="run-42",
            issue_key="MAB-1",
            stage="test",
            tool_name="project.request_runtime_values",
            tool_args={"keys": ["SUPABASE_URL", "SUPABASE_ANON_KEY"], "reason": "Needed for iOS auth tests"},
        )

    assert payload == {
        "requested_keys": ["SUPABASE_URL", "SUPABASE_ANON_KEY"],
        "reason": "Needed for iOS auth tests",
        "notification_sent": True,
        "notification_reason": "sent",
        "channel_id": "123",
    }
    send_mock.assert_called_once()
