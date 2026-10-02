from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from orchestrator.api.admin import integration_dependencies as deps
from orchestrator.api.admin.release_bootstrap_helpers import (
    compute_release_bootstrap_result as compute_release_bootstrap_result_impl,
)
from orchestrator.api.admin.release_bootstrap_service import (
    get_release_bootstrap_report as get_release_bootstrap_report_impl,
    list_tenant_repo_bootstrap_states as list_tenant_repo_bootstrap_states_impl,
    run_release_bootstrap as run_release_bootstrap_impl,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import ReleaseBootstrapReportRead, RepoBootstrapStateRead
from orchestrator.core.config import get_settings
from orchestrator.core.security import require_admin
from orchestrator.storage.models import Tenant
from orchestrator.tools.bootstrap import list_repo_bootstrap_states

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get(
    "/tenants/{tenant_id}/repo-bootstrap", response_model=list[RepoBootstrapStateRead]
)
def list_tenant_repo_bootstrap_states(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[RepoBootstrapStateRead]:
    return list_tenant_repo_bootstrap_states_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        list_repo_bootstrap_states_fn=list_repo_bootstrap_states,
    )


@router.get(
    "/tenants/{tenant_id}/release/bootstrap",
    response_model=ReleaseBootstrapReportRead | None,
)
def get_release_bootstrap_report(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ReleaseBootstrapReportRead | None:
    return get_release_bootstrap_report_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        release_bootstrap_report_from_config_fn=deps.release_bootstrap_report_from_config,
    )


@router.post(
    "/tenants/{tenant_id}/release/bootstrap", response_model=ReleaseBootstrapReportRead
)
def run_release_bootstrap(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ReleaseBootstrapReportRead:
    return run_release_bootstrap_impl(
        session=session,
        tenant_id=tenant_id,
        tenant_model=Tenant,
        settings=get_settings(),
        required_statuses=deps.RELEASE_BOOTSTRAP_REQUIRED_STATUSES,
        compute_release_bootstrap_result_fn=compute_release_bootstrap_result_impl,
        refresh_atlassian_connection_tokens_fn=deps.refresh_atlassian_connection_tokens,
        atlassian_oauth_client_fn=deps.atlassian_oauth_client,
    )
