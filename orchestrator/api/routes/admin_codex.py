from __future__ import annotations

from fastapi import APIRouter, Depends

from orchestrator.api.schemas import CodexModelCatalogRead, CodexModelOptionRead, CodexReasoningOptionRead
from orchestrator.core.codex_models import parse_supported_codex_models, parse_supported_reasoning_efforts
from orchestrator.core.config import get_settings
from orchestrator.core.security import require_admin

router = APIRouter(prefix="/api/admin", tags=["admin"])


@router.get("/codex/models", response_model=CodexModelCatalogRead)
def list_codex_models(
    _: str = Depends(require_admin),
) -> CodexModelCatalogRead:
    settings = get_settings()
    return CodexModelCatalogRead(
        default_model=settings.codex_model,
        default_reasoning_effort=settings.codex_reasoning_effort,
        models=[
            CodexModelOptionRead(
                id=option.model_id,
                label=option.label,
                description=option.description,
            )
            for option in parse_supported_codex_models(
                default_model=settings.codex_model,
                configured_models=settings.codex_supported_models,
            )
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
        ],
    )
