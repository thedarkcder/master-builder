from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from orchestrator.api.admin.schema_mappers import run_to_schema
from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    PlatformAgentRead,
    PlatformAgentWrite,
    PlatformPersonaRead,
    PlatformPersonaWrite,
    PlatformRuntimeBindingsRead,
    TeamApprovalRequest,
    TeamTaskCompleteRequest,
    PlatformTeamRunCreateRequest,
    PlatformTeamTemplateRead,
    PlatformTeamTemplateWrite,
    RunRead,
)
from orchestrator.core.config import get_settings
from orchestrator.core.platform_team_catalog_service import platform_team_catalog_service
from orchestrator.core.runs import RunBootstrap, RunStateTransitionError, enqueue_run
from orchestrator.core.security import require_admin
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.temporal.team_run_orchestration import (
    complete_team_task_via_temporal,
    submit_team_approval_via_temporal,
)
from orchestrator.storage.models import Run

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _persona_to_schema(persona) -> PlatformPersonaRead:  # noqa: ANN001
    return PlatformPersonaRead(
        persona_id=persona.persona_id,
        persona_key=persona.persona_key,
        label=persona.label,
        description=persona.description,
        default_display_name=persona.default_display_name,
        default_voice_id=persona.default_voice_id,
        system_prompt_template=persona.system_prompt_template,
        user_prompt_template=persona.user_prompt_template,
        allowed_surfaces=list(persona.allowed_surfaces or []),
        is_active=bool(persona.is_active),
        version=persona.version,
        published_at=persona.published_at,
        created_at=persona.created_at,
        updated_at=persona.updated_at,
    )


def _agent_to_schema(*, session: Session, agent) -> PlatformAgentRead:  # noqa: ANN001
    persona = platform_team_catalog_service.get_persona(session=session, persona_id=agent.persona_id)
    if persona is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Agent references missing persona")
    return PlatformAgentRead(
        agent_id=agent.agent_id,
        agent_key=agent.agent_key,
        label=agent.label,
        description=agent.description,
        persona_key=persona.persona_key,
        runtime_role_key=agent.runtime_role_key,
        named_agent_key=agent.named_agent_key,
        selector_key=agent.selector_key,
        default_profile_name=agent.default_profile_name,
        is_active=bool(agent.is_active),
        persona=_persona_to_schema(persona),
        version=agent.version,
        published_at=agent.published_at,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _template_to_schema(*, session: Session, template) -> PlatformTeamTemplateRead:  # noqa: ANN001
    roles = platform_team_catalog_service.list_team_roles(session=session, template_id=template.template_id)
    tasks = platform_team_catalog_service.list_team_tasks(session=session, template_id=template.template_id)
    edges = platform_team_catalog_service.list_team_edges(session=session, template_id=template.template_id)
    role_reads = []
    for role in roles:
        persona = platform_team_catalog_service.get_persona(session=session, persona_id=role.persona_id)
        agent = platform_team_catalog_service.get_agent(session=session, agent_id=role.agent_id)
        if persona is None or agent is None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Template references missing persona or agent")
        role_reads.append(
            {
                "role_id": role.role_id,
                "role_key": role.role_key,
                "label": role.label,
                "description": role.description,
                "position": role.position,
                "persona": _persona_to_schema(persona),
                "agent": _agent_to_schema(session=session, agent=agent),
            }
        )
    return PlatformTeamTemplateRead(
        template_id=template.template_id,
        team_key=template.team_key,
        team_label=template.label,
        description=template.description,
        is_active=bool(template.is_active),
        definition_version=template.definition_version,
        published_at=template.published_at,
        roles=role_reads,
        tasks=[
            {
                "task_id": task.task_id,
                "task_key": task.task_key,
                "label": task.label,
                "owner_role_key": task.owner_role_key,
                "position": task.position,
                "executor_kind": task.executor_kind,
                "artifact_contract": dict(task.artifact_contract or {}),
                "approval_rule": dict(task.approval_rule or {}),
            }
            for task in tasks
        ],
        edges=[
            {
                "edge_id": edge.edge_id,
                "from_task_key": edge.from_task_key,
                "to_task_key": edge.to_task_key,
            }
            for edge in edges
        ],
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


@router.get("/platform-personas", response_model=list[PlatformPersonaRead])
def list_platform_personas(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[PlatformPersonaRead]:
    return [_persona_to_schema(persona) for persona in platform_team_catalog_service.list_personas(session=session)]


@router.post("/platform-personas", response_model=PlatformPersonaRead, status_code=status.HTTP_201_CREATED)
def create_platform_persona(
    payload: PlatformPersonaWrite,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformPersonaRead:
    try:
        persona = platform_team_catalog_service.create_persona(session=session, payload=payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _persona_to_schema(persona)


@router.put("/platform-personas/{persona_id}", response_model=PlatformPersonaRead)
def update_platform_persona(
    persona_id: str,
    payload: PlatformPersonaWrite,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformPersonaRead:
    try:
        persona = platform_team_catalog_service.update_persona(session=session, persona_id=persona_id, payload=payload.model_dump())
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _persona_to_schema(persona)


@router.get("/platform-agents", response_model=list[PlatformAgentRead])
def list_platform_agents(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[PlatformAgentRead]:
    return [_agent_to_schema(session=session, agent=agent) for agent in platform_team_catalog_service.list_agents(session=session)]


@router.post("/platform-agents", response_model=PlatformAgentRead, status_code=status.HTTP_201_CREATED)
def create_platform_agent(
    payload: PlatformAgentWrite,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformAgentRead:
    try:
        agent = platform_team_catalog_service.create_agent(session=session, payload=payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _agent_to_schema(session=session, agent=agent)


@router.put("/platform-agents/{agent_id}", response_model=PlatformAgentRead)
def update_platform_agent(
    agent_id: str,
    payload: PlatformAgentWrite,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformAgentRead:
    try:
        agent = platform_team_catalog_service.update_agent(session=session, agent_id=agent_id, payload=payload.model_dump())
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _agent_to_schema(session=session, agent=agent)


@router.get("/platform-team-templates", response_model=list[PlatformTeamTemplateRead])
def list_platform_team_templates(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[PlatformTeamTemplateRead]:
    return [_template_to_schema(session=session, template=template) for template in platform_team_catalog_service.list_team_templates(session=session)]


@router.post("/platform-team-templates", response_model=PlatformTeamTemplateRead, status_code=status.HTTP_201_CREATED)
def create_platform_team_template(
    payload: PlatformTeamTemplateWrite,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformTeamTemplateRead:
    try:
        template = platform_team_catalog_service.create_team_template(session=session, payload=payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _template_to_schema(session=session, template=template)


@router.put("/platform-team-templates/{template_id}", response_model=PlatformTeamTemplateRead)
def update_platform_team_template(
    template_id: str,
    payload: PlatformTeamTemplateWrite,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformTeamTemplateRead:
    merged_payload = payload.model_dump()
    existing = platform_team_catalog_service.get_team_template(session=session, template_id=template_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Platform team template not found")
    merged_payload["team_key"] = existing.team_key
    try:
        template = platform_team_catalog_service.update_team_template(session=session, template_id=template_id, payload=merged_payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _template_to_schema(session=session, template=template)


@router.post("/platform-team-templates/{template_id}/publish", response_model=PlatformTeamTemplateRead)
def publish_platform_team_template(
    template_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformTeamTemplateRead:
    try:
        template = platform_team_catalog_service.publish_team_template(session=session, template_id=template_id)
    except LookupError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _template_to_schema(session=session, template=template)


@router.get("/platform-runtime-bindings", response_model=PlatformRuntimeBindingsRead)
def get_platform_runtime_bindings(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> PlatformRuntimeBindingsRead:
    return PlatformRuntimeBindingsRead(**platform_team_catalog_service.list_runtime_bindings(session=session))


@router.post("/platform-team-templates/{template_id}/runs", response_model=RunRead, status_code=status.HTTP_201_CREATED)
def launch_platform_team_run(
    template_id: str,
    payload: PlatformTeamRunCreateRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    template = platform_team_catalog_service.get_team_template(session=session, template_id=template_id)
    if template is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Platform team template not found")
    if template.published_at is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Team template must be published before launch")

    snapshot = ExecutionSnapshot.empty(trigger_context={"source": "platform_team_template", "template_id": template_id})
    snapshot.context.execution_context["team_run"] = platform_team_catalog_service.build_team_run_snapshot(
        session=session,
        template=template,
    )
    snapshot.context.execution_context["team_template_id"] = template.template_id
    snapshot.context.execution_context["orchestration_backend"] = "temporal"

    enqueue_result = enqueue_run(
        session,
        tenant_id=payload.tenant_id,
        project_id=payload.project_id,
        issue_key=payload.issue_key,
        issue_summary=payload.issue_summary,
        issue_description=payload.issue_description,
        bootstrap=RunBootstrap(
            plan=snapshot.dump(),
            entry_mode="fresh",
            entry_stage="team",
        ),
    )
    return run_to_schema(enqueue_result.run)


@router.post("/runs/{run_id}/team-tasks/{task_key}/complete", response_model=RunRead)
def complete_team_task(
    run_id: str,
    task_key: str,
    payload: TeamTaskCompleteRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    try:
        complete_team_task_via_temporal(
            settings=get_settings(),
            workflow_id=str(run.workflow_id),
            run_id=str(run.run_id),
            task_key=task_key,
            artifact_payload=dict(payload.artifact_payload or {}),
            summary=payload.summary,
        )
    except RunStateTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    session.refresh(run)
    return run_to_schema(run)


@router.post("/runs/{run_id}/team-tasks/{task_key}/approvals", response_model=RunRead)
def submit_team_task_approval(
    run_id: str,
    task_key: str,
    payload: TeamApprovalRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> RunRead:
    run = session.get(Run, run_id)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    try:
        submit_team_approval_via_temporal(
            settings=get_settings(),
            workflow_id=str(run.workflow_id),
            run_id=str(run.run_id),
            task_key=task_key,
            decision=payload.decision,
            comment=payload.comment,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    session.refresh(run)
    return run_to_schema(run)
