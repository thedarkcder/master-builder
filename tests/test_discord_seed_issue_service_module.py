from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from orchestrator.api.discord.seed.issue_service import seed_issues_with_codex
from orchestrator.tools.jira_oauth import JiraIssueCreateResult, JiraOAuthError


def _seed_payload(*, project_key: str = "GP", parent_issue_type: str = "Story", child_count: int = 1) -> dict:
    children = [
        {
            "summary": "Instrument checkout retry telemetry",
            "issue_type": "Sub-task",
            "behavior_slice": "Track retry attempts and recovery outcomes.",
            "technical_objective": "Emit bounded retry telemetry from checkout recovery flow.",
            "implementation_plan": ["Add retry attempt events", "Capture terminal recovery outcome"],
            "technical_dependencies": ["Telemetry schema review"],
            "risks": ["Event volume could be noisy"],
            "how_to_test": ["Run checkout retry integration test"],
            "done_criteria": ["Retry metrics appear in analytics dashboard"],
            "labels": ["engineering"],
        }
    ]
    if child_count > 1:
        children.append(
            {
                "summary": "Persist checkout recovery UI state",
                "issue_type": "Sub-task",
                "behavior_slice": "Keep customer context while recovery decisions change.",
                "technical_objective": "Persist retry state across view transitions.",
                "implementation_plan": ["Store retry state", "Restore state on render"],
                "technical_dependencies": [],
                "risks": [],
                "how_to_test": ["Run recovery state UI test"],
                "done_criteria": ["State persists during retry flow"],
                "labels": ["engineering"],
            }
        )
    return {
        "project_key": project_key,
        "parent_issue": {
            "summary": "Improve checkout recovery",
            "issue_type": parent_issue_type,
            "objective": "Reduce failed checkouts from transient errors.",
            "user_value": "Customers can complete checkout after a recoverable failure.",
            "recommendation": "Ship a tighter retry and fallback experience.",
            "scope_in": ["Retry UX", "Checkout telemetry"],
            "scope_out": ["Payments provider migration"],
            "acceptance_criteria": ["Customers can retry without losing cart state"],
            "ui_references": ["Figma: checkout-recovery-v2"],
            "risks": ["Telemetry coverage is incomplete"],
            "dependencies": ["Design copy approval"],
            "open_questions": [],
            "success_outcomes": ["Reduce recoverable checkout drop-off"],
            "labels": ["product"],
        },
        "engineering_children": children,
        "questions": [],
    }


def test_seed_issues_scopes_allowed_project_keys() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    with __import__("pytest").raises(HTTPException) as exc_ctx:
        seed_issues_with_codex(
            session=MagicMock(),
            tenant=tenant,
            prompt_markdown="seed issues",
            scoped_project_id="project-a",
            force_issue_keys=None,
            allow_create=True,
            scoped_project_keys=["GP"],
            codex_working_dir="/tmp",
            tenant_project_keys_fn=lambda **_kwargs: ["GP", "example"],
            get_settings_fn=lambda: SimpleNamespace(),
            build_codex_runtime_fn=lambda **_kwargs: object(),
            plan_seed_issues_with_codex_fn=lambda **_kwargs: _seed_payload(project_key="example"),
            codex_runtime_error_type=RuntimeError,
            build_seed_issue_description_fn=lambda **_kwargs: "",
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_jira_oauth_context_fn=lambda **_kwargs: {},
            select_seed_match_fn=lambda **_kwargs: None,
        )
    assert exc_ctx.value.status_code == 409
    assert "unsupported Jira project key 'example'" in str(exc_ctx.value.detail)


def test_seed_issues_creates_parent_and_engineering_child() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task", "Issue"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            issue = kwargs["issue"]
            if issue.parent_issue_key:
                return JiraIssueCreateResult(key="GP-2", issue_id="2")
            return JiraIssueCreateResult(key="GP-1", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}

    message, data = seed_issues_with_codex(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_codex_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_codex_fn=lambda **_kwargs: _seed_payload(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert "Issue upsert complete." in message
    assert data["parent_issue_key"] == "GP-1"
    assert data["created_parent"] == "GP-1"
    assert data["created_children"] == ["GP-2"]
    assert data["created_issue_keys"] == ["GP-1", "GP-2"]
    assert data["children_sync_status"] == "children_current"
    assert created[0].issue_type == "Story"
    assert created[1].issue_type == "Sub-task"
    assert created[1].parent_issue_key == "GP-1"


def test_seed_issues_falls_back_to_linked_task_when_subtasks_unavailable() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []
    linked: list[tuple[str, str]] = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            issue = kwargs["issue"]
            created.append(issue)
            if issue.summary == "Improve checkout recovery":
                return JiraIssueCreateResult(key="GP-10", issue_id="10")
            if issue.parent_issue_key:
                raise JiraOAuthError("Subtask issue type is not available for project GP")
            return JiraIssueCreateResult(key="GP-11", issue_id="11")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **kwargs):  # type: ignore[no-untyped-def]
            linked.append((kwargs["inward_issue_key"], kwargs["outward_issue_key"]))
            return {}

    message, data = seed_issues_with_codex(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_codex_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_codex_fn=lambda **_kwargs: _seed_payload(),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert "Issue upsert complete." in message
    assert data["created_children"] == ["GP-11"]
    assert created[1].issue_type == "Sub-task"
    assert created[2].issue_type == "Task"
    assert linked == [("GP-11", "GP-10")]


def test_seed_issues_with_incomplete_oauth_context_returns_controlled_502() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    with __import__("pytest").raises(HTTPException) as exc_ctx:
        seed_issues_with_codex(
            session=MagicMock(),
            tenant=tenant,
            prompt_markdown="seed issues",
            scoped_project_id="project-a",
            force_issue_keys=None,
            allow_create=True,
            scoped_project_keys=["GP"],
            codex_working_dir="/tmp",
            tenant_project_keys_fn=lambda **_kwargs: ["GP"],
            get_settings_fn=lambda: SimpleNamespace(),
            build_codex_runtime_fn=lambda **_kwargs: object(),
            plan_seed_issues_with_codex_fn=lambda **_kwargs: _seed_payload(),
            codex_runtime_error_type=RuntimeError,
            build_seed_issue_description_fn=lambda **_kwargs: {},
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_jira_oauth_context_fn=lambda **_kwargs: {"access_token": "tok-only"},
            select_seed_match_fn=lambda **_kwargs: None,
        )
    assert exc_ctx.value.status_code == 502
    assert str(exc_ctx.value.detail) == "Failed to seed Jira issues: Jira OAuth context is incomplete"
    assert "tok-only" not in str(exc_ctx.value.detail)


def test_seed_issues_normalizes_blank_parent_issue_type_to_project_supported_story() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Story", "Task", "Issue"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            issue = kwargs["issue"]
            if issue.parent_issue_key:
                return JiraIssueCreateResult(key="GP-2", issue_id="2")
            return JiraIssueCreateResult(key="GP-1", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}

    _, data = seed_issues_with_codex(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_codex_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_codex_fn=lambda **_kwargs: _seed_payload(parent_issue_type=""),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert data["created_parent"] == "GP-1"
    assert created[0].issue_type == "Story"


def test_seed_issues_normalizes_multi_child_parent_to_epic_when_available() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    created: list = []

    class _FakeClient:
        def list_project_issue_types_for_create(self, **_kwargs):  # type: ignore[no-untyped-def]
            return ["Epic", "Story", "Task"]

        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issue(self, **kwargs):  # type: ignore[no-untyped-def]
            created.append(kwargs["issue"])
            issue = kwargs["issue"]
            if issue.parent_issue_key:
                return JiraIssueCreateResult(key=f"GP-{len(created)}", issue_id=str(len(created)))
            return JiraIssueCreateResult(key="GP-1", issue_id="1")

        def update_issue_fields(self, **_kwargs):  # type: ignore[no-untyped-def]
            return None

        def add_issue_link(self, **_kwargs):  # type: ignore[no-untyped-def]
            return {}

    _, data = seed_issues_with_codex(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        scoped_project_id="project-a",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_codex_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_codex_fn=lambda **_kwargs: _seed_payload(parent_issue_type="Feature", child_count=2),
        codex_runtime_error_type=RuntimeError,
        build_seed_issue_description_fn=lambda **_kwargs: {},
        issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
        tenant_jira_oauth_context_fn=lambda **_kwargs: {
            "client": _FakeClient(),
            "access_token": "token",
            "connection": SimpleNamespace(cloud_id="cloud-1", site_url="https://example.atlassian.net"),
        },
        select_seed_match_fn=lambda **_kwargs: None,
    )

    assert data["created_parent"] == "GP-1"
    assert created[0].issue_type == "Epic"
