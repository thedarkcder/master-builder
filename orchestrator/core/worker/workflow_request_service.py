from __future__ import annotations

from orchestrator.core.platform_secret_service import resolve_platform_secret_ref
from orchestrator.core.run_human_input_service import answered_human_inputs_for_attempt
from orchestrator.core.tenant_secret_service import resolve_scoped_secret_ref
from orchestrator.core.worker.repo_setup_service import prepare_execution_repo_for_run
from orchestrator.core.worker.workflow_request_factory import (
    _entry_checkpoint,
    _resolve_branch_from_open_pull_requests,
    build_workflow_request,
)
from orchestrator.tools.github_app import github_client_from_tenant_config


def build_workflow_request_for_run(
    *,
    session,
    tenant,
    run,
    project,
    effective_policy: dict,
    settings,
):  # noqa: ANN001
    return build_workflow_request(
        session=session,
        tenant=tenant,
        run=run,
        project=project,
        effective_policy=effective_policy,
        settings=settings,
        prepare_execution_repo_for_run_fn=prepare_execution_repo_for_run,
        github_client_from_tenant_config_fn=github_client_from_tenant_config,
        resolve_scoped_secret_ref_fn=resolve_scoped_secret_ref,
        resolve_platform_secret_ref_fn=resolve_platform_secret_ref,
        answered_human_inputs_for_attempt_fn=answered_human_inputs_for_attempt,
        entry_checkpoint_fn=_entry_checkpoint,
        resolve_branch_from_open_pull_requests_fn=_resolve_branch_from_open_pull_requests,
    )
