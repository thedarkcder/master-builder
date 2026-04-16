from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import CodexModelCatalogRead, CodexModelOptionRead, CodexReasoningOptionRead
from orchestrator.core.agent_execution_profiles import (
    build_agent_execution_profile,
    collect_models_for_runtime_kind,
    default_execution_profiles,
    normalize_execution_profiles,
    runtime_kind_supports_reasoning_effort,
)
from orchestrator.core.codex_models import parse_supported_reasoning_efforts
from orchestrator.core.config import get_settings
from orchestrator.core.platform_settings_service import (
    SETTING_KEY_AGENT_RUNTIME_PROFILES,
    platform_settings_service,
)
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/codex/models", response_model=CodexModelCatalogRead)
def list_codex_models(
    runtime_kind: str | None = Query(default=None),
    profile_name: str | None = Query(default=None),
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> CodexModelCatalogRead:
    settings = get_settings()
    default_profiles = default_execution_profiles(
        default_codex_cli_command=settings.codex_cli_command,
        default_codex_reasoning_effort=settings.codex_reasoning_effort,
        default_codex_supported_models=settings.codex_supported_models,
        default_chat_cli_command=settings.chat_cli_command,
        default_chat_reasoning_effort=settings.chat_reasoning_effort,
        default_claude_cli_command=getattr(settings, "claude_cli_command", ""),
    )
    payload = platform_settings_service.get_json(session=session, setting_key=SETTING_KEY_AGENT_RUNTIME_PROFILES)
    merged_profiles = dict(default_profiles)
    merged_profiles.update(normalize_execution_profiles(payload.get("profiles")))
    normalized_profile_name = str(profile_name or "").strip() or None
    if normalized_profile_name and normalized_profile_name not in merged_profiles:
        raise HTTPException(status_code=404, detail="Execution profile not found")
    resolved_profile = (
        build_agent_execution_profile(profile_name=normalized_profile_name, profiles=merged_profiles)
        if normalized_profile_name
        else None
    )
    resolved_runtime_kind = (
        resolved_profile.runtime_kind
        if resolved_profile is not None
        else str(runtime_kind or "codex_cli").strip().lower() or "codex_cli"
    )
    options = collect_models_for_runtime_kind(
        runtime_kind=resolved_runtime_kind,
        default_model=resolved_profile.model if resolved_profile is not None else None,
        codex_supported_models=settings.codex_supported_models,
        profiles=merged_profiles,
    )
    resolved_default_model = (
        resolved_profile.model
        if resolved_profile is not None
        else (options[0].model_id if options else "")
    )
    return CodexModelCatalogRead(
        default_model=resolved_default_model,
        default_reasoning_effort=settings.codex_reasoning_effort,
        runtime_kind=resolved_runtime_kind,
        profile_name=normalized_profile_name,
        models=[
            CodexModelOptionRead(
                id=option.model_id,
                label=option.label,
                description=option.description,
            )
            for option in options
        ],
        reasoning_efforts=[
            CodexReasoningOptionRead(
                id=option.effort_id,
                label=option.label,
                description=option.description,
            )
            for option in parse_supported_reasoning_efforts(
                default_effort=settings.codex_reasoning_effort,
            )
        ] if runtime_kind_supports_reasoning_effort(resolved_runtime_kind) else [],
    )
