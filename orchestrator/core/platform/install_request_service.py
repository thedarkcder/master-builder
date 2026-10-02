from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.platform.install_registry_service import SUPPORTED_INSTALL_KINDS
from orchestrator.core.platform.secret_manager import normalize_secret_ref
from orchestrator.core.runs.human_input_service import (
    INPUT_STATUS_ANSWERED,
    INPUT_STATUS_EXPIRED,
    INPUT_STATUS_PENDING,
    answer_human_input_request,
    create_human_input_request,
    resume_workflow_from_human_input_answer,
)
from orchestrator.storage.models import (
    Project,
    ProjectInstall,
    ProjectInstallRequest,
    Run,
    RunHumanInputRequest,
    Tenant,
)

INSTALL_REQUEST_STATUS_PENDING = "pending"
INSTALL_REQUEST_STATUS_APPROVED = "approved"
INSTALL_REQUEST_STATUS_FULFILLED = "fulfilled"
INSTALL_REQUEST_STATUS_REJECTED = "rejected"

INSTALL_REQUEST_KIND_PROJECT_MISSING = "project_missing_install"
INSTALL_REQUEST_KIND_UNSUPPORTED = "unsupported_kind"
KNOWN_INTEGRATION_LABELS = (
    "hubspot",
    "stripe",
    "slack",
    "jira",
    "github",
    "atlassian",
    "supabase",
    "railway",
    "fastlane",
)
GENERIC_INSTALL_LABEL_TOKENS = {
    "config",
    "configuration",
    "configure",
    "connect",
    "connector",
    "dependency",
    "dependencies",
    "install",
    "integration",
    "setup",
}


@dataclass(frozen=True)
class ProjectInstallRequestWrite:
    kind: str
    label: str
    reason: str
    suggested_config: dict
    required_bindings: tuple[str, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_bindings(
    values: tuple[str, ...] | list[str] | set[str] | None,
) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in values or ():
        value = str(raw_value or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return tuple(normalized)


def normalize_install_request_label(*, kind: str, label: str) -> str:
    normalized_label = str(label or "").strip()
    if str(kind or "").strip().lower() != "integration":
        return normalized_label
    tokens = [
        token
        for token in "".join(
            character.lower() if character.isalnum() else " "
            for character in normalized_label
        ).split()
        if token
    ]
    for known_label in KNOWN_INTEGRATION_LABELS:
        if known_label in tokens:
            return known_label
    meaningful_tokens = [
        token
        for token in tokens
        if token not in GENERIC_INSTALL_LABEL_TOKENS
        and not any(character.isdigit() for character in token)
    ]
    if meaningful_tokens:
        return "-".join(meaningful_tokens)
    return normalized_label


def normalize_install_request_write(
    payload: ProjectInstallRequestWrite,
) -> ProjectInstallRequestWrite:
    kind = str(payload.kind or "").strip().lower()
    label = normalize_install_request_label(kind=kind, label=payload.label)
    reason = str(payload.reason or "").strip()
    if not kind:
        raise ValueError("Install kind is required")
    if not label:
        raise ValueError("Install label is required")
    if not reason:
        raise ValueError("Install reason is required")
    return ProjectInstallRequestWrite(
        kind=kind,
        label=label,
        reason=reason,
        suggested_config=payload.suggested_config
        if isinstance(payload.suggested_config, dict)
        else {},
        required_bindings=_normalize_bindings(payload.required_bindings),
    )


def request_kind_for_install(kind: str) -> str:
    normalized_kind = str(kind or "").strip().lower()
    if normalized_kind in SUPPORTED_INSTALL_KINDS:
        return INSTALL_REQUEST_KIND_PROJECT_MISSING
    return INSTALL_REQUEST_KIND_UNSUPPORTED


def _clean_string_list(value: object) -> list[str]:
    if isinstance(value, str):
        items: list[object] = [value]
    elif isinstance(value, list | tuple | set):
        items = list(value)
    else:
        return []
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_item in items:
        item = str(raw_item or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        cleaned.append(item)
    return cleaned


def _suggested_config_list(config: dict, key: str) -> list[str]:
    return _clean_string_list(config.get(key))


def _format_bullets(label: str, items: list[str]) -> list[str]:
    if not items:
        return []
    lines = [f"{label}:"]
    lines.extend(f"- {item}" for item in items)
    return lines


def _project_installs_url(*, settings, tenant_id: str, project_id: str) -> str | None:
    base_url = str(getattr(settings, "admin_ui_base_url", "") or "").strip().rstrip("/")
    if not base_url:
        return None
    return (
        f"{base_url}/{quote(str(tenant_id), safe='')}/projects/"
        f"{quote(str(project_id), safe='')}/installs"
    )


def _project_secret_placeholder_ref(
    *, tenant_id: str, project_id: str, binding_name: str
) -> str:
    normalized_binding = normalize_secret_ref(binding_name)
    if "/" in normalized_binding:
        raise ValueError(
            "Required binding names must be unscoped names, not secret refs"
        )
    return normalize_secret_ref(
        f"project/{tenant_id}/{project_id}/{normalized_binding}"
    )


def _apply_required_binding_placeholders(
    *,
    project: Project,
    required_bindings: list[str] | tuple[str, ...],
    now: datetime,
) -> list[str]:
    current_refs = project.secret_refs if isinstance(project.secret_refs, dict) else {}
    next_refs = {
        str(key): str(value) for key, value in current_refs.items() if str(key).strip()
    }
    added: list[str] = []
    for raw_binding in required_bindings:
        binding = normalize_secret_ref(str(raw_binding or "").strip())
        if "/" in binding:
            raise ValueError(
                "Required binding names must be unscoped names, not secret refs"
            )
        if binding in next_refs and str(next_refs[binding] or "").strip():
            continue
        next_refs[binding] = _project_secret_placeholder_ref(
            tenant_id=project.tenant_id,
            project_id=project.project_id,
            binding_name=binding,
        )
        added.append(binding)
    if added:
        project.secret_refs = next_refs
        project.updated_at = now
    return added


def _ensure_project_install_for_request(
    *,
    session: Session,
    request: ProjectInstallRequest,
    now: datetime,
) -> ProjectInstall:
    existing = (
        session.execute(
            select(ProjectInstall).where(
                ProjectInstall.tenant_id == request.tenant_id,
                ProjectInstall.project_id == request.project_id,
                ProjectInstall.kind == request.kind,
                ProjectInstall.label == request.label,
            )
        )
        .scalars()
        .first()
    )
    config = (
        request.suggested_config_json
        if isinstance(request.suggested_config_json, dict)
        else {}
    )
    binding_names = list(request.required_bindings_json or [])
    if existing is not None:
        existing.enabled = True
        existing.config_json = config
        existing.binding_names_json = binding_names
        existing.updated_at = now
        return existing
    install = ProjectInstall(
        install_id=uuid4().hex,
        tenant_id=request.tenant_id,
        project_id=request.project_id,
        kind=request.kind,
        label=request.label,
        enabled=True,
        config_json=config,
        binding_names_json=binding_names,
        created_at=now,
        updated_at=now,
    )
    session.add(install)
    return install


def _build_install_operator_prompt(
    *,
    settings,
    tenant: Tenant,
    project: Project,
    issue_key: str,
    normalized: ProjectInstallRequestWrite,
    request_kind: str,
) -> tuple[str, str, str, dict]:
    source_changes = _suggested_config_list(
        normalized.suggested_config, "source_project_changes"
    )
    platform_changes = _suggested_config_list(
        normalized.suggested_config, "master_builder_changes"
    )
    package_changes = _suggested_config_list(
        normalized.suggested_config, "package_changes"
    )
    secret_or_binding_changes = _suggested_config_list(
        normalized.suggested_config, "secrets_or_bindings_needed"
    )
    resume_when = str(normalized.suggested_config.get("resume_when") or "").strip()
    operator_decision = str(
        normalized.suggested_config.get("operator_decision") or ""
    ).strip()
    if not operator_decision:
        operator_decision = (
            f"Can Master Builder install or configure the dependencies needed for {normalized.label} "
            f"in project {project.name} and in Master Builder so {issue_key} can continue?"
        )

    lines = [
        f"`{issue_key}` needs approval before the run can continue.",
        "",
        operator_decision,
        "",
        f"Why this is needed: {normalized.reason}",
    ]
    if request_kind == INSTALL_REQUEST_KIND_UNSUPPORTED:
        lines.extend(
            [
                "",
                "Master Builder does not already have a preconfigured install executor for this capability, "
                "so this is asking for approval to add the required source-project and Master Builder support.",
            ]
        )
    detail_lines = [
        *_format_bullets("Source project changes", source_changes),
        *_format_bullets("Master Builder changes", platform_changes),
        *_format_bullets("Package or dependency changes", package_changes),
        *_format_bullets(
            "Secrets or bindings the operator may need to configure",
            secret_or_binding_changes,
        ),
    ]
    if detail_lines:
        lines.extend(["", *detail_lines])
    if normalized.required_bindings:
        lines.extend(
            [
                "",
                "Required binding names:",
                *[f"- {binding}" for binding in normalized.required_bindings],
            ]
        )

    admin_url = _project_installs_url(
        settings=settings, tenant_id=tenant.tenant_id, project_id=project.project_id
    )
    if admin_url:
        lines.extend(["", f"Admin link: {admin_url}"])

    instructions_parts = [
        "Reply `yes` to approve this dependency/install work, or `no` to stop and replan."
    ]
    if admin_url:
        instructions_parts.append(
            "You can also review the request on the project Installs page before replying."
        )
    if resume_when:
        instructions_parts.append(f"Resume condition: {resume_when}")

    context = {
        "kind": normalized.kind,
        "label": normalized.label,
        "request_kind": request_kind,
        "operator_decision": operator_decision,
        "source_project_changes": source_changes,
        "master_builder_changes": platform_changes,
        "package_changes": package_changes,
        "secrets_or_bindings_needed": secret_or_binding_changes,
        "resume_when": resume_when,
        "admin_url": admin_url,
        "questions": [
            {
                "id": "approve_install_dependency_work",
                "question": operator_decision,
                "options": ["yes - approve and continue", "no - stop and replan"],
            }
        ],
    }
    return (
        "\n".join(lines),
        " ".join(instructions_parts),
        "Reply `yes` to approve, or `no` to stop and replan.",
        context,
    )


def list_project_install_requests(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    statuses: tuple[str, ...] | None = None,
) -> list[ProjectInstallRequest]:
    query = (
        select(ProjectInstallRequest)
        .where(
            ProjectInstallRequest.tenant_id == tenant_id,
            ProjectInstallRequest.project_id == project_id,
        )
        .order_by(ProjectInstallRequest.created_at.desc())
    )
    if statuses:
        query = query.where(ProjectInstallRequest.status.in_(tuple(statuses)))
    return session.execute(query).scalars().all()


def create_install_request(
    *,
    session: Session,
    settings,
    tenant: Tenant,
    project: Project,
    run: Run,
    source_stage: str,
    payload: ProjectInstallRequestWrite,
    now: datetime | None = None,
) -> ProjectInstallRequest:
    normalized = normalize_install_request_write(payload)
    request_kind = request_kind_for_install(normalized.kind)
    approved = (
        session.execute(
            select(ProjectInstallRequest)
            .where(
                ProjectInstallRequest.tenant_id == tenant.tenant_id,
                ProjectInstallRequest.project_id == project.project_id,
                ProjectInstallRequest.kind == normalized.kind,
                ProjectInstallRequest.label == normalized.label,
                ProjectInstallRequest.status == INSTALL_REQUEST_STATUS_APPROVED,
            )
            .order_by(
                ProjectInstallRequest.updated_at.desc(),
                ProjectInstallRequest.created_at.desc(),
            )
            .limit(1)
        )
        .scalars()
        .first()
    )
    if approved is not None:
        _ensure_project_install_for_request(
            session=session, request=approved, now=now or _utc_now()
        )
        session.commit()
        session.refresh(approved)
        return approved
    existing = (
        session.execute(
            select(ProjectInstallRequest)
            .where(
                ProjectInstallRequest.tenant_id == tenant.tenant_id,
                ProjectInstallRequest.project_id == project.project_id,
                ProjectInstallRequest.workflow_id == run.workflow_id,
                ProjectInstallRequest.kind == normalized.kind,
                ProjectInstallRequest.label == normalized.label,
                ProjectInstallRequest.status == INSTALL_REQUEST_STATUS_PENDING,
            )
            .order_by(ProjectInstallRequest.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return existing

    timestamp = now or _utc_now()
    request = ProjectInstallRequest(
        request_id=uuid4().hex,
        tenant_id=tenant.tenant_id,
        project_id=project.project_id,
        workflow_id=run.workflow_id,
        run_id=run.run_id,
        issue_key=str(run.issue_key or "").strip(),
        kind=normalized.kind,
        label=normalized.label,
        reason=normalized.reason,
        suggested_config_json=normalized.suggested_config,
        required_bindings_json=list(normalized.required_bindings),
        status=INSTALL_REQUEST_STATUS_PENDING,
        request_kind=request_kind,
        created_at=timestamp,
        updated_at=timestamp,
    )
    session.add(request)
    human_prompt, instructions, expected_reply_format, operator_context = (
        _build_install_operator_prompt(
            settings=settings,
            tenant=tenant,
            project=project,
            issue_key=str(run.issue_key or "").strip(),
            normalized=normalized,
            request_kind=request_kind,
        )
    )
    create_human_input_request(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        run=run,
        issue_key=run.issue_key,
        source_stage=source_stage,
        request_type="install_request",
        prompt=human_prompt,
        instructions=instructions,
        expected_reply_format=expected_reply_format,
        request_context={
            "install_request_id": request.request_id,
            "required_bindings": list(normalized.required_bindings),
            **operator_context,
        },
        expires_in_minutes=60,
    )
    session.refresh(request)
    return request


def get_project_install_request(
    *, session: Session, request_id: str
) -> ProjectInstallRequest | None:
    return session.get(ProjectInstallRequest, request_id)


def resumable_human_input_for_install_request(
    *,
    session: Session,
    request: ProjectInstallRequest,
) -> RunHumanInputRequest | None:
    workflow_id = str(request.workflow_id or "").strip()
    if not workflow_id:
        return None
    candidates = (
        session.execute(
            select(RunHumanInputRequest)
            .where(
                RunHumanInputRequest.workflow_id == workflow_id,
                RunHumanInputRequest.request_type == "install_request",
                RunHumanInputRequest.status.in_(
                    (INPUT_STATUS_PENDING, INPUT_STATUS_ANSWERED, INPUT_STATUS_EXPIRED)
                ),
            )
            .order_by(RunHumanInputRequest.created_at.desc())
        )
        .scalars()
        .all()
    )
    for candidate in candidates:
        context = (
            candidate.request_context_json
            if isinstance(candidate.request_context_json, dict)
            else {}
        )
        if human_input_context_matches_install_request(
            session=session, request=request, context=context
        ):
            return candidate
    return None


def human_input_context_matches_install_request(
    *,
    session: Session,
    request: ProjectInstallRequest,
    context: dict,
) -> bool:
    context_request_id = str(context.get("install_request_id") or "").strip()
    if context_request_id == str(request.request_id or "").strip():
        return True

    context_request = (
        session.get(ProjectInstallRequest, context_request_id)
        if context_request_id
        else None
    )
    if context_request is not None:
        return (
            context_request.tenant_id == request.tenant_id
            and context_request.project_id == request.project_id
            and str(context_request.kind or "").strip().lower()
            == str(request.kind or "").strip().lower()
            and normalize_install_request_label(
                kind=context_request.kind, label=context_request.label
            )
            == normalize_install_request_label(kind=request.kind, label=request.label)
        )

    context_kind = str(context.get("kind") or "").strip().lower()
    if context_kind != str(request.kind or "").strip().lower():
        return False
    context_label = str(context.get("label") or "").strip()
    if not context_label:
        return False
    return normalize_install_request_label(
        kind=context_kind, label=context_label
    ) == normalize_install_request_label(
        kind=request.kind,
        label=request.label,
    )


def equivalent_install_requests(
    *,
    session: Session,
    request: ProjectInstallRequest,
    statuses: tuple[str, ...] | None = None,
) -> list[ProjectInstallRequest]:
    canonical_label = normalize_install_request_label(
        kind=request.kind, label=request.label
    )
    query = (
        select(ProjectInstallRequest)
        .where(
            ProjectInstallRequest.tenant_id == request.tenant_id,
            ProjectInstallRequest.project_id == request.project_id,
            ProjectInstallRequest.kind == request.kind,
        )
        .order_by(ProjectInstallRequest.created_at.asc())
    )
    if statuses is not None:
        query = query.where(ProjectInstallRequest.status.in_(tuple(statuses)))
    candidates = session.execute(query).scalars().all()
    return [
        candidate
        for candidate in candidates
        if normalize_install_request_label(kind=candidate.kind, label=candidate.label)
        == canonical_label
    ]


def equivalent_pending_install_requests(
    *,
    session: Session,
    request: ProjectInstallRequest,
) -> list[ProjectInstallRequest]:
    return equivalent_install_requests(
        session=session,
        request=request,
        statuses=(INSTALL_REQUEST_STATUS_PENDING,),
    )


def approve_install_request(
    *,
    session: Session,
    settings,
    request: ProjectInstallRequest,
    project: Project,
    source_ref: str | None = None,
    now: datetime | None = None,
) -> ProjectInstallRequest:
    if (
        request.tenant_id != project.tenant_id
        or request.project_id != project.project_id
    ):
        raise ValueError("Install request does not belong to the supplied project")
    current_status = str(request.status or "").strip().lower()
    if current_status == INSTALL_REQUEST_STATUS_REJECTED:
        raise ValueError("Rejected install requests cannot be approved")
    timestamp = now or _utc_now()
    _apply_required_binding_placeholders(
        project=project,
        required_bindings=list(request.required_bindings_json or []),
        now=timestamp,
    )
    requests_to_approve = equivalent_install_requests(
        session=session,
        request=request,
        statuses=(INSTALL_REQUEST_STATUS_PENDING, INSTALL_REQUEST_STATUS_APPROVED),
    )
    request_workflow_id = str(request.workflow_id or "").strip()
    if request_workflow_id:
        requests_to_approve = [
            install_request
            for install_request in requests_to_approve
            if str(install_request.workflow_id or "").strip() == request_workflow_id
        ]
    if request.request_id not in {
        install_request.request_id for install_request in requests_to_approve
    }:
        requests_to_approve.append(request)
    for install_request in requests_to_approve:
        install_request.label = normalize_install_request_label(
            kind=install_request.kind, label=install_request.label
        )
        install_request.status = INSTALL_REQUEST_STATUS_APPROVED
        install_request.updated_at = timestamp
        _ensure_project_install_for_request(
            session=session, request=install_request, now=timestamp
        )

    for install_request in requests_to_approve:
        human_request = resumable_human_input_for_install_request(
            session=session, request=install_request
        )
        if human_request is None:
            continue
        answered_request = answer_human_input_request(
            session=session,
            settings=settings,
            request=human_request,
            reply_text=(
                "Approved. Master Builder may add the required project dependencies, package manifest changes, "
                "and secret placeholders needed for this request."
            ),
            source_ref=source_ref,
            allow_expired=True,
        )
        resume_workflow_from_human_input_answer(
            session=session,
            settings=settings,
            request=answered_request,
        )
    request = session.get(ProjectInstallRequest, request.request_id) or request
    project = session.get(Project, project.project_id) or project
    request.status = INSTALL_REQUEST_STATUS_APPROVED
    request.updated_at = timestamp
    project.updated_at = timestamp
    session.commit()
    session.refresh(request)
    return request


def reject_install_request(
    *,
    session: Session,
    settings,
    request: ProjectInstallRequest,
    source_ref: str | None = None,
    now: datetime | None = None,
) -> ProjectInstallRequest:
    timestamp = now or _utc_now()
    request.status = INSTALL_REQUEST_STATUS_REJECTED
    request.updated_at = timestamp
    human_request = resumable_human_input_for_install_request(
        session=session, request=request
    )
    if human_request is not None:
        answered_request = answer_human_input_request(
            session=session,
            settings=settings,
            request=human_request,
            reply_text="Rejected. Do not install or configure the requested project dependency work.",
            source_ref=source_ref,
            allow_expired=True,
        )
        resume_workflow_from_human_input_answer(
            session=session,
            settings=settings,
            request=answered_request,
        )
        request = session.get(ProjectInstallRequest, request.request_id) or request
        request.status = INSTALL_REQUEST_STATUS_REJECTED
        request.updated_at = timestamp
    session.commit()
    session.refresh(request)
    return request


def update_install_request_status(
    *,
    session: Session,
    request: ProjectInstallRequest,
    status: str,
    now: datetime | None = None,
) -> ProjectInstallRequest:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {
        INSTALL_REQUEST_STATUS_PENDING,
        INSTALL_REQUEST_STATUS_APPROVED,
        INSTALL_REQUEST_STATUS_FULFILLED,
        INSTALL_REQUEST_STATUS_REJECTED,
    }:
        raise ValueError(f"Unsupported install request status '{status}'")
    request.status = normalized_status
    request.updated_at = now or _utc_now()
    session.commit()
    session.refresh(request)
    return request
