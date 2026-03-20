from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.config import Settings
from orchestrator.core.knowledge_base import ensure_knowledge_embedding_model_ready


@dataclass(frozen=True)
class KnowledgeDependencyPrewarmResult:
    embedding_model: str


def prewarm_knowledge_dependencies(*, settings: Settings) -> KnowledgeDependencyPrewarmResult:
    _ = settings
    try:
        embedding_model = ensure_knowledge_embedding_model_ready(local_files_only=True)
    except Exception:  # noqa: BLE001
        embedding_model = ensure_knowledge_embedding_model_ready(local_files_only=False)
    return KnowledgeDependencyPrewarmResult(
        embedding_model=embedding_model,
    )
