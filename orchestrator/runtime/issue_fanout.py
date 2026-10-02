from __future__ import annotations

import re

from sqlalchemy import select

from orchestrator.api.atlassian_oauth.connection_service import (
    tenant_atlassian_oauth_context,
)
from orchestrator.core.runtime.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.runtime.agents import (
    plan_pm_parent_issues_with_runtime,
    plan_seed_issues_with_runtime,
)
from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.config import get_settings
from orchestrator.core.decision.types import tenant_jira_project_keys
from orchestrator.core.issue_fanout.description import build_seed_issue_description
from orchestrator.core.issue_fanout.matching import select_seed_match
from orchestrator.core.issue_fanout.service import (
    seed_issues_with_runtime as _seed_issues_with_runtime,
    seed_parent_issues_with_runtime as _seed_parent_issues_with_runtime,
    validate_seed_followup_context as _validate_seed_followup_context,
)
from orchestrator.core.workflow.attempt_ref import WorkflowAttemptRef
from orchestrator.storage.models import Project, Tenant

ISSUE_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


def tenant_project_keys(*, session, tenant: Tenant) -> list[str]:  # noqa: ANN001
    project_keys = [
        key
        for key in session.execute(
            select(Project.jira_project_key)
            .where(
                Project.tenant_id == tenant.tenant_id, Project.is_archived.is_(False)
            )
            .order_by(Project.created_at)
        ).scalars()
        if str(key or "").strip()
    ]
    if project_keys:
        return [str(key).strip().upper() for key in project_keys]
    return [key.upper() for key in tenant_jira_project_keys(tenant)]


def build_issue_seed_runtime(
    *,
    session,
    settings,
    tenant_id: str | None = None,
    project_id: str | None = None,
    selector: str = "issue_seed",
):  # noqa: ANN001
    return build_runtime_for_selector(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
        selector=selector,
    )


def seed_issues_with_runtime(
    *,
    session,
    tenant,
    prompt_markdown: str,
    scoped_project_id: str | None = None,
    force_issue_keys: list[str] | None = None,
    allow_create: bool = True,
    allow_empty_children: bool = False,
    scoped_project_keys: list[str] | None = None,
    codex_working_dir: str = "",
    pm_status: str | None = None,
    planning_package: dict | None = None,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    attempt_ref: WorkflowAttemptRef | None = None,
) -> tuple[str, dict]:  # noqa: ANN001
    return _seed_issues_with_runtime(
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
        build_runtime_fn=lambda **kwargs: build_issue_seed_runtime(
            session=kwargs.get("session"),
            settings=kwargs["settings"],
            tenant_id=getattr(tenant, "tenant_id", None),
            project_id=scoped_project_id,
            selector="issue_seed",
        ),
        plan_seed_issues_with_runtime_fn=plan_seed_issues_with_runtime,
        codex_runtime_error_type=CodexRuntimeError,
        build_seed_issue_description_fn=build_seed_issue_description,
        issue_key_pattern=ISSUE_KEY_PATTERN,
        tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context,
        select_seed_match_fn=select_seed_match,
        allow_empty_children=allow_empty_children,
        pm_status=pm_status,
        planning_package=planning_package,
        workflow_id=workflow_id,
        operation_id=operation_id,
        attempt_ref=attempt_ref,
    )


def seed_parent_issues_with_runtime(
    *,
    session,
    tenant,
    prompt_markdown: str,
    scoped_project_id: str | None = None,
    force_issue_keys: list[str] | None = None,
    allow_create: bool = True,
    scoped_project_keys: list[str] | None = None,
    codex_working_dir: str = "",
    pm_status: str | None = None,
    pm_interview_notes_json: dict | None = None,
) -> tuple[str, dict]:  # noqa: ANN001
    return _seed_parent_issues_with_runtime(
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
        build_runtime_fn=lambda **kwargs: build_issue_seed_runtime(
            session=kwargs.get("session"),
            settings=kwargs["settings"],
            tenant_id=getattr(tenant, "tenant_id", None),
            project_id=scoped_project_id,
            selector="pm_seed",
        ),
        plan_pm_parent_issues_with_runtime_fn=plan_pm_parent_issues_with_runtime,
        codex_runtime_error_type=CodexRuntimeError,
        issue_key_pattern=ISSUE_KEY_PATTERN,
        tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context,
        select_seed_match_fn=select_seed_match,
        pm_status=pm_status,
        pm_interview_notes_json=pm_interview_notes_json,
    )


def validate_seed_followup_context(
    *, session, tenant, context: dict
) -> tuple[bool, str | None]:  # noqa: ANN001
    return _validate_seed_followup_context(
        session=session,
        tenant=tenant,
        context=context,
        get_settings_fn=get_settings,
        tenant_atlassian_oauth_context_fn=tenant_atlassian_oauth_context,
    )
