from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.admin.deployment_host_service import (
    deployment_host_command_to_schema,
    register_deployment_host,
    touch_deployment_host,
)
from orchestrator.api.admin.deployment_restore_service import (
    build_restore_host_command_payload,
    complete_project_deployment_restore_run,
    start_project_deployment_restore_run,
)
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    DeploymentHostCommandClaimRead,
    DeploymentHostCommandRead,
    DeploymentHostCommandResultWrite,
    DeploymentHostCommandStartWrite,
    DeploymentHostHeartbeatWrite,
    DeploymentHostRead,
    DeploymentHostRegistrationRead,
    DeploymentHostRegistrationRequest,
)
from orchestrator.core.deployment_host_queue import (
    claim_next_deployment_host_command,
    complete_deployment_host_command,
    start_deployment_host_command,
)
from orchestrator.core.deployment_host_recovery import (
    fail_stale_running_restore_commands,
)
from orchestrator.core.config import get_settings
from orchestrator.core.security import (
    DeploymentHostPrincipal,
    require_deployment_host_agent,
)
from orchestrator.storage.models import DeploymentHostCommand

router = APIRouter(
    prefix="/api/internal/deployment-hosts", tags=["internal-deployment-hosts"]
)


def _hydrate_command_payload(
    *, session: Session, command: DeploymentHostCommand
) -> dict[str, object]:
    if command.kind == "restore_database":
        restore_run_id = str(command.restore_run_id or "").strip()
        if not restore_run_id:
            raise RuntimeError("Restore command is missing restore_run_id")
        return build_restore_host_command_payload(
            session=session, restore_run_id=restore_run_id
        )
    return dict(command.payload_json or {})


@router.post("/register", response_model=DeploymentHostRegistrationRead)
def register_host_agent(
    payload: DeploymentHostRegistrationRequest,
    session: Session = Depends(get_session),
) -> DeploymentHostRegistrationRead:
    return register_deployment_host(
        session=session,
        bootstrap_token=payload.bootstrap_token,
        agent_version=payload.agent_version,
        advertised_capabilities=payload.advertised_capabilities,
    )


@router.post(
    "/heartbeat", response_model=DeploymentHostRead, status_code=status.HTTP_200_OK
)
def heartbeat_host_agent(
    payload: DeploymentHostHeartbeatWrite,
    host: DeploymentHostPrincipal = Depends(require_deployment_host_agent),
    session: Session = Depends(get_session),
) -> DeploymentHostRead:
    return touch_deployment_host(
        session=session,
        host_id=host.host_id,
        agent_version=payload.agent_version,
        advertised_capabilities=payload.advertised_capabilities,
        state=payload.state,
    )


@router.post("/commands/claim", response_model=DeploymentHostCommandClaimRead)
def claim_host_command(
    host: DeploymentHostPrincipal = Depends(require_deployment_host_agent),
    session: Session = Depends(get_session),
) -> DeploymentHostCommandClaimRead:
    fail_stale_running_restore_commands(session=session, host_id=host.host_id)
    command = claim_next_deployment_host_command(session=session, host_id=host.host_id)
    if command is None:
        session.commit()
        return DeploymentHostCommandClaimRead(command=None)
    try:
        payload = _hydrate_command_payload(session=session, command=command)
    except Exception as exc:  # noqa: BLE001
        failed = complete_deployment_host_command(
            session=session,
            host_id=host.host_id,
            command_id=command.command_id,
            claim_id=str(command.claim_id or ""),
            status="failed",
            result_json={},
            last_error=str(exc),
        )
        if failed.kind == "restore_database" and failed.restore_run_id:
            complete_project_deployment_restore_run(
                session=session,
                restore_run_id=failed.restore_run_id,
                status="failed",
                last_error=str(exc),
            )
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    session.commit()
    session.refresh(command)
    return DeploymentHostCommandClaimRead(
        command=deployment_host_command_to_schema(command, payload=payload)
    )


@router.post("/commands/{command_id}/start", response_model=DeploymentHostCommandRead)
def start_host_command(
    command_id: str,
    payload: DeploymentHostCommandStartWrite,
    host: DeploymentHostPrincipal = Depends(require_deployment_host_agent),
    session: Session = Depends(get_session),
) -> DeploymentHostCommandRead:
    command = start_deployment_host_command(
        session=session,
        host_id=host.host_id,
        command_id=command_id,
        claim_id=payload.claim_id,
        lease_duration=timedelta(
            seconds=max(
                60,
                int(
                    getattr(
                        get_settings(),
                        "deployment_host_agent_command_timeout_seconds",
                        900,
                    )
                )
                + 60,
            )
        ),
    )
    if command.kind == "restore_database" and command.restore_run_id:
        start_project_deployment_restore_run(
            session=session, restore_run_id=command.restore_run_id
        )
    session.commit()
    session.refresh(command)
    return deployment_host_command_to_schema(command)


@router.post("/commands/{command_id}/result", response_model=DeploymentHostCommandRead)
def complete_host_command(
    command_id: str,
    payload: DeploymentHostCommandResultWrite,
    host: DeploymentHostPrincipal = Depends(require_deployment_host_agent),
    session: Session = Depends(get_session),
) -> DeploymentHostCommandRead:
    command = complete_deployment_host_command(
        session=session,
        host_id=host.host_id,
        command_id=command_id,
        claim_id=payload.claim_id,
        status=payload.status,
        result_json=payload.result,
        last_error=payload.last_error,
    )
    if command.kind == "restore_database" and command.restore_run_id:
        complete_project_deployment_restore_run(
            session=session,
            restore_run_id=command.restore_run_id,
            status=payload.status,
            last_error=payload.last_error,
        )
    session.commit()
    session.refresh(command)
    return deployment_host_command_to_schema(command)
