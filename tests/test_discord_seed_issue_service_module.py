from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from orchestrator.api.discord.seed.issue_service import seed_issues_with_codex
from orchestrator.tools.jira_oauth import JiraIssueBulkCreateResult, JiraIssueCreateResult


def test_seed_issues_scopes_allowed_project_keys() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    with __import__("pytest").raises(HTTPException) as exc_ctx:
        seed_issues_with_codex(
            session=MagicMock(),
            tenant=tenant,
            prompt_markdown="seed issues",
            force_issue_keys=None,
            allow_create=True,
            scoped_project_keys=["GP"],
            codex_working_dir="/tmp",
            tenant_project_keys_fn=lambda **_kwargs: ["GP", "YANA"],
            get_settings_fn=lambda: SimpleNamespace(),
            build_codex_runtime_fn=lambda **_kwargs: object(),
            plan_seed_issues_with_codex_fn=lambda **_kwargs: {
                "project_key": "YANA",
                "issues": [],
            },
            codex_runtime_error_type=RuntimeError,
            build_seed_issue_description_fn=lambda **_kwargs: "",
            issue_key_pattern=__import__("re").compile(r"^[A-Z]+-\d+$"),
            tenant_jira_oauth_context_fn=lambda **_kwargs: {},
            select_seed_match_fn=lambda **_kwargs: None,
        )
    assert exc_ctx.value.status_code == 409
    assert "unsupported Jira project key 'YANA'" in str(exc_ctx.value.detail)


def test_seed_issues_preserves_skill_output_without_mutation() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    captured_issues: list = []

    class _FakeClient:
        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issues_bulk(self, **kwargs):  # type: ignore[no-untyped-def]
            captured_issues.extend(kwargs["issues"])
            return JiraIssueBulkCreateResult(created=[JiraIssueCreateResult(key="GP-1", issue_id="1")], errors=[])

    message, data = seed_issues_with_codex(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_codex_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_codex_fn=lambda **_kwargs: {
            "project_key": "GP",
            "issues": [
                {
                    "summary": "Build iOS app shell",
                    "objective": "Build iOS app shell and nav",
                    "scope_in": ["iOS launch flow"],
                    "scope_out": ["Android"],
                    "acceptance_criteria": ["Launch works"],
                    "how_to_test": ["Run xcodebuild"],
                    "nfr_intent": "MVP",
                    "dependencies": [],
                    "risks": [],
                    "labels": ["Mobile", "iOS-App"],
                    "issue_type": "Task",
                }
            ],
        },
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
    assert data["created_issue_keys"] == ["GP-1"]
    assert captured_issues[0].summary == "Build iOS app shell"
    assert captured_issues[0].labels == ["Mobile", "iOS-App"]
    assert captured_issues[0].issue_type == "Task"


def test_seed_issues_allows_noncanonical_issue_type_passthrough() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-a")
    captured_issues: list = []

    class _FakeClient:
        def search_issues_by_jql(self, **_kwargs):  # type: ignore[no-untyped-def]
            return []

        def create_issues_bulk(self, **kwargs):  # type: ignore[no-untyped-def]
            captured_issues.extend(kwargs["issues"])
            return JiraIssueBulkCreateResult(created=[JiraIssueCreateResult(key="GP-2", issue_id="2")], errors=[])

    message, data = seed_issues_with_codex(
        session=MagicMock(),
        tenant=tenant,
        prompt_markdown="seed issues",
        force_issue_keys=None,
        allow_create=True,
        scoped_project_keys=["GP"],
        codex_working_dir="/tmp",
        tenant_project_keys_fn=lambda **_kwargs: ["GP"],
        get_settings_fn=lambda: SimpleNamespace(),
        build_codex_runtime_fn=lambda **_kwargs: object(),
        plan_seed_issues_with_codex_fn=lambda **_kwargs: {
            "project_key": "GP",
            "issues": [
                {
                    "summary": "Build iOS app shell",
                    "objective": "Build iOS app shell and nav",
                    "scope_in": [],
                    "scope_out": [],
                    "acceptance_criteria": [],
                    "how_to_test": [],
                    "nfr_intent": "MVP",
                    "dependencies": [],
                    "risks": [],
                    "labels": [],
                    "issue_type": "task",
                }
            ],
        },
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
    assert data["created_issue_keys"] == ["GP-2"]
    assert captured_issues[0].issue_type == "task"
