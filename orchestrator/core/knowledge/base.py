from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from functools import lru_cache
import hashlib
import importlib
import logging
import math
import mimetypes
import os
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import case, delete, desc, func, select, text
from sqlalchemy.orm import Session

from orchestrator.core.config import get_settings
from orchestrator.storage.models import KnowledgeAsset, KnowledgeChunk, KnowledgeFact
from orchestrator.storage.vector_type import vector_literal

SLOT_ALIASES: dict[str, tuple[str, ...]] = {
    "objective": ("objective", "goal", "problem"),
    "scope": ("scope", "in scope"),
    "acceptance_criteria": ("acceptance criteria", "acceptance", "done when"),
    "how_to_test": ("how to test", "test plan", "validation"),
    "nfr_intent": ("nfr intent", "mvp vs scale-ready", "nfr"),
    "reliability_security_constraints": (
        "reliability/security constraints",
        "reliability constraints",
        "security constraints",
    ),
    "out_of_scope": ("out of scope", "not in scope"),
    "rollout_constraints": ("rollout constraints", "migration constraints"),
    "decision_owner": ("decision owner", "owner"),
    "dependencies_and_risks": ("dependencies / risks", "dependencies and risks", "risks", "dependencies"),
}

FACT_SLOT_ORDER = tuple(SLOT_ALIASES.keys())
_WORD_PATTERN = re.compile(r"[a-z0-9]{2,}")
_SECTION_PATTERN = re.compile(r"(?im)^\s*#+\s*(.+?)\s*$")
_LABELED_FACT_PATTERN = re.compile(r"(?i)^(?P<label>[a-z0-9][a-z0-9 /_().-]{1,96})\s*:\s*(?P<value>.+)$")

_FACT_TYPE_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("configuration", ("bundle id", "service id", "redirect uri", "url scheme", "project id", "callback", "client id", "endpoint", "host")),
    ("ownership", ("owner", "approver", "approval", "sign-off", "responsible")),
    ("rollout_constraint", ("rollout", "migration", "release", "deploy", "cutover", "rollback")),
    ("security_policy", ("session", "token", "auth", "security", "revocation", "expiration", "rotation")),
    ("merge_policy", ("merge", "duplicate", "stale", "conflict", "retry", "failure policy")),
)
_INLINE_CONFIGURATION_ALIASES: dict[str, tuple[str, ...]] = {
    "production_bundle_id": ("production bundle id",),
    "staging_bundle_id": ("staging bundle id",),
    "bundle_id": ("bundle id",),
    "service_id": ("service id",),
    "redirect_uri": ("redirect uri", "callback url"),
    "url_scheme": ("url scheme",),
    "project_id": ("project id",),
}
_KNOWLEDGE_EMBEDDING_MODEL_DEFAULT = "BAAI/bge-small-en-v1.5"
_REFERENCE_FACT_SLOT_NAME = "reference_fact"
_SLOT_NAME_MAX_LENGTH = 128
_EMBEDDING_MODEL_RETRY_COOLDOWN_SECONDS = 300
_embedding_model_unavailable_until_epoch: float = 0.0


@dataclass(frozen=True)
class KnowledgePromptContext:
    text: str
    citations: list[dict[str, Any]]


class KnowledgeEmbeddingAccessMode(str, Enum):
    BEST_EFFORT = "best_effort"
    LOCAL_ONLY = "local_only"


@dataclass(frozen=True)
class SlotResolution:
    slot_name: str
    slot_value: str
    source_timestamp: datetime | None
    confidence: float
    citation: dict[str, Any]
    inferred: bool


@dataclass(frozen=True)
class KnowledgeSyncResult:
    created_assets: int = 0
    updated_assets: int = 0
    unchanged_assets: int = 0
    deleted_assets: int = 0
    skipped_assets: int = 0
    failed_assets: int = 0
    details: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed_assets == 0

    @property
    def synced_assets(self) -> int:
        return self.created_assets + self.updated_assets


@dataclass(frozen=True)
class KnowledgeDebugMatch:
    layer: str
    score: float
    asset_id: str
    source_type: str
    title: str
    source_ref: str | None
    source_timestamp: str | None
    fact_id: str | None = None
    chunk_id: str | None = None
    snippet: str = ""
    metadata: dict[str, Any] | None = None


def approval_state_for_asset_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "ready":
        return "approved"
    if normalized == "rejected":
        return "rejected"
    return "pending_review"


def _normalize_fact_key(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")
    return normalized[:128] or "fact"


def _normalize_fact_slot_name(*, fact_type: str, slot_name: str | None, fact_key: str) -> str:
    canonical_slot = normalize_slot_name(slot_name or "")
    if canonical_slot:
        return canonical_slot
    if fact_type == "decision_slot":
        return normalize_slot_name(fact_key) or _normalize_fact_key(fact_key)[:_SLOT_NAME_MAX_LENGTH] or "fact"
    return _REFERENCE_FACT_SLOT_NAME


def _classify_fact_type(*, label: str) -> str:
    normalized = str(label or "").strip().lower()
    for fact_type, keywords in _FACT_TYPE_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return fact_type
    return "reference_fact"


def extract_decision_facts(text: str) -> list[dict[str, Any]]:
    normalized = str(text or "").strip()
    if not normalized:
        return []

    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def add_fact(
        *,
        fact_type: str,
        fact_key: str,
        fact_value: str,
        confidence: float,
        is_inferred: bool,
        metadata_json: dict[str, Any] | None = None,
        slot_name: str | None = None,
    ) -> None:
        clean_key = _normalize_fact_key(fact_key)
        clean_value = str(fact_value or "").strip()
        if not clean_key or not clean_value:
            return
        dedupe_key = (fact_type, clean_key, clean_value)
        if dedupe_key in seen:
            return
        seen.add(dedupe_key)
        resolved_slot_name = _normalize_fact_slot_name(
            fact_type=fact_type,
            slot_name=slot_name,
            fact_key=clean_key,
        )
        facts.append(
            {
                "fact_type": fact_type,
                "fact_key": clean_key,
                "fact_value": clean_value,
                "confidence": confidence,
                "is_inferred": is_inferred,
                "slot_name": resolved_slot_name[:_SLOT_NAME_MAX_LENGTH],
                "slot_value": clean_value,
                "metadata_json": dict(metadata_json or {}),
            }
        )

    for slot_name, slot_value in extract_slot_facts(normalized).items():
        add_fact(
            fact_type="decision_slot",
            fact_key=slot_name,
            fact_value=slot_value,
            confidence=0.95,
            is_inferred=False,
            metadata_json={"extraction_method": "slot_alias"},
            slot_name=slot_name,
        )

    lines = [line.strip() for line in normalized.splitlines()]
    for line in lines:
        match = _LABELED_FACT_PATTERN.match(line)
        if not match:
            continue
        label = str(match.group("label") or "").strip()
        value = str(match.group("value") or "").strip()
        if not label or not value:
            continue
        slot_name = normalize_slot_name(label)
        generic_fact_type = _classify_fact_type(label=label)
        if slot_name:
            add_fact(
                fact_type="decision_slot",
                fact_key=slot_name,
                fact_value=value,
                confidence=0.88,
                is_inferred=False,
                metadata_json={"label": label, "extraction_method": "labeled_line"},
                slot_name=slot_name,
            )
        add_fact(
            fact_type=generic_fact_type,
            fact_key=label,
            fact_value=value,
            confidence=0.78,
            is_inferred=False,
            metadata_json={"label": label, "extraction_method": "labeled_line"},
            slot_name=slot_name,
        )

    for fact_key, aliases in _INLINE_CONFIGURATION_ALIASES.items():
        for alias in aliases:
            pattern = re.compile(rf"(?i)\b{re.escape(alias)}\b\s*(?:is|=)\s*([^\n.;]+)")
            match = pattern.search(normalized)
            if not match:
                continue
            add_fact(
                fact_type="configuration",
                fact_key=fact_key,
                fact_value=match.group(1).strip(),
                confidence=0.74,
                is_inferred=False,
                metadata_json={"label": alias, "extraction_method": "inline_is"},
                slot_name=normalize_slot_name(fact_key),
            )
            break

    return facts


def normalize_slot_name(value: str) -> str | None:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return None
    normalized = normalized.replace("-", " ").replace("_", " ")
    for slot_name, aliases in SLOT_ALIASES.items():
        if normalized == slot_name.replace("_", " "):
            return slot_name
        for alias in aliases:
            if normalized == alias:
                return slot_name
    for slot_name, aliases in SLOT_ALIASES.items():
        search_terms = (slot_name.replace("_", " "), *aliases)
        if any(term in normalized for term in search_terms):
            return slot_name
    return None


def parse_source_timestamp(raw: str | None) -> datetime | None:
    candidate = str(raw or "").strip()
    if not candidate:
        return None
    try:
        parsed = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def decode_base64_content(raw_value: str | None) -> bytes | None:
    candidate = str(raw_value or "").strip()
    if not candidate:
        return None
    try:
        return b64decode(candidate, validate=True)
    except Exception:  # noqa: BLE001
        return None


def _chunk_text(text: str, *, max_chars: int = 900, overlap_chars: int = 120) -> list[str]:
    normalized = str(text or "").strip()
    if not normalized:
        return []
    if len(normalized) <= max_chars:
        return [normalized]
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(len(normalized), start + max_chars)
        candidate = normalized[start:end]
        if end < len(normalized):
            split = candidate.rfind("\n")
            if split > max_chars // 3:
                candidate = candidate[:split].strip()
                end = start + split
        cleaned = candidate.strip()
        if cleaned:
            chunks.append(cleaned)
        if end >= len(normalized):
            break
        start = max(0, end - overlap_chars)
    return chunks


def _extract_text_from_binary(*, content: bytes | None, mime_type: str | None, title: str) -> str:
    if not content:
        return ""
    normalized_mime = str(mime_type or "").strip().lower()
    lower_title = title.lower()

    if normalized_mime.startswith("text/") or lower_title.endswith((".txt", ".md", ".markdown", ".json", ".yaml", ".yml")):
        try:
            return content.decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""

    if normalized_mime == "application/pdf" or lower_title.endswith(".pdf"):
        try:
            from pypdf import PdfReader  # type: ignore[import-not-found]
            from io import BytesIO

            reader = PdfReader(BytesIO(content))
            pages = [str(page.extract_text() or "").strip() for page in reader.pages]
            return "\n\n".join(page for page in pages if page)
        except Exception:  # noqa: BLE001
            return ""

    if normalized_mime in {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    } or lower_title.endswith((".docx", ".doc")):
        try:
            from docx import Document  # type: ignore[import-not-found]
            from io import BytesIO

            document = Document(BytesIO(content))
            return "\n".join(paragraph.text.strip() for paragraph in document.paragraphs if paragraph.text.strip())
        except Exception:  # noqa: BLE001
            return ""

    if normalized_mime.startswith("image/") or lower_title.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff")):
        try:
            from PIL import Image  # type: ignore[import-not-found]
            import pytesseract  # type: ignore[import-not-found]
            from io import BytesIO

            image = Image.open(BytesIO(content))
            return str(pytesseract.image_to_string(image) or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    return ""


def _extract_labeled_fact(text: str, aliases: tuple[str, ...]) -> str | None:
    lines = [line.strip() for line in text.splitlines()]
    if not lines:
        return None
    for idx, line in enumerate(lines):
        for alias in aliases:
            pattern = rf"(?i)^{re.escape(alias)}\s*:\s*(.+)$"
            match = re.match(pattern, line)
            if match:
                value = match.group(1).strip()
                if value:
                    return value
                # If the label exists but value is on following lines, capture up to 4 lines.
                continuation: list[str] = []
                for next_line in lines[idx + 1 : idx + 5]:
                    if not next_line:
                        break
                    if _SECTION_PATTERN.match(next_line):
                        break
                    continuation.append(next_line)
                if continuation:
                    return " ".join(continuation).strip()
    return None


def extract_slot_facts(text: str) -> dict[str, str]:
    normalized = str(text or "").strip()
    if not normalized:
        return {}
    extracted: dict[str, str] = {}
    for slot_name, aliases in SLOT_ALIASES.items():
        preferred_aliases = (slot_name.replace("_", " "), *aliases)
        value = _extract_labeled_fact(normalized, preferred_aliases)
        if value:
            extracted[slot_name] = value
            continue
        # Markdown heading fallback: "# Objective" + body until next heading.
        for alias in preferred_aliases:
            pattern = re.compile(
                rf"(?is)(?:^|\n)#+\s*{re.escape(alias)}\s*\n(.+?)(?=\n#|\Z)"
            )
            match = pattern.search(normalized)
            if match:
                candidate = " ".join(line.strip() for line in match.group(1).splitlines() if line.strip())
                if candidate:
                    extracted[slot_name] = candidate
                    break
    return extracted


def _candidate_tokens(value: str) -> set[str]:
    return {token for token in _WORD_PATTERN.findall(str(value or "").lower()) if len(token) >= 2}


def _lexical_score(*, query_tokens: set[str], text: str) -> float:
    if not query_tokens:
        return 0.0
    text_tokens = _candidate_tokens(text)
    if not text_tokens:
        return 0.0
    overlap = len(query_tokens.intersection(text_tokens))
    if overlap == 0:
        return 0.0
    return overlap / max(len(query_tokens), 1)


def _cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embed_texts(
    texts: list[str],
    *,
    embedding_access_mode: KnowledgeEmbeddingAccessMode = KnowledgeEmbeddingAccessMode.BEST_EFFORT,
) -> list[list[float] | None]:
    if not texts:
        return []
    if _embedding_model_retry_suppressed():
        return [None for _ in texts]
    try:
        model = _knowledge_text_embedding_model(
            _local_files_only_for_embedding_access_mode(embedding_access_mode)
        )
    except Exception:  # noqa: BLE001
        _mark_embedding_model_unavailable()
        return [None for _ in texts]
    try:
        vectors = list(model.embed(texts))
    except Exception:  # noqa: BLE001
        _mark_embedding_model_unavailable()
        return [None for _ in texts]
    normalized: list[list[float] | None] = []
    for vector in vectors:
        if vector is None:
            normalized.append(None)
            continue
        normalized.append([float(value) for value in vector])
    while len(normalized) < len(texts):
        normalized.append(None)
    return normalized[: len(texts)]


def _embedding_model_retry_suppressed(*, now_epoch: float | None = None) -> bool:
    now_value = float(now_epoch) if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    return now_value < _embedding_model_unavailable_until_epoch


def _mark_embedding_model_unavailable(*, now_epoch: float | None = None) -> None:
    global _embedding_model_unavailable_until_epoch
    now_value = float(now_epoch) if now_epoch is not None else datetime.now(timezone.utc).timestamp()
    _embedding_model_unavailable_until_epoch = max(
        _embedding_model_unavailable_until_epoch,
        now_value + float(_EMBEDDING_MODEL_RETRY_COOLDOWN_SECONDS),
    )


def ensure_knowledge_embedding_model_ready(*, local_files_only: bool | None = None) -> str:
    model_name = _knowledge_embedding_model_name()
    model = _knowledge_text_embedding_model(local_files_only)
    list(model.embed(["knowledge prewarm"]))
    return model_name


@lru_cache(maxsize=3)
def _knowledge_text_embedding_model(local_files_only: bool | None = None):  # noqa: ANN202
    return _build_knowledge_text_embedding_model(local_files_only=local_files_only)


def _build_knowledge_text_embedding_model(*, local_files_only: bool | None = None):  # noqa: ANN202
    _suppress_known_onnxruntime_warning_noise()
    from fastembed import TextEmbedding  # type: ignore[import-not-found]

    kwargs: dict[str, Any] = {}
    if (cache_dir := _knowledge_embedding_cache_dir()) is not None:
        kwargs["cache_dir"] = cache_dir
    if _resolve_knowledge_embedding_local_files_only(local_files_only):
        kwargs["local_files_only"] = True
    return TextEmbedding(
        model_name=_knowledge_embedding_model_name(),
        **kwargs,
    )


def _suppress_known_onnxruntime_warning_noise() -> None:
    try:
        onnxruntime = importlib.import_module("onnxruntime")
    except Exception:  # noqa: BLE001
        return
    set_default_logger_severity = getattr(onnxruntime, "set_default_logger_severity", None)
    if not callable(set_default_logger_severity):
        return
    try:
        set_default_logger_severity(logging.ERROR)
    except Exception:  # noqa: BLE001
        return


def _knowledge_embedding_model_name() -> str:
    try:
        configured = str(get_settings().knowledge_embedding_model or "").strip()
    except Exception:  # noqa: BLE001
        configured = ""
    return configured or _KNOWLEDGE_EMBEDDING_MODEL_DEFAULT


def _knowledge_embedding_cache_dir() -> str | None:
    normalized = str(os.environ.get("HF_HOME") or "").strip()
    return normalized or None


def _knowledge_embedding_offline_enabled() -> bool:
    normalized = str(os.environ.get("HF_HUB_OFFLINE") or "").strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _resolve_knowledge_embedding_local_files_only(local_files_only: bool | None) -> bool:
    if local_files_only is not None:
        return local_files_only
    return _knowledge_embedding_offline_enabled()


def _local_files_only_for_embedding_access_mode(
    embedding_access_mode: KnowledgeEmbeddingAccessMode,
) -> bool | None:
    _ = embedding_access_mode
    return True


def _asset_checksum(*, text_content: str, binary_content: bytes | None) -> str | None:
    checksum_input = binary_content if binary_content else text_content.encode("utf-8", errors="ignore")
    return hashlib.sha256(checksum_input).hexdigest() if checksum_input else None


def _attachment_extension(filename: str) -> str:
    lowered = str(filename or "").strip().lower()
    if "." not in lowered:
        return ""
    return "." + lowered.rsplit(".", 1)[-1]


def _is_supported_text_attachment(*, filename: str, mime_type: str | None, size_bytes: int | None) -> bool:
    if isinstance(size_bytes, int) and size_bytes > 5 * 1024 * 1024:
        return False
    normalized_mime = str(mime_type or "").strip().lower()
    extension = _attachment_extension(filename)
    if normalized_mime.startswith("text/"):
        return True
    if normalized_mime in {
        "application/pdf",
        "application/json",
        "application/yaml",
        "application/x-yaml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/msword",
    }:
        return True
    if normalized_mime.startswith("image/"):
        return True
    return extension in {
        ".txt",
        ".md",
        ".markdown",
        ".json",
        ".yaml",
        ".yml",
        ".pdf",
        ".doc",
        ".docx",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
    }


def _jira_issue_asset_ref(issue_key: str) -> str:
    return f"issue:{issue_key}"


def _jira_comment_asset_ref(issue_key: str, comment_id: str) -> str:
    return f"comment:{issue_key}:{comment_id}"


def _jira_attachment_asset_ref(issue_key: str, attachment_id: str) -> str:
    return f"attachment:{issue_key}:{attachment_id}"


def _store_asset_facts(
    *,
    session: Session,
    asset: KnowledgeAsset,
    extracted_text: str,
    source_timestamp: datetime | None,
    now: datetime,
) -> None:
    approval_state = approval_state_for_asset_status(asset.status)
    for fact_payload in extract_decision_facts(extracted_text):
        session.add(
            KnowledgeFact(
                fact_id=uuid4().hex,
                asset_id=asset.asset_id,
                chunk_id=None,
                tenant_id=asset.tenant_id,
                project_id=asset.project_id,
                fact_type=str(fact_payload["fact_type"]),
                fact_key=str(fact_payload["fact_key"]),
                fact_value=str(fact_payload["fact_value"]),
                approval_state=approval_state,
                slot_name=str(fact_payload["slot_name"]),
                slot_value=str(fact_payload["slot_value"]),
                confidence=float(fact_payload["confidence"]),
                is_inferred=bool(fact_payload["is_inferred"]),
                metadata_json=dict(fact_payload.get("metadata_json") or {}),
                source_timestamp=source_timestamp,
                superseded_at=None,
                created_at=now,
                updated_at=now,
            )
        )


def sync_knowledge_fact_approval_state_for_asset(*, session: Session, asset: KnowledgeAsset) -> None:
    approval_state = approval_state_for_asset_status(asset.status)
    now = datetime.now(timezone.utc)
    facts = session.execute(
        select(KnowledgeFact).where(KnowledgeFact.asset_id == asset.asset_id)
    ).scalars().all()
    for fact in facts:
        fact.approval_state = approval_state
        if approval_state == "rejected":
            fact.superseded_at = fact.superseded_at or now
        else:
            fact.superseded_at = None
        fact.updated_at = now


def _refresh_asset_content(
    *,
    session: Session,
    asset: KnowledgeAsset,
    title: str,
    mime_type: str | None,
    source_timestamp: datetime | None,
    text_content: str | None,
    binary_content: bytes | None,
    metadata_json: dict[str, Any],
) -> None:
    now = datetime.now(timezone.utc)
    normalized_title = str(title or "").strip()[:255] or "Untitled asset"
    normalized_mime_type = str(mime_type or "").strip()[:128] or None
    extracted_text = str(text_content or "").strip()
    if not extracted_text:
        extracted_text = _extract_text_from_binary(
            content=binary_content,
            mime_type=normalized_mime_type,
            title=normalized_title,
        )

    asset.title = normalized_title
    asset.mime_type = normalized_mime_type
    asset.source_timestamp = source_timestamp
    asset.text_content = extracted_text or None
    asset.binary_content = binary_content
    asset.checksum = _asset_checksum(text_content=extracted_text, binary_content=binary_content)
    asset.metadata_json = dict(metadata_json)
    asset.status = "ready"
    asset.updated_at = now

    session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.asset_id == asset.asset_id))
    session.execute(delete(KnowledgeFact).where(KnowledgeFact.asset_id == asset.asset_id))
    session.flush()

    chunks = _chunk_text(extracted_text)
    embeddings = _embed_texts(chunks)
    for idx, chunk in enumerate(chunks):
        session.add(
            KnowledgeChunk(
                chunk_id=uuid4().hex,
                asset_id=asset.asset_id,
                tenant_id=asset.tenant_id,
                project_id=asset.project_id,
                chunk_index=idx,
                content=chunk,
                token_count=max(1, len(chunk) // 4),
                embedding=embeddings[idx] if idx < len(embeddings) else None,
                source_timestamp=source_timestamp,
                created_at=now,
                updated_at=now,
            )
        )
    asset.chunk_count = len(chunks)
    _store_asset_facts(
        session=session,
        asset=asset,
        extracted_text=extracted_text,
        source_timestamp=source_timestamp,
        now=now,
    )


def _upsert_knowledge_asset(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    source_type: str,
    source_ref: str,
    title: str,
    mime_type: str | None,
    source_timestamp: datetime | None,
    text_content: str | None,
    binary_content: bytes | None,
    metadata_json: dict[str, Any] | None = None,
) -> str:
    metadata = dict(metadata_json or {})
    normalized_source_type = str(source_type or "").strip().lower()
    normalized_source_ref = str(source_ref or "").strip()
    if not normalized_source_type or not normalized_source_ref:
        raise ValueError("Upserted knowledge assets require source_type and source_ref")

    normalized_title = str(title or "").strip()[:255]
    extracted_text = str(text_content or "").strip()
    if not extracted_text:
        extracted_text = _extract_text_from_binary(
            content=binary_content,
            mime_type=mime_type,
            title=normalized_title,
        )
    checksum = _asset_checksum(text_content=extracted_text, binary_content=binary_content)
    existing = session.execute(
        select(KnowledgeAsset).where(
            KnowledgeAsset.tenant_id == tenant_id,
            KnowledgeAsset.project_id == project_id,
            KnowledgeAsset.source_type == normalized_source_type,
            KnowledgeAsset.source_ref == normalized_source_ref,
        )
    ).scalar_one_or_none()
    if existing is None:
        create_knowledge_asset(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            source_type=normalized_source_type,
            title=normalized_title,
            mime_type=mime_type,
            source_ref=normalized_source_ref,
            source_timestamp=source_timestamp,
            text_content=extracted_text,
            binary_content=binary_content,
            metadata_json=metadata,
            status="ready",
            commit=False,
        )
        return "created"

    if (
        existing.status != "deleted"
        and existing.checksum == checksum
        and dict(existing.metadata_json or {}) == metadata
        and existing.title == (normalized_title or "Untitled asset")
        and existing.mime_type == (str(mime_type or "").strip()[:128] or None)
        and existing.source_timestamp == source_timestamp
    ):
        if existing.status != "ready":
            existing.status = "ready"
            existing.updated_at = datetime.now(timezone.utc)
            return "updated"
        return "unchanged"

    _refresh_asset_content(
        session=session,
        asset=existing,
        title=normalized_title,
        mime_type=mime_type,
        source_timestamp=source_timestamp,
        text_content=extracted_text,
        binary_content=binary_content,
        metadata_json=metadata,
    )
    return "updated"


def list_knowledge_assets(*, session: Session, tenant_id: str, project_id: str) -> list[KnowledgeAsset]:
    return session.execute(
        select(KnowledgeAsset)
        .where(
            KnowledgeAsset.tenant_id == tenant_id,
            KnowledgeAsset.project_id == project_id,
            KnowledgeAsset.status != "deleted",
        )
        .order_by(desc(KnowledgeAsset.updated_at))
    ).scalars().all()


def list_knowledge_assets_page(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    status: str | None = None,
    source_type: str | None = None,
    query: str | None = None,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[KnowledgeAsset], int]:
    normalized_status = str(status or "").strip().lower() or None
    normalized_source_type = str(source_type or "").strip().lower() or None
    normalized_query = str(query or "").strip().lower() or None
    safe_limit = max(1, min(limit, 100))
    safe_offset = max(0, offset)

    filters = [
        KnowledgeAsset.tenant_id == tenant_id,
        KnowledgeAsset.project_id == project_id,
        KnowledgeAsset.status != "deleted",
    ]
    if normalized_status:
        filters.append(KnowledgeAsset.status == normalized_status)
    if normalized_source_type:
        filters.append(KnowledgeAsset.source_type == normalized_source_type)
    if normalized_query:
        wildcard_query = f"%{normalized_query}%"
        filters.append(
            func.lower(func.coalesce(KnowledgeAsset.title, "")).like(wildcard_query)
            | func.lower(func.coalesce(KnowledgeAsset.source_ref, "")).like(wildcard_query)
        )

    total = session.execute(select(func.count()).select_from(KnowledgeAsset).where(*filters)).scalar_one()
    items = session.execute(
        select(KnowledgeAsset)
        .where(*filters)
        .order_by(desc(KnowledgeAsset.updated_at), desc(KnowledgeAsset.created_at))
        .limit(safe_limit)
        .offset(safe_offset)
    ).scalars().all()
    return items, int(total)


def list_knowledge_chunks_page(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    asset_id: str,
    limit: int = 20,
    offset: int = 0,
) -> tuple[list[KnowledgeChunk], int]:
    safe_limit = max(1, min(limit, 100))
    safe_offset = max(0, offset)
    filters = [
        KnowledgeChunk.tenant_id == tenant_id,
        KnowledgeChunk.project_id == project_id,
        KnowledgeChunk.asset_id == asset_id,
    ]
    total = session.execute(select(func.count()).select_from(KnowledgeChunk).where(*filters)).scalar_one()
    items = session.execute(
        select(KnowledgeChunk)
        .where(*filters)
        .order_by(KnowledgeChunk.chunk_index.asc())
        .limit(safe_limit)
        .offset(safe_offset)
    ).scalars().all()
    return items, int(total)


def list_knowledge_facts_for_asset(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    asset_id: str,
) -> list[KnowledgeFact]:
    return session.execute(
        select(KnowledgeFact)
        .where(
            KnowledgeFact.tenant_id == tenant_id,
            KnowledgeFact.project_id == project_id,
            KnowledgeFact.asset_id == asset_id,
        )
        .order_by(
            case((KnowledgeFact.superseded_at.is_(None), 0), else_=1),
            KnowledgeFact.approval_state.asc(),
            KnowledgeFact.fact_type.asc(),
            KnowledgeFact.fact_key.asc(),
            desc(KnowledgeFact.source_timestamp),
            desc(KnowledgeFact.updated_at),
        )
    ).scalars().all()


def get_knowledge_asset_stats(*, session: Session, tenant_id: str, project_id: str) -> dict[str, Any]:
    filters = [
        KnowledgeAsset.tenant_id == tenant_id,
        KnowledgeAsset.project_id == project_id,
        KnowledgeAsset.status != "deleted",
    ]
    totals = session.execute(
        select(
            func.count(KnowledgeAsset.asset_id),
            func.coalesce(func.sum(KnowledgeAsset.chunk_count), 0),
            func.max(KnowledgeAsset.updated_at),
        ).where(*filters)
    ).one()
    status_counts = dict(
        session.execute(
            select(KnowledgeAsset.status, func.count(KnowledgeAsset.asset_id))
            .where(*filters)
            .group_by(KnowledgeAsset.status)
        ).all()
    )
    source_type_counts = dict(
        session.execute(
            select(KnowledgeAsset.source_type, func.count(KnowledgeAsset.asset_id))
            .where(*filters)
            .group_by(KnowledgeAsset.source_type)
        ).all()
    )
    fact_totals = session.execute(
        select(
            func.count(KnowledgeFact.fact_id),
            func.coalesce(func.sum(case((KnowledgeFact.approval_state == "approved", 1), else_=0)), 0),
            func.coalesce(func.sum(case((KnowledgeFact.approval_state == "pending_review", 1), else_=0)), 0),
            func.coalesce(func.sum(case((KnowledgeFact.superseded_at.is_not(None), 1), else_=0)), 0),
        )
        .where(
            KnowledgeFact.tenant_id == tenant_id,
            KnowledgeFact.project_id == project_id,
        )
    ).one()
    return {
        "total_assets": int(totals[0] or 0),
        "total_chunks": int(totals[1] or 0),
        "total_facts": int(fact_totals[0] or 0),
        "approved_facts": int(fact_totals[1] or 0),
        "pending_review_facts": int(fact_totals[2] or 0),
        "superseded_facts": int(fact_totals[3] or 0),
        "latest_asset_updated_at": totals[2],
        "ready_assets": int(status_counts.get("ready", 0)),
        "pending_review_assets": int(status_counts.get("pending_review", 0)),
        "rejected_assets": int(status_counts.get("rejected", 0)),
        "source_type_counts": {str(key): int(value) for key, value in source_type_counts.items()},
    }


def create_knowledge_asset(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    source_type: str,
    title: str,
    mime_type: str | None = None,
    source_ref: str | None = None,
    source_timestamp: datetime | None = None,
    text_content: str | None = None,
    binary_content: bytes | None = None,
    metadata_json: dict[str, Any] | None = None,
    status: str = "ready",
    commit: bool = True,
) -> KnowledgeAsset:
    now = datetime.now(timezone.utc)
    normalized_title = str(title or "").strip()[:255]
    normalized_source_type = str(source_type or "").strip().lower() or "manual"
    normalized_mime_type = str(mime_type or "").strip()[:128] or None
    normalized_source_ref = str(source_ref or "").strip()[:1024] or None
    extracted_text = str(text_content or "").strip()
    if not extracted_text:
        extracted_text = _extract_text_from_binary(
            content=binary_content,
            mime_type=normalized_mime_type,
            title=normalized_title,
        )
    checksum = _asset_checksum(text_content=extracted_text, binary_content=binary_content)

    asset = KnowledgeAsset(
        asset_id=uuid4().hex,
        tenant_id=tenant_id,
        project_id=project_id,
        source_type=normalized_source_type,
        title=normalized_title or "Untitled asset",
        mime_type=normalized_mime_type,
        source_ref=normalized_source_ref,
        source_timestamp=source_timestamp,
        checksum=checksum,
        text_content=extracted_text or None,
        binary_content=binary_content,
        chunk_count=0,
        status=str(status or "ready").strip() or "ready",
        metadata_json=dict(metadata_json or {}),
        created_at=now,
        updated_at=now,
    )
    session.add(asset)
    session.flush()

    chunks = _chunk_text(extracted_text)
    embeddings = _embed_texts(chunks)
    for idx, chunk in enumerate(chunks):
        chunk_model = KnowledgeChunk(
            chunk_id=uuid4().hex,
            asset_id=asset.asset_id,
            tenant_id=tenant_id,
            project_id=project_id,
            chunk_index=idx,
            content=chunk,
            token_count=max(1, len(chunk) // 4),
            embedding=embeddings[idx] if idx < len(embeddings) else None,
            source_timestamp=source_timestamp,
            created_at=now,
            updated_at=now,
        )
        session.add(chunk_model)
    asset.chunk_count = len(chunks)
    asset.updated_at = now
    _store_asset_facts(
        session=session,
        asset=asset,
        extracted_text=extracted_text,
        source_timestamp=source_timestamp,
        now=now,
    )

    if commit:
        session.commit()
        session.refresh(asset)
    return asset


def delete_knowledge_asset(*, session: Session, tenant_id: str, project_id: str, asset_id: str) -> bool:
    asset = session.get(KnowledgeAsset, asset_id)
    if asset is None:
        return False
    if asset.tenant_id != tenant_id or asset.project_id != project_id:
        return False
    asset.status = "deleted"
    asset.updated_at = datetime.now(timezone.utc)
    session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.asset_id == asset_id))
    session.execute(delete(KnowledgeFact).where(KnowledgeFact.asset_id == asset_id))
    session.commit()
    return True


def replace_knowledge_asset_text(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    asset_id: str,
    title: str,
    text_content: str,
    metadata_json: dict[str, Any] | None = None,
    status: str = "ready",
    commit: bool = True,
) -> KnowledgeAsset:
    asset = session.get(KnowledgeAsset, asset_id)
    if asset is None or asset.tenant_id != tenant_id or asset.project_id != project_id:
        raise ValueError("Knowledge asset not found")
    _refresh_asset_content(
        session=session,
        asset=asset,
        title=title,
        mime_type="text/markdown",
        source_timestamp=datetime.now(timezone.utc),
        text_content=text_content,
        binary_content=None,
        metadata_json=dict(metadata_json or {}),
    )
    asset.status = str(status or "ready").strip() or "ready"
    if commit:
        session.commit()
        session.refresh(asset)
    return asset


def sync_project_knowledge_from_jira(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    project_key: str,
    jira_client,  # noqa: ANN001
    access_token: str,
    cloud_id: str,
    max_issues: int = 500,
) -> KnowledgeSyncResult:
    normalized_project_key = str(project_key or "").strip().upper()
    if not normalized_project_key:
        return KnowledgeSyncResult(details="Jira project key is not configured")

    counts = {
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "deleted": 0,
        "skipped": 0,
        "failed": 0,
    }
    active_refs: set[str] = set()
    page_size = max(1, min(max_issues, 50))
    remaining = max(1, max_issues)
    start_at = 0

    while remaining > 0:
        requested = min(page_size, remaining)
        previews = jira_client.search_issues_by_jql(
            access_token=access_token,
            cloud_id=cloud_id,
            jql=f'project = "{normalized_project_key}" ORDER BY updated DESC',
            max_results=requested,
            start_at=start_at,
        )
        if not previews:
            break

        for preview in previews:
            if remaining <= 0:
                break
            remaining -= 1
            issue_key = str(getattr(preview, "key", "") or "").strip().upper()
            if not issue_key:
                counts["skipped"] += 1
                continue
            try:
                detail = jira_client.get_issue_detail(
                    access_token=access_token,
                    cloud_id=cloud_id,
                    issue_id_or_key=issue_key,
                )
                comments = jira_client.list_issue_comments(
                    access_token=access_token,
                    cloud_id=cloud_id,
                    issue_id_or_key=issue_key,
                )
                attachments = jira_client.list_issue_attachments(
                    access_token=access_token,
                    cloud_id=cloud_id,
                    issue_id_or_key=issue_key,
                )
            except Exception:  # noqa: BLE001
                counts["failed"] += 1
                continue

            summary = str(getattr(detail, "summary", "") or "").strip()
            description = str(getattr(detail, "description", "") or "").strip()
            status = str(getattr(detail, "status", "") or "").strip()
            labels = [str(label).strip() for label in list(getattr(detail, "labels", []) or []) if str(label).strip()]
            issue_body_text = "\n".join(
                line
                for line in [
                    f"Issue: {issue_key}",
                    f"Status: {status}",
                    f"Summary: {summary}",
                    f"Labels: {', '.join(labels)}" if labels else None,
                    "",
                    "Description:",
                    description,
                ]
                if line is not None
            ).strip()
            issue_ref = _jira_issue_asset_ref(issue_key)
            active_refs.add(issue_ref)
            counts[_upsert_knowledge_asset(
                session=session,
                tenant_id=tenant_id,
                project_id=project_id,
                source_type="jira_issue",
                source_ref=issue_ref,
                title=f"{issue_key}: {summary[:220] if summary else 'Jira issue'}",
                mime_type="text/plain",
                source_timestamp=datetime.now(timezone.utc),
                text_content=issue_body_text,
                binary_content=None,
                metadata_json={
                    "issue_key": issue_key,
                    "status": status,
                    "labels": labels,
                },
            )] += 1

            for comment in comments:
                comment_id = str(getattr(comment, "comment_id", "") or "").strip()
                if not comment_id:
                    counts["skipped"] += 1
                    continue
                comment_body = str(getattr(comment, "body", "") or "").strip()
                if not comment_body:
                    counts["skipped"] += 1
                    continue
                comment_ref = _jira_comment_asset_ref(issue_key, comment_id)
                active_refs.add(comment_ref)
                counts[_upsert_knowledge_asset(
                    session=session,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    source_type="jira_comment",
                    source_ref=comment_ref,
                    title=f"{issue_key} comment {comment_id}",
                    mime_type="text/plain",
                    source_timestamp=getattr(comment, "updated_at", None),
                    text_content=comment_body,
                    binary_content=None,
                    metadata_json={
                        "issue_key": issue_key,
                        "comment_id": comment_id,
                        "author_display_name": getattr(comment, "author_display_name", None),
                    },
                )] += 1

            for attachment in attachments:
                attachment_id = str(getattr(attachment, "attachment_id", "") or "").strip()
                filename = str(getattr(attachment, "filename", "") or "").strip()
                content_url = str(getattr(attachment, "content_url", "") or "").strip()
                mime_type = str(getattr(attachment, "mime_type", "") or "").strip() or mimetypes.guess_type(filename)[0]
                size_bytes = getattr(attachment, "size_bytes", None)
                if not attachment_id or not filename or not content_url:
                    counts["skipped"] += 1
                    continue
                attachment_ref = _jira_attachment_asset_ref(issue_key, attachment_id)
                active_refs.add(attachment_ref)
                if not _is_supported_text_attachment(
                    filename=filename,
                    mime_type=mime_type,
                    size_bytes=size_bytes if isinstance(size_bytes, int) else None,
                ):
                    counts["skipped"] += 1
                    continue
                try:
                    content_bytes = jira_client.download_attachment(
                        access_token=access_token,
                        content_url=content_url,
                    )
                except Exception:  # noqa: BLE001
                    counts["failed"] += 1
                    continue
                extracted_text = _extract_text_from_binary(
                    content=content_bytes,
                    mime_type=mime_type,
                    title=filename,
                ).strip()
                if not extracted_text:
                    counts["skipped"] += 1
                    continue
                counts[_upsert_knowledge_asset(
                    session=session,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    source_type="jira_attachment",
                    source_ref=attachment_ref,
                    title=f"{issue_key} attachment {filename}",
                    mime_type=mime_type,
                    source_timestamp=getattr(attachment, "created_at", None),
                    text_content=extracted_text,
                    binary_content=content_bytes,
                    metadata_json={
                        "issue_key": issue_key,
                        "attachment_id": attachment_id,
                        "filename": filename,
                        "content_url": content_url,
                        "size_bytes": size_bytes,
                    },
                )] += 1

        start_at += len(previews)
        if len(previews) < requested:
            break

    existing_assets = session.execute(
        select(KnowledgeAsset).where(
            KnowledgeAsset.tenant_id == tenant_id,
            KnowledgeAsset.project_id == project_id,
            KnowledgeAsset.source_type.in_(("jira_issue", "jira_comment", "jira_attachment")),
            KnowledgeAsset.status != "deleted",
        )
    ).scalars().all()
    now = datetime.now(timezone.utc)
    for asset in existing_assets:
        source_ref = str(asset.source_ref or "").strip()
        if source_ref and source_ref in active_refs:
            continue
        asset.status = "deleted"
        asset.updated_at = now
        session.execute(delete(KnowledgeChunk).where(KnowledgeChunk.asset_id == asset.asset_id))
        session.execute(delete(KnowledgeFact).where(KnowledgeFact.asset_id == asset.asset_id))
        counts["deleted"] += 1

    session.commit()
    return KnowledgeSyncResult(
        created_assets=counts["created"],
        updated_assets=counts["updated"],
        unchanged_assets=counts["unchanged"],
        deleted_assets=counts["deleted"],
        skipped_assets=counts["skipped"],
        failed_assets=counts["failed"],
    )


def _mode_thresholds(mode: str) -> tuple[float, float]:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode == "safe":
        return (0.90, 0.80)
    if normalized_mode == "balanced":
        return (0.78, 0.65)
    return (0.55, 0.50)


def _match_facts(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    normalized_query: str,
    max_items: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    query_tokens = _candidate_tokens(normalized_query)
    if not query_tokens:
        return [], []
    rows = session.execute(
        select(KnowledgeFact, KnowledgeAsset)
        .join(KnowledgeAsset, KnowledgeAsset.asset_id == KnowledgeFact.asset_id)
        .where(
            KnowledgeFact.tenant_id == tenant_id,
            KnowledgeFact.project_id == project_id,
            KnowledgeFact.approval_state == "approved",
            KnowledgeFact.superseded_at.is_(None),
            KnowledgeAsset.status == "ready",
        )
        .order_by(
            desc(func.coalesce(KnowledgeFact.source_timestamp, KnowledgeAsset.source_timestamp)),
            desc(KnowledgeFact.updated_at),
        )
        .limit(max(20, max_items * 10))
    ).all()
    if not rows:
        return [], []

    now = datetime.now(timezone.utc)
    scored: list[tuple[float, KnowledgeFact, KnowledgeAsset]] = []
    for fact, asset in rows:
        candidate_text = " ".join(
            part
            for part in (
                str(fact.fact_type or "").strip(),
                str(fact.fact_key or fact.slot_name or "").strip(),
                str(fact.fact_value or fact.slot_value or "").strip(),
                str(asset.title or "").strip(),
                str(asset.source_ref or "").strip(),
            )
            if part
        )
        lexical = _lexical_score(query_tokens=query_tokens, text=candidate_text)
        if lexical <= 0.0:
            continue
        source_time = fact.source_timestamp or asset.source_timestamp
        recency_bonus = 0.0
        if source_time is not None:
            age_days = max(0.0, (now - source_time.astimezone(timezone.utc)).total_seconds() / 86400.0)
            recency_bonus = 1.0 / (1.0 + age_days / 30.0)
        approval_bonus = 0.05 if str(fact.approval_state or "") == "approved" else 0.0
        score = (lexical * 0.85) + (recency_bonus * 0.10) + approval_bonus
        if score <= 0.0:
            continue
        scored.append((score, fact, asset))

    if not scored:
        return [], []

    scored.sort(key=lambda item: item[0], reverse=True)
    lines: list[str] = []
    citations: list[dict[str, Any]] = []
    for score, fact, asset in scored[:max(1, max_items)]:
        source_time = fact.source_timestamp or asset.source_timestamp
        lines.append(
            (
                f"- Fact [{fact.fact_type}] {fact.fact_key} = {fact.fact_value} "
                f"(asset_id={asset.asset_id}, fact_id={fact.fact_id}, source_type={asset.source_type}, "
                f"source_ref={asset.source_ref or 'unknown'}, "
                f"source_timestamp={source_time.isoformat() if source_time else 'unknown'}, score={score:.3f})"
            )
        )
        citations.append(
            {
                "asset_id": asset.asset_id,
                "fact_id": fact.fact_id,
                "title": asset.title,
                "source_type": asset.source_type,
                "source_ref": asset.source_ref,
                "source_timestamp": source_time.isoformat() if source_time else None,
                "score": round(score, 6),
                "layer": "knowledge_fact",
            }
        )
    return lines, citations


def _match_chunk_debug(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    normalized_query: str,
    max_items: int,
) -> list[KnowledgeDebugMatch]:
    context = build_knowledge_prompt_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        query=normalized_query,
        max_items=max_items,
        max_chars=4000,
    )
    chunk_matches: list[KnowledgeDebugMatch] = []
    for citation in context.citations:
        if citation.get("layer") != "knowledge_chunk":
            continue
        chunk_id = str(citation.get("chunk_id") or "").strip() or None
        asset_id = str(citation.get("asset_id") or "").strip()
        if not asset_id:
            continue
        chunk = session.get(KnowledgeChunk, chunk_id) if chunk_id else None
        snippet = str(chunk.content or "").strip() if chunk is not None else ""
        if len(snippet) > 400:
            snippet = f"{snippet[:397].rstrip()}..."
        chunk_matches.append(
            KnowledgeDebugMatch(
                layer="knowledge_chunk",
                score=float(citation.get("score") or 0.0),
                asset_id=asset_id,
                chunk_id=chunk_id,
                source_type=str(citation.get("source_type") or ""),
                title=str(citation.get("title") or ""),
                source_ref=str(citation.get("source_ref") or "").strip() or None,
                source_timestamp=str(citation.get("source_timestamp") or "").strip() or None,
                snippet=snippet,
                metadata={},
            )
        )
    return chunk_matches[:max_items]


def search_knowledge_debug(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    query: str,
    limit: int = 5,
) -> list[KnowledgeDebugMatch]:
    normalized_query = str(query or "").strip()
    if not tenant_id or not project_id or not normalized_query:
        return []
    fact_lines, fact_citations = _match_facts(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        normalized_query=normalized_query,
        max_items=limit,
    )
    del fact_lines
    fact_matches = [
        KnowledgeDebugMatch(
            layer="knowledge_fact",
            score=float(citation.get("score") or 0.0),
            asset_id=str(citation.get("asset_id") or ""),
            fact_id=str(citation.get("fact_id") or "").strip() or None,
            source_type=str(citation.get("source_type") or ""),
            title=str(citation.get("title") or ""),
            source_ref=str(citation.get("source_ref") or "").strip() or None,
            source_timestamp=str(citation.get("source_timestamp") or "").strip() or None,
            snippet=str(
                (
                    session.get(KnowledgeFact, str(citation.get("fact_id") or "").strip()).fact_value
                    if citation.get("fact_id")
                    else ""
                )
                or ""
            ),
            metadata={},
        )
        for citation in fact_citations
        if str(citation.get("asset_id") or "").strip()
    ]
    chunk_matches = _match_chunk_debug(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        normalized_query=normalized_query,
        max_items=limit,
    )
    combined = [*fact_matches, *chunk_matches]
    combined.sort(key=lambda item: item.score, reverse=True)
    return combined[: max(1, limit * 2)]


def resolve_missing_slots_from_knowledge(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    missing_slots: list[str],
    mode: str,
) -> dict[str, SlotResolution]:
    normalized_slots = [normalize_slot_name(slot) for slot in missing_slots]
    candidate_slots = [slot for slot in normalized_slots if slot]
    if not candidate_slots:
        return {}
    confidence_threshold, inferred_threshold = _mode_thresholds(mode)
    results: dict[str, SlotResolution] = {}
    for slot_name in candidate_slots:
        facts = session.execute(
            select(KnowledgeFact, KnowledgeAsset)
            .join(KnowledgeAsset, KnowledgeAsset.asset_id == KnowledgeFact.asset_id)
            .where(
                KnowledgeFact.tenant_id == tenant_id,
                KnowledgeFact.project_id == project_id,
                KnowledgeFact.fact_key == slot_name,
                KnowledgeFact.fact_type == "decision_slot",
                KnowledgeFact.approval_state == "approved",
                KnowledgeFact.superseded_at.is_(None),
                KnowledgeAsset.status == "ready",
            )
            .order_by(
                desc(func.coalesce(KnowledgeFact.source_timestamp, KnowledgeAsset.source_timestamp)),
                desc(KnowledgeFact.confidence),
                desc(KnowledgeFact.updated_at),
            )
            .limit(10)
        ).all()
        if not facts:
            continue
        chosen: SlotResolution | None = None
        for fact, asset in facts:
            confidence = float(fact.confidence or 0.0)
            threshold = inferred_threshold if bool(fact.is_inferred) else confidence_threshold
            if confidence < threshold:
                continue
            candidate = SlotResolution(
                slot_name=slot_name,
                slot_value=str(fact.fact_value or fact.slot_value or "").strip(),
                source_timestamp=fact.source_timestamp or asset.source_timestamp,
                confidence=confidence,
                citation={
                    "fact_id": fact.fact_id,
                    "asset_id": asset.asset_id,
                    "title": asset.title,
                    "source_type": asset.source_type,
                    "source_timestamp": (fact.source_timestamp or asset.source_timestamp).isoformat()
                    if (fact.source_timestamp or asset.source_timestamp)
                    else None,
                },
                inferred=bool(fact.is_inferred),
            )
            if not candidate.slot_value:
                continue
            chosen = candidate
            break
        if chosen:
            results[slot_name] = chosen
    return results


def _sqlite_fallback_context(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    normalized_query: str,
    max_items: int,
    max_chars: int,
    embedding_access_mode: KnowledgeEmbeddingAccessMode,
) -> KnowledgePromptContext:
    query_tokens = _candidate_tokens(normalized_query)
    query_embedding = _embed_texts(
        [normalized_query],
        embedding_access_mode=embedding_access_mode,
    )[0]
    rows = session.execute(
        select(KnowledgeChunk, KnowledgeAsset)
        .join(KnowledgeAsset, KnowledgeAsset.asset_id == KnowledgeChunk.asset_id)
        .where(
            KnowledgeChunk.tenant_id == tenant_id,
            KnowledgeChunk.project_id == project_id,
            KnowledgeAsset.status == "ready",
        )
        .order_by(
            desc(func.coalesce(KnowledgeChunk.source_timestamp, KnowledgeAsset.source_timestamp)),
            desc(KnowledgeChunk.updated_at),
        )
        .limit(350)
    ).all()
    if not rows:
        return KnowledgePromptContext(text="", citations=[])

    scored: list[tuple[float, KnowledgeChunk, KnowledgeAsset]] = []
    for chunk, asset in rows:
        lexical = _lexical_score(query_tokens=query_tokens, text=chunk.content)
        semantic = 0.0
        chunk_embedding = chunk.embedding if isinstance(chunk.embedding, list) else None
        if (
            query_embedding
            and isinstance(chunk_embedding, list)
            and all(isinstance(item, (int, float)) for item in chunk_embedding)
        ):
            semantic = _cosine_similarity(
                [float(value) for value in query_embedding],
                [float(value) for value in chunk_embedding],
            )
        recency_bonus = 0.0
        source_time = chunk.source_timestamp or asset.source_timestamp
        if source_time is not None:
            age_days = max(
                0.0,
                (datetime.now(timezone.utc) - source_time.astimezone(timezone.utc)).total_seconds() / 86400.0,
            )
            recency_bonus = 1.0 / (1.0 + age_days / 30.0)
        score = (lexical * 0.55) + (semantic * 0.35) + (recency_bonus * 0.10)
        if score <= 0.0:
            continue
        scored.append((score, chunk, asset))
    return _format_knowledge_context(scored=scored, max_items=max_items, max_chars=max_chars)


def _format_knowledge_context(
    *,
    scored: list[tuple[float, KnowledgeChunk, KnowledgeAsset]],
    max_items: int,
    max_chars: int,
) -> KnowledgePromptContext:
    if not scored:
        return KnowledgePromptContext(text="", citations=[])
    scored.sort(key=lambda item: item[0], reverse=True)
    selected = scored[: max(1, max_items)]
    lines: list[str] = []
    citations: list[dict[str, Any]] = []
    consumed_chars = 0
    for score, chunk, asset in selected:
        source_time = chunk.source_timestamp or asset.source_timestamp
        snippet = str(chunk.content or "").strip()
        if len(snippet) > 560:
            snippet = f"{snippet[:557].rstrip()}..."
        line = (
            f"- [{asset.title}] (asset_id={asset.asset_id}, source_type={asset.source_type}, "
            f"source_ref={asset.source_ref or 'unknown'}, "
            f"source_timestamp={source_time.isoformat() if source_time else 'unknown'}, score={score:.3f})\n"
            f"  {snippet}"
        )
        if consumed_chars + len(line) > max_chars:
            break
        lines.append(line)
        consumed_chars += len(line)
        citations.append(
            {
                "asset_id": asset.asset_id,
                "chunk_id": chunk.chunk_id,
                "title": asset.title,
                "source_type": asset.source_type,
                "source_ref": asset.source_ref,
                "source_timestamp": source_time.isoformat() if source_time else None,
                "score": round(score, 6),
                "layer": "knowledge_chunk",
            }
        )
    return KnowledgePromptContext(text="\n".join(lines), citations=citations)


def _postgres_hybrid_context(
    *,
    session: Session,
    tenant_id: str,
    project_id: str,
    normalized_query: str,
    max_items: int,
    max_chars: int,
    embedding_access_mode: KnowledgeEmbeddingAccessMode,
) -> KnowledgePromptContext:
    query_tokens = _candidate_tokens(normalized_query)
    query_embedding = _embed_texts(
        [normalized_query],
        embedding_access_mode=embedding_access_mode,
    )[0]
    candidate_scores: dict[str, dict[str, float]] = {}
    if query_embedding:
        embedding_literal = vector_literal(query_embedding)
        if embedding_literal:
            vector_rows = session.execute(
                text(
                    """
                    SELECT chunk_id, 1 - (embedding <=> CAST(:embedding AS vector)) AS semantic_score
                    FROM knowledge_chunks
                    WHERE tenant_id = :tenant_id
                      AND project_id = :project_id
                      AND embedding IS NOT NULL
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT :candidate_limit
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "embedding": embedding_literal,
                    "candidate_limit": max(20, max_items * 4),
                },
            ).mappings().all()
            for row in vector_rows:
                chunk_id = str(row.get("chunk_id") or "").strip()
                if not chunk_id:
                    continue
                candidate_scores.setdefault(chunk_id, {})["semantic"] = max(
                    0.0,
                    float(row.get("semantic_score") or 0.0),
                )

    lexical_rows = session.execute(
        text(
            """
            SELECT chunk_id, ts_rank_cd(to_tsvector('simple', content), plainto_tsquery('simple', :query)) AS lexical_score
            FROM knowledge_chunks
            WHERE tenant_id = :tenant_id
              AND project_id = :project_id
              AND to_tsvector('simple', content) @@ plainto_tsquery('simple', :query)
            ORDER BY lexical_score DESC, updated_at DESC
            LIMIT :candidate_limit
            """
        ),
        {
            "tenant_id": tenant_id,
            "project_id": project_id,
            "query": normalized_query,
            "candidate_limit": max(20, max_items * 4),
        },
    ).mappings().all()
    for row in lexical_rows:
        chunk_id = str(row.get("chunk_id") or "").strip()
        if not chunk_id:
            continue
        candidate_scores.setdefault(chunk_id, {})["lexical"] = max(
            0.0,
            float(row.get("lexical_score") or 0.0),
        )

    if not candidate_scores:
        return KnowledgePromptContext(text="", citations=[])

    rows = session.execute(
        select(KnowledgeChunk, KnowledgeAsset)
        .join(KnowledgeAsset, KnowledgeAsset.asset_id == KnowledgeChunk.asset_id)
        .where(
            KnowledgeChunk.tenant_id == tenant_id,
            KnowledgeChunk.project_id == project_id,
            KnowledgeChunk.chunk_id.in_(list(candidate_scores.keys())),
            KnowledgeAsset.status == "ready",
        )
    ).all()
    if not rows:
        return KnowledgePromptContext(text="", citations=[])

    scored: list[tuple[float, KnowledgeChunk, KnowledgeAsset]] = []
    now = datetime.now(timezone.utc)
    for chunk, asset in rows:
        signals = candidate_scores.get(chunk.chunk_id, {})
        lexical = signals.get("lexical")
        if lexical is None:
            lexical = _lexical_score(query_tokens=query_tokens, text=chunk.content)
        semantic = signals.get("semantic", 0.0)
        source_time = chunk.source_timestamp or asset.source_timestamp
        recency_bonus = 0.0
        if source_time is not None:
            age_days = max(0.0, (now - source_time.astimezone(timezone.utc)).total_seconds() / 86400.0)
            recency_bonus = 1.0 / (1.0 + age_days / 30.0)
        score = (float(lexical) * 0.45) + (float(semantic) * 0.45) + (recency_bonus * 0.10)
        if score <= 0.0:
            continue
        scored.append((score, chunk, asset))
    return _format_knowledge_context(scored=scored, max_items=max_items, max_chars=max_chars)


def build_knowledge_prompt_context(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    query: str,
    max_items: int = 5,
    max_chars: int = 3200,
    embedding_access_mode: KnowledgeEmbeddingAccessMode = KnowledgeEmbeddingAccessMode.BEST_EFFORT,
) -> KnowledgePromptContext:
    if not tenant_id:
        return KnowledgePromptContext(text="", citations=[])
    # KB prompt injection must remain project-scoped; never fall back to tenant-wide retrieval.
    if not project_id:
        return KnowledgePromptContext(text="", citations=[])
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return KnowledgePromptContext(text="", citations=[])
    fact_lines, fact_citations = _match_facts(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        normalized_query=normalized_query,
        max_items=max(1, min(max_items, 3)),
    )
    bind = session.get_bind()
    chunk_context: KnowledgePromptContext
    if bind.dialect.name == "postgresql":
        try:
            chunk_context = _postgres_hybrid_context(
                session=session,
                tenant_id=tenant_id,
                project_id=project_id,
                normalized_query=normalized_query,
                max_items=max_items,
                max_chars=max_chars,
                embedding_access_mode=embedding_access_mode,
            )
        except Exception:  # noqa: BLE001
            chunk_context = _sqlite_fallback_context(
                session=session,
                tenant_id=tenant_id,
                project_id=project_id,
                normalized_query=normalized_query,
                max_items=max_items,
                max_chars=max_chars,
                embedding_access_mode=embedding_access_mode,
            )
    else:
        chunk_context = _sqlite_fallback_context(
            session=session,
            tenant_id=tenant_id,
            project_id=project_id,
            normalized_query=normalized_query,
            max_items=max_items,
            max_chars=max_chars,
            embedding_access_mode=embedding_access_mode,
        )

    sections = []
    if fact_lines:
        sections.append("Relevant facts:\n" + "\n".join(fact_lines))
    if chunk_context.text:
        sections.append("Supporting excerpts:\n" + chunk_context.text)
    return KnowledgePromptContext(
        text="\n\n".join(section for section in sections if section).strip(),
        citations=[*fact_citations, *chunk_context.citations],
    )
