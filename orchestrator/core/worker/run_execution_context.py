from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.projects.policy import resolve_effective_policy
from orchestrator.core.worker.run_lifecycle import resolve_project_for_run
from orchestrator.storage.models import Project, Run, Tenant


@dataclass(frozen=True)
class RunExecutionPolicyContext:
    project: Project | None
    effective_policy: dict[str, Any]


def effective_policy_for_project(*, tenant: Tenant, project: Project | None) -> dict[str, Any]:
    project_overrides = project.policy_overrides if project is not None else {}
    return resolve_effective_policy(
        tenant_policy=tenant.policy_config,
        project_overrides=project_overrides,
    )


def resolve_run_execution_policy_context(
    session: Session,
    *,
    tenant: Tenant,
    run: Run,
) -> RunExecutionPolicyContext:
    project = resolve_project_for_run(session, run=run)
    return RunExecutionPolicyContext(
        project=project,
        effective_policy=effective_policy_for_project(tenant=tenant, project=project),
    )
