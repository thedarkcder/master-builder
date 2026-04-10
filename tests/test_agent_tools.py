from __future__ import annotations

from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from orchestrator.core.agent_tools import (
    allowed_tools_for_stage,
    execute_agent_tool,
    list_implemented_tools,
    tool_catalog_for_stage,
)
from orchestrator.core.knowledge_base import KnowledgeEmbeddingAccessMode
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.storage.models import DecisionCycle
from orchestrator.tools.github_app import PullRequestSummary


def test_allowed_tools_for_stage_dev_contains_github_and_jira() -> None:
    tools = allowed_tools_for_stage("dev")
    assert "github.create_branch" in tools
    assert "github.open_pr" in tools
    assert "jira.comment" in tools
    assert "project.check_runtime_bindings" in tools
    assert "exec.run_install" in tools


def test_allowed_tools_for_stage_pm_contains_evidence_tools() -> None:
    tools = allowed_tools_for_stage("pm")
    assert "jira.get_issue" in tools
    assert "decision.read_state" in tools
    assert "knowledge.exact_read" in tools
    assert "knowledge.read" in tools
    assert "project.list_installs" in tools
    assert "project.check_runtime_bindings" in tools
    assert "project.request_install" in tools
    assert "run.request_human_input" in tools


def test_allowed_tools_for_stage_dev_contains_human_input_request_tool() -> None:
    tools = allowed_tools_for_stage("dev")
    assert "run.request_human_input" in tools


def test_public_discord_stages_are_read_only_and_secret_safe() -> None:
    ask_tools = allowed_tools_for_stage("discord_ask_answer")
    persona_tools = allowed_tools_for_stage("discord_voice_room_persona")
    router_tools = allowed_tools_for_stage("voice_entry_router")

    assert "jira.comment" not in ask_tools
    assert "run.request_human_input" not in ask_tools
    assert "project.check_runtime_bindings" not in ask_tools
    assert "project.list_installs" not in ask_tools
    assert "project.request_install" not in ask_tools
    assert "exec.run_install" not in ask_tools
    assert "project.check_runtime_bindings" not in persona_tools
    assert "project.list_installs" not in router_tools


def test_tool_catalog_descriptions_explain_usage() -> None:
    tools = list_implemented_tools()
    assert tools
    by_name = {str(item["tool_name"]): str(item["description"]) for item in tools}

    for tool_name, description in by_name.items():
        assert description
        assert description != "Implemented governed tool."
        assert "Use " in description, f"{tool_name} description should explain when to use the tool"

    runtime_description = by_name["project.check_runtime_bindings"]
    assert "verify presence" in runtime_description
    assert "never returns the underlying values" in runtime_description


def test_tool_catalog_for_stage_returns_structured_entries() -> None:
    tools = tool_catalog_for_stage("test")
    assert tools

    runtime_tool = next(item for item in tools if item["tool_name"] == "project.check_runtime_bindings")
    assert runtime_tool["category"] == "project"
    assert "explicitly named project bindings" in str(runtime_tool["description"])
    assert "test" in runtime_tool["stages"]


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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
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


def test_run_request_human_input_creates_request() -> None:
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
        issue_key = "GP-122"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    fake_run = SimpleNamespace(run_id="run-1")
    fake_request = SimpleNamespace(
        request_id="request-1",
        request_type="verification_code",
        source_stage="test",
        workflow_id="workflow-1",
        checkpoint_id="checkpoint-1",
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
            tenant_id="route25",
            project_id="route25-default",
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
        "source_stage": "test",
        "workflow_id": "workflow-1",
        "checkpoint_id": "checkpoint-1",
        "thread_channel_id": "thread-1",
        "expires_at": "2026-03-13T12:00:00+00:00",
    }
    create_mock.assert_called_once()


def test_run_request_human_input_rejects_missing_request_type() -> None:
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
        issue_key = "GP-125"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    fake_session = MagicMock()
    fake_session.get.return_value = SimpleNamespace(run_id="run-1")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with pytest.raises(ValueError, match="requires non-empty 'request_type'"):
            execute_agent_tool(
                session=fake_session,
                settings=SimpleNamespace(),
                tenant_id="route25",
                project_id="route25-default",
                run_id="run-1",
                issue_key="GP-125",
                stage="pm",
                tool_name="run.request_human_input",
                tool_args={"prompt": "Need a clarification"},
            )


def test_run_request_human_input_rejects_top_level_questions_payload() -> None:
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
        issue_key = "GP-125"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    fake_session = MagicMock()
    fake_session.get.return_value = SimpleNamespace(run_id="run-1")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with pytest.raises(ValueError, match="requires non-empty 'prompt'"):
            execute_agent_tool(
                session=fake_session,
                settings=SimpleNamespace(),
                tenant_id="route25",
                project_id="route25-default",
                run_id="run-1",
                issue_key="GP-125",
                stage="pm",
                tool_name="run.request_human_input",
                tool_args={
                    "questions": [
                        {
                            "id": "sync_failure_policy",
                            "question": "If StoreKit status is indeterminate, should gating fail closed?",
                        }
                    ]
                },
            )


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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
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


def test_github_push_branch_uses_canonical_run_branch() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
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
            tenant_id="route25",
            project_id="route25-default",
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
        tenant_id = "route25"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
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
            tenant_id="route25",
            project_id="route25-default",
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
        tenant_id = "route25"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"default_branch": "staging"}

    class _FakeRun:
        branch = None
        plan = ExecutionSnapshot.empty(
            trigger_context={
                "source": "github_pr_review_feedback",
                "pr_number": 14,
                "head_ref": "run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
                "base_ref": "main",
            }
        ).dump()

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
            tenant_id="route25",
            project_id="route25-default",
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
        tenant_id = "route25"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"default_branch": "main"}

    class _FakeRun:
        branch = None
        plan = ExecutionSnapshot.empty(
            trigger_context={
                "source": "github_pr_review_feedback",
                "pr_number": 14,
                "head_ref": "run/gp-122/6fc2dd62-c996-468f-84ba-3ac052c08703",
                "base_ref": "main",
            }
        ).dump()

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
            tenant_id="route25",
            project_id="route25-default",
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
        tenant_id = "route25"
        github_config = {"installation_id": "12345"}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"default_branch": "main"}

    class _FakeRun:
        branch = "feature/GP-122-stale"
        plan = ExecutionSnapshot.empty(
            trigger_context={
                "source": "github_pr_review_feedback",
                "pr_number": 14,
                "head_ref": "run/gp-122/remediation-head",
                "base_ref": "main",
            }
        ).dump()

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
            tenant_id="route25",
            project_id="route25-default",
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
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
        with patch("orchestrator.core.agent_tools._ensure_repo_checkout_exists"):
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


def test_decision_read_state_does_not_require_repo_checkout() -> None:
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
            tenant_id="route25",
            project_id="route25-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="decision_planner",
            tool_name="decision.read_state",
            tool_args={},
        )

    assert payload == {"ok": True}
    execute_mock.assert_called_once()


def test_decision_read_state_returns_case_cycle_answers_and_recent_evidence() -> None:
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
        stage = "decision_planner"
        issue_key = "GP-124"
        run_id = "run-1"
        repo_dir = Path("/tmp/missing-repo")

    case = SimpleNamespace(
        case_id="case-1",
        state="blocked",
        classification="both",
        blocked_reason="missing_decision_input",
        active_cycle_id="cycle-1",
        metadata_json={"source": "discord"},
    )
    cycle = SimpleNamespace(
        cycle_id="cycle-1",
        status="open",
        classification="both",
        reason="Need one product answer",
        question_set_json=[{"question_id": "q1", "question": "Which user segment comes first?"}],
        unresolved_question_ids_json=["q1"],
        metadata_json={"asked_via": "discord"},
    )
    answer = SimpleNamespace(
        question_id="q0",
        question_text="What problem are we solving?",
        status="answered",
        normalized_answer="Reduce drop-off during onboarding",
        metadata_json={"notes": "Captured from voice note"},
        updated_at=SimpleNamespace(isoformat=lambda: "2026-03-22T22:00:00+00:00"),
    )
    evidence = SimpleNamespace(
        evidence_id="ev-1",
        source_transport="discord",
        source_ref="message-123",
        raw_text="We need to cut onboarding friction.",
        question_ids_json=["q0"],
        normalized_answers_json={"problem": "Cut onboarding friction"},
        created_at=SimpleNamespace(isoformat=lambda: "2026-03-22T22:01:00+00:00"),
    )

    session = MagicMock()
    session.get.side_effect = lambda cls, key: cycle if cls is DecisionCycle and key == "cycle-1" else None
    session.execute.side_effect = [
        SimpleNamespace(scalar_one_or_none=lambda: case),
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [answer])),
        SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [evidence])),
    ]

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        payload = execute_agent_tool(
            session=session,  # type: ignore[arg-type]
            settings=None,
            tenant_id="route25",
            project_id="route25-default",
            run_id="run-1",
            issue_key="GP-124",
            stage="decision_planner",
            tool_name="decision.read_state",
            tool_args={},
        )

    assert payload["issue_key"] == "GP-124"
    assert payload["case"]["case_id"] == "case-1"
    assert payload["case"]["classification"] == "both"
    assert payload["active_cycle"]["cycle_id"] == "cycle-1"
    assert payload["active_cycle"]["questions"] == [{"question_id": "q1", "question": "Which user segment comes first?"}]
    assert payload["answers"][0]["question_id"] == "q0"
    assert payload["answers"][0]["notes"] == "Captured from voice note"
    assert payload["recent_evidence"][0]["evidence_id"] == "ev-1"
    assert payload["recent_evidence"][0]["normalized_answers"] == {"problem": "Cut onboarding friction"}


def test_repo_read_requires_repo_checkout() -> None:
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
        repo_dir = Path("/tmp/missing-repo")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with pytest.raises(ValueError, match="Repository checkout missing"):
            execute_agent_tool(
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


def test_resolve_context_prefers_run_worktree_for_run_scoped_tools() -> None:
    from orchestrator.storage.models import Project, Tenant

    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        tenant_id = "route25"
        project_id = "route25-default"
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
            if cls is Tenant and key == "route25":
                return _FakeTenant()
            if cls is Project and key == "route25-default":
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
                tenant_id="route25",
                project_id="route25-default",
                run_id="run-123",
                issue_key="MAB-1",
                stage="test",
                tool_name="repo.read",
                tool_args={"command": "git status -sb"},
            )

    assert (
        run_mock.call_args.kwargs["cwd"]
        == "/tmp/workdirs/route25/route25-default/runs/run-123/workspaces/worker-a/repo"
    )


def test_project_check_runtime_bindings_reports_environment_and_secret_ref_presence() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"SUPABASE_URL": "https://example.supabase.co"}
        secret_refs = {"SUPABASE_ANON_KEY": "project/route25-default/supabase_anon_key"}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools.check_project_bindings") as bindings_mock,
    ):
        bindings_mock.return_value = [
            SimpleNamespace(key="SUPABASE_URL", present=True, source="environment"),
            SimpleNamespace(key="SUPABASE_ANON_KEY", present=True, source="secret_ref"),
        ]
        payload = execute_agent_tool(
            session=object(),  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key="enc-key"),
            tenant_id="route25",
            project_id="route25-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="dev",
            tool_name="project.check_runtime_bindings",
            tool_args={"keys": ["SUPABASE_URL", "SUPABASE_ANON_KEY"]},
        )

    assert payload == {
        "bindings": [
            {"key": "SUPABASE_URL", "present": True, "source": "environment"},
            {"key": "SUPABASE_ANON_KEY", "present": True, "source": "secret_ref"},
        ]
    }
    bindings_mock.assert_called_once()


def test_project_check_runtime_bindings_requires_keys_argument() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {}
        secret_refs = {"SUPABASE_ANON_KEY": "project/route25-default/supabase_anon_key"}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with pytest.raises(ValueError, match="requires non-empty 'keys'"):
            execute_agent_tool(
                session=None,  # type: ignore[arg-type]
                settings=SimpleNamespace(secrets_encryption_key=""),
                tenant_id="route25",
                project_id="route25-default",
                run_id="run-1",
                issue_key="MAB-1",
                stage="test",
                tool_name="project.check_runtime_bindings",
                tool_args={},
            )


def test_project_list_installs_returns_safe_metadata_only() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {"SUPABASE_URL": "https://example.supabase.co"}
        secret_refs = {"SUPABASE_ANON_KEY": "project/route25-default/supabase_anon_key"}

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    fake_installs = [
        SimpleNamespace(
            install_id="install-1",
            kind="fastlane_lane",
            label="iOS Beta Lane",
            enabled=True,
            config_json={"lane": "beta"},
            binding_names_json=["MATCH_PASSWORD"],
        )
    ]
    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools.list_project_installs", return_value=fake_installs),
    ):
        payload = execute_agent_tool(
            session=object(),  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="route25",
            project_id="route25-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="dev",
            tool_name="project.list_installs",
            tool_args={},
        )

    assert payload == {
        "installs": [
            {
                "install_id": "install-1",
                "kind": "fastlane_lane",
                "label": "iOS Beta Lane",
                "enabled": True,
            }
        ]
    }


def test_project_request_install_creates_request_and_pauses() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
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
        run = SimpleNamespace(run_id="run-42", workflow_id="workflow-1", issue_key="MAB-1")

    fake_request = SimpleNamespace(request_id="request-1", request_kind="project_missing_install", status="pending")
    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools.create_install_request", return_value=fake_request) as request_mock,
    ):
        payload = execute_agent_tool(
            session=object(),  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="route25",
            project_id="route25-default",
            run_id="run-42",
            issue_key="MAB-1",
            stage="test",
            tool_name="project.request_install",
            tool_args={
                "kind": "fastlane_lane",
                "label": "iOS Beta Lane",
                "reason": "Ticket requires Fastlane delivery",
                "suggested_config": {"lane": "beta", "platform": "ios"},
                "required_bindings": ["MATCH_PASSWORD", "FASTLANE_SESSION"],
            },
        )

    assert payload == {
        "request_id": "request-1",
        "request_kind": "project_missing_install",
        "status": "pending",
        "waiting_for_input": True,
        "kind_supported": True,
    }
    request_mock.assert_called_once()


def test_exec_run_install_executes_registered_install() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}

    class _FakeProject:
        project_id = "route25-default"
        github_repository = "https://github.com/acme/repo"
        policy_overrides = {}
        environment = {}
        secret_refs = {}
        tenant_id = "route25"

    class _FakeContext:
        tenant = _FakeTenant()
        project = _FakeProject()
        stage = "dev"
        issue_key = "MAB-1"
        run_id = "run-1"
        repo_dir = Path("/tmp/repo")

    fake_install = SimpleNamespace(
        install_id="install-1",
        tenant_id="route25",
        project_id="route25-default",
        enabled=True,
        kind="fastlane_lane",
        label="iOS Beta Lane",
    )
    fake_result = {
        "install_id": "install-1",
        "kind": "fastlane_lane",
        "label": "iOS Beta Lane",
        "ok": True,
        "exit_code": 0,
        "stdout": "lane complete",
        "stderr": "",
    }
    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch("orchestrator.core.agent_tools.get_project_install", return_value=fake_install),
        patch("orchestrator.core.agent_tools.execute_project_install", return_value=fake_result) as run_mock,
    ):
        payload = execute_agent_tool(
            session=object(),  # type: ignore[arg-type]
            settings=SimpleNamespace(secrets_encryption_key=""),
            tenant_id="route25",
            project_id="route25-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="dev",
            tool_name="exec.run_install",
            tool_args={"install_id": "install-1", "runtime_input": {"message": "ignored"}},
        )

    assert payload == fake_result
    run_mock.assert_called_once()


def test_knowledge_exact_read_returns_stored_asset_payload() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}
        jira_config = {}

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
            tenant_id="route25",
            project_id="route25-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="pm",
            tool_name="knowledge.exact_read",
            tool_args={"asset_id": "kb-1"},
        )

    assert payload == {"ok": True, "connector": "stored_asset", "layer": "exact_read"}
    exact_read_mock.assert_called_once()


def test_knowledge_read_uses_local_only_embeddings_for_background_runs() -> None:
    class _FakeTenant:
        tenant_id = "route25"
        github_config = {}
        policy_config = {}
        jira_config = {}

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

    with (
        patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()),
        patch(
            "orchestrator.core.agent_tools.build_knowledge_prompt_context",
            return_value=SimpleNamespace(text="facts", citations=[]),
        ) as knowledge_mock,
    ):
        payload = execute_agent_tool(
            session=object(),  # type: ignore[arg-type]
            settings=SimpleNamespace(),
            tenant_id="route25",
            project_id="route25-default",
            run_id="run-1",
            issue_key="MAB-1",
            stage="pm",
            tool_name="knowledge.read",
            tool_args={"query": "bundle id"},
        )

    assert payload == {"query": "bundle id", "text": "facts", "citations": []}
    assert knowledge_mock.call_args.kwargs["embedding_access_mode"] is KnowledgeEmbeddingAccessMode.LOCAL_ONLY
