from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.agent_tools import allowed_tools_for_stage, execute_agent_tool
from orchestrator.tools.github_app import PullRequestSummary


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
    assert "knowledge.exact_read" in tools
    assert "knowledge.read" in tools
    assert "project.list_runtime_keys" in tools
    assert "project.get_runtime_values" in tools
    assert "run.request_human_input" in tools


def test_allowed_tools_for_stage_dev_contains_human_input_request_tool() -> None:
    tools = allowed_tools_for_stage("dev")
    assert "run.request_human_input" in tools


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


def test_run_request_human_input_creates_request() -> None:
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
        issue_key = "GP-122"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    fake_run = SimpleNamespace(run_id="run-1")
    fake_request = SimpleNamespace(
        request_id="request-1",
        request_type="verification_code",
        resume_stage="dev",
        thread_channel_id="thread-1",
        expires_at=SimpleNamespace(isoformat=lambda: "2026-03-13T12:00:00+00:00"),
    )
    fake_session = MagicMock()
    fake_session.get.return_value = fake_run

    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools.create_human_input_request", return_value=fake_request) as create_mock,
    ):
        payload = execute_agent_tool(
            session=fake_session,
            settings=SimpleNamespace(),
            tenant_id="example",
            project_id="example-default",
            run_id="run-1",
            issue_key="GP-122",
            stage="test",
            tool_name="run.request_human_input",
            tool_args={
                "request_type": "verification_code",
                "prompt": "Reply with the 2FA code",
                "instructions": "Use the latest code only.",
                "expected_reply_format": "6 digits",
                "request_context": {"provider": "apple"},
            },
        )

    assert payload == {
        "request_id": "request-1",
        "request_type": "verification_code",
        "resume_stage": "dev",
        "thread_channel_id": "thread-1",
        "expires_at": "2026-03-13T12:00:00+00:00",
    }
    create_mock.assert_called_once()


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


def test_github_push_branch_uses_canonical_run_branch() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"default_branch": "main"}

    class _FakeRun:
        branch = "feature/MAB-1-shared"

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        run = _FakeRun()
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

    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"),
        patch("orchestrator.core.agent_tools.github_client_from_tenant_config", return_value=_FakeGitHubClient()),
        patch("orchestrator.core.agent_tools._run_git", side_effect=_fake_run_git),
    ):
        payload = execute_agent_tool(
            session=SimpleNamespace(flush=lambda: None),  # type: ignore[arg-type]
            settings=None,
            tenant_id="example",
            project_id="example-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="dev",
            tool_name="github.push_branch",
            tool_args={"branch_name": "run/mab-1/run-1"},
        )

    assert payload == {"branch_name": "feature/MAB-1-shared"}
    assert git_calls == [
        (["push", "-u", "origin", "HEAD:feature/MAB-1-shared"], "token-123"),
    ]


def test_github_open_pr_reuses_existing_pull_request() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"default_branch": "staging"}

    class _FakeRun:
        branch = "feature/MAB-1-shared"

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        run = _FakeRun()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    class _FakeGitHubClient:
        def __init__(self) -> None:
            self.find_args: tuple[str, str, str, int] | None = None
            self.create_called = False

        def find_open_pull_request(
            self,
            *,
            repo_full_name: str,
            head_branch: str,
            base_branch: str | None = None,
            limit: int = 100,
        ) -> PullRequestSummary | None:
            self.find_args = (repo_full_name, head_branch, str(base_branch), limit)
            return PullRequestSummary(
                number=42,
                title="MAB-1 existing PR",
                state="open",
                html_url="https://github.com/acme/repo/pull/42",
                head_ref=head_branch,
                base_ref=str(base_branch),
                updated_at="2026-03-15T10:00:00Z",
            )

        def create_pull_request(self, **_kwargs):  # noqa: ANN003, ANN202
            self.create_called = True
            raise AssertionError("create_pull_request should not be called when an open PR exists")

    fake_client = _FakeGitHubClient()
    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"),
        patch("orchestrator.core.agent_tools.github_client_from_tenant_config", return_value=fake_client),
    ):
        payload = execute_agent_tool(
            session=SimpleNamespace(flush=lambda: None),  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="example",
            project_id="example-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="dev",
            tool_name="github.open_pr",
            tool_args={"title": "MAB-1: update"},
        )

    assert payload == {"pr_number": 42, "pr_url": "https://github.com/acme/repo/pull/42"}
    assert fake_client.find_args == ("acme/repo", "feature/MAB-1-shared", "staging", 100)
    assert fake_client.create_called is False


def test_github_open_pr_prefers_remediation_pr_number_when_open() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"default_branch": "staging"}

    class _FakeRun:
        branch = None
        plan = {
            "trigger_context": {
                "source": "github_pr_review_feedback",
                "pr_number": 14,
                "head_ref": "run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
                "base_ref": "main",
            }
        }

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        run = _FakeRun()
        stage = "dev"
        issue_key = "GP-122"
        run_id = "run-remediate-1"
        repo_dir = Path("/tmp/repo")

    class _FakeGitHubClient:
        def __init__(self) -> None:
            self.find_called = False
            self.create_called = False

        def get_pull_request_details(self, *, repo_full_name: str, pr_number: int):  # noqa: ANN201
            assert repo_full_name == "acme/repo"
            assert pr_number == 14
            return SimpleNamespace(
                number=14,
                html_url="https://github.com/acme/repo/pull/14",
                state="open",
                head_ref="run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
                base_ref="main",
                title="GP-122: fix auth bootstrap remediation",
                body="",
                head_sha="abc123",
            )

        def find_open_pull_request(self, **_kwargs):  # noqa: ANN003, ANN202
            self.find_called = True
            return None

        def create_pull_request(self, **_kwargs):  # noqa: ANN003, ANN202
            self.create_called = True
            raise AssertionError("create_pull_request should not be called for open remediation PR")

    fake_client = _FakeGitHubClient()
    fake_session = SimpleNamespace(flush=lambda: None)
    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"),
        patch("orchestrator.core.agent_tools.github_client_from_tenant_config", return_value=fake_client),
    ):
        payload = execute_agent_tool(
            session=fake_session,  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="example",
            project_id="example-default",
            run_id="run-remediate-1",
            issue_key="GP-122",
            stage="dev",
            tool_name="github.open_pr",
            tool_args={"title": "GP-122: remediation"},
        )

    assert payload == {"pr_number": 14, "pr_url": "https://github.com/acme/repo/pull/14"}
    assert fake_client.find_called is False
    assert fake_client.create_called is False


def test_github_open_pr_falls_back_when_remediation_pr_is_closed() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"default_branch": "main"}

    class _FakeRun:
        branch = None
        plan = {
            "trigger_context": {
                "source": "github_pr_review_feedback",
                "pr_number": 14,
                "head_ref": "run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
                "base_ref": "main",
            }
        }

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        run = _FakeRun()
        stage = "dev"
        issue_key = "GP-122"
        run_id = "run-remediate-2"
        repo_dir = Path("/tmp/repo")

    class _FakeGitHubClient:
        def __init__(self) -> None:
            self.find_args: tuple[str, str, str, int] | None = None
            self.create_called = False

        def get_pull_request_details(self, *, repo_full_name: str, pr_number: int):  # noqa: ANN201
            assert repo_full_name == "acme/repo"
            assert pr_number == 14
            return SimpleNamespace(
                number=14,
                html_url="https://github.com/acme/repo/pull/14",
                state="closed",
                head_ref="run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
                base_ref="main",
                title="",
                body="",
                head_sha="abc123",
            )

        def find_open_pull_request(
            self,
            *,
            repo_full_name: str,
            head_branch: str,
            base_branch: str | None = None,
            limit: int = 100,
        ) -> PullRequestSummary | None:
            self.find_args = (repo_full_name, head_branch, str(base_branch), limit)
            return None

        def create_pull_request(self, **_kwargs):  # noqa: ANN003, ANN202
            self.create_called = True
            return SimpleNamespace(number=55, html_url="https://github.com/acme/repo/pull/55")

    fake_client = _FakeGitHubClient()
    fake_session = SimpleNamespace(flush=lambda: None)
    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"),
        patch("orchestrator.core.agent_tools.github_client_from_tenant_config", return_value=fake_client),
    ):
        payload = execute_agent_tool(
            session=fake_session,  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="example",
            project_id="example-default",
            run_id="run-remediate-2",
            issue_key="GP-122",
            stage="dev",
            tool_name="github.open_pr",
            tool_args={"title": "GP-122: remediation"},
        )

    assert payload == {"pr_number": 55, "pr_url": "https://github.com/acme/repo/pull/55"}
    assert fake_client.find_args == (
        "acme/repo",
        "run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
        "main",
        100,
    )
    assert fake_client.create_called is True


def test_github_open_pr_uses_remediation_head_ref_over_stale_run_branch() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"default_branch": "main"}

    class _FakeRun:
        branch = "feature/GP-122-stale"
        plan = {
            "trigger_context": {
                "source": "github_pr_review_feedback",
                "pr_number": 14,
                "head_ref": "run/gp-122/remediation-head",
                "base_ref": "main",
            }
        }

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        run = _FakeRun()
        stage = "dev"
        issue_key = "GP-122"
        run_id = "run-remediate-3"
        repo_dir = Path("/tmp/repo")

    class _FakeGitHubClient:
        def __init__(self) -> None:
            self.find_args: tuple[str, str, str, int] | None = None

        def get_pull_request_details(self, *, repo_full_name: str, pr_number: int):  # noqa: ANN201
            assert repo_full_name == "acme/repo"
            assert pr_number == 14
            return SimpleNamespace(
                number=14,
                html_url="https://github.com/acme/repo/pull/14",
                state="closed",
                head_ref="run/gp-122/remediation-head",
                base_ref="main",
                title="",
                body="",
                head_sha="abc123",
            )

        def find_open_pull_request(
            self,
            *,
            repo_full_name: str,
            head_branch: str,
            base_branch: str | None = None,
            limit: int = 100,
        ) -> PullRequestSummary | None:
            self.find_args = (repo_full_name, head_branch, str(base_branch), limit)
            return None

        def create_pull_request(self, **_kwargs):  # noqa: ANN003, ANN202
            return SimpleNamespace(number=55, html_url="https://github.com/acme/repo/pull/55")

    fake_client = _FakeGitHubClient()
    fake_session = SimpleNamespace(flush=lambda: None)
    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"),
        patch("orchestrator.core.agent_tools.github_client_from_tenant_config", return_value=fake_client),
    ):
        payload = execute_agent_tool(
            session=fake_session,  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="example",
            project_id="example-default",
            run_id="run-remediate-3",
            issue_key="GP-122",
            stage="dev",
            tool_name="github.open_pr",
            tool_args={"title": "GP-122: remediation"},
        )

    assert payload == {"pr_number": 55, "pr_url": "https://github.com/acme/repo/pull/55"}
    assert fake_client.find_args == (
        "acme/repo",
        "run/gp-122/remediation-head",
        "main",
        100,
    )
    assert _FakeContext.run.branch == "run/gp-122/remediation-head"


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


def test_resolve_context_prefers_run_worktree_for_run_scoped_tools() -> None:
    from orchestrator.storage.models import Project, Tenant

    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        tenant_id = "example"
        project_id = "example-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        is_archived = False

    class _FakeQuery:
        def filter(self, *_args, **_kwargs):  # noqa: ANN001
            return self

        def first(self):
            return None

    class _FakeSession:
        def get(self, cls, key):  # noqa: ANN001
            if cls is Tenant and key == "example":
                return _FakeTenant()
            if cls is Project and key == "example-default":
                return _FakeProject()
            return None

        def query(self, _cls):  # noqa: ANN001
            return _FakeQuery()

    settings = SimpleNamespace(
        project_repo_checkout_base_dir="/tmp/workdirs",
        worker_workspace_key="worker-a",
    )

    with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
        with patch(
            "orchestrator.core.agent_tools.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="ok", stderr=""),
        ) as run_mock:
            execute_agent_tool(
                session=_FakeSession(),  # type: ignore[arg-type]
                settings=settings,
                tenant_id="example",
                project_id="example-default",
                run_id="run-123",
                issue_key="MAB-1",
                stage="test",
                tool_name="repo.read",
                tool_args={"command": "git status -sb"},
            )

    assert (
        run_mock.call_args.kwargs["cwd"]
        == "/tmp/workdirs/example/example-default/runs/run-123/workspaces/worker-a/repo"
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


def test_knowledge_exact_read_returns_stored_asset_payload() -> None:
    class _FakeTenant:
        tenant_id = "example"
        github_config = {}
        policy_config = {}
        jira_config = {}

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

    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch(
            "orchestrator.core.agent_tools.exact_read_knowledge_source",
            return_value={"ok": True, "connector": "stored_asset", "layer": "exact_read"},
        ) as exact_read_mock,
    ):
        payload = execute_agent_tool(
            session=object(),  # type: ignore[arg-type]
            settings=SimpleNamespace(),
            tenant_id="example",
            project_id="example-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="pm",
            tool_name="knowledge.exact_read",
            tool_args={"asset_id": "kb-1"},
        )

    assert payload == {"ok": True, "connector": "stored_asset", "layer": "exact_read"}
    exact_read_mock.assert_called_once()
