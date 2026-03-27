from __future__ import annotations

import re

from orchestrator.api.discord.ingress import jira_runtime
from orchestrator.api.discord.seed.description import build_seed_issue_description
from orchestrator.api.discord.seed.issue_service import (
    seed_issues_with_codex as _seed_issues_with_codex_impl,
    validate_seed_followup_context as _validate_seed_followup_context_impl,
)
from orchestrator.api.discord.seed.matching import select_seed_match
from orchestrator.api.discord.ask.context import tenant_project_keys
from orchestrator.core.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.codex_agents import plan_seed_issues_with_codex
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime as _legacy_build_codex_runtime
from orchestrator.core.config import get_settings

ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
build_codex_runtime = _legacy_build_codex_runtime


def seed_issues_with_codex(
    *,
    session,
    tenant,
    prompt_markdown: str,
    scoped_project_id: str | None = None,
    force_issue_keys: list[str] | None = None,
    allow_create: bool = True,
    scoped_project_keys: list[str] | None = None,
    codex_working_dir: str = "",
) -> tuple[str, dict]:  # noqa: ANN001
    return _seed_issues_with_codex_impl(
        session=session,
        tenant=tenant,
        prompt_markdown=prompt_markdown,
        scoped_project_id=scoped_project_id,
        force_issue_keys=force_issue_keys,
        allow_create=allow_create,
        scoped_project_keys=scoped_project_keys,
        codex_working_dir=codex_working_dir,
        tenant_project_keys_fn=tenant_project_keys,
        get_settings_fn=get_settings,
        build_codex_runtime_fn=lambda **kwargs: build_runtime_for_selector(
            session=kwargs.get("session"),
            settings=kwargs["settings"],
            tenant_id=getattr(tenant, "tenant_id", None),
            project_id=scoped_project_id,
            selector="discord.issue_seed",
        ),
        plan_seed_issues_with_codex_fn=plan_seed_issues_with_codex,
        codex_runtime_error_type=CodexRuntimeError,
        build_seed_issue_description_fn=build_seed_issue_description,
        issue_key_pattern=ISSUE_KEY_PATTERN,
        tenant_jira_oauth_context_fn=jira_runtime.tenant_jira_oauth_context,
        select_seed_match_fn=select_seed_match,
    )



def validate_seed_followup_context(*, session, tenant, context: dict) -> tuple[bool, str | None]:  # noqa: ANN001
    return _validate_seed_followup_context_impl(
        session=session,
        tenant=tenant,
        context=context,
        get_settings_fn=get_settings,
        tenant_jira_oauth_context_fn=jira_runtime.tenant_jira_oauth_context,
    )
