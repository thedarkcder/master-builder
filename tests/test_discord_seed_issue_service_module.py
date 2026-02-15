from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from orchestrator.api.discord.seed.issue_service import seed_issues_with_codex


def test_seed_issues_scopes_allowed_project_keys() -> None:
    tenant = SimpleNamespace()
    with __import__("pytest").raises(HTTPException) as exc_ctx:
        seed_issues_with_codex(
            session=MagicMock(),
            tenant=tenant,
            prompt_markdown="seed issues",
            force_issue_keys=None,
            allow_create=True,
            scoped_project_keys=["GP"],
            tenant_project_keys_fn=lambda **_kwargs: ["GP", "example"],
            get_settings_fn=lambda: SimpleNamespace(),
            build_codex_runtime_fn=lambda **_kwargs: object(),
            plan_seed_issues_with_codex_fn=lambda **_kwargs: {
                "project_key": "example",
                "issues": [],
            },
            codex_runtime_error_type=RuntimeError,
            normalize_seed_issue_scope_fn=lambda value: [],
            normalize_seed_issue_key_fn=lambda value: None,
            normalize_seed_issue_tags_fn=lambda value: [],
            normalize_seed_issue_labels_fn=lambda value: [],
            collect_seed_issue_questions_fn=lambda **_kwargs: [],
            build_seed_issue_description_fn=lambda **_kwargs: "",
            parse_seed_issue_type_fn=lambda _value: "Task",
            tenant_jira_oauth_context_fn=lambda **_kwargs: {},
            select_seed_match_fn=lambda **_kwargs: None,
        )
    assert exc_ctx.value.status_code == 409
    assert "unsupported Jira project key 'example'" in str(exc_ctx.value.detail)
