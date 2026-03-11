from __future__ import annotations

from base64 import b64decode
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import math
import mimetypes
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, delete, desc, func, select, text
from sqlalchemy.orm import Session

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


@dataclass(frozen=True)
class KnowledgePromptContext:
    text: str
    citations: list[dict[str, Any]]


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


def _embed_texts(texts: list[str]) -> list[list[float] | None]:
    if not texts:
        return []
    try:
        from fastembed import TextEmbedding  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return [None for _ in texts]
    try:
        model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        vectors = list(model.embed(texts))
    except Exception:  # noqa: BLE001
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

    for slot_name, slot_value in extract_slot_facts(extracted_text).items():
        session.add(
            KnowledgeFact(
                fact_id=uuid4().hex,
                asset_id=asset.asset_id,
                chunk_id=None,
                tenant_id=asset.tenant_id,
                project_id=asset.project_id,
                slot_name=slot_name,
                slot_value=slot_value,
                confidence=0.95,
                is_inferred=False,
                source_timestamp=source_timestamp,
                created_at=now,
                updated_at=now,
            )
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

    extracted_facts = extract_slot_facts(extracted_text)
    for slot_name, slot_value in extracted_facts.items():
        fact = KnowledgeFact(
            fact_id=uuid4().hex,
            asset_id=asset.asset_id,
            chunk_id=None,
            tenant_id=tenant_id,
            project_id=project_id,
            slot_name=slot_name,
            slot_value=slot_value,
            confidence=0.95,
            is_inferred=False,
            source_timestamp=source_timestamp,
            created_at=now,
            updated_at=now,
        )
        session.add(fact)

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
                KnowledgeFact.slot_name == slot_name,
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
                slot_value=str(fact.slot_value or "").strip(),
                source_timestamp=fact.source_timestamp or asset.source_timestamp,
                confidence=confidence,
                citation={
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
) -> KnowledgePromptContext:
    query_tokens = _candidate_tokens(normalized_query)
    query_embedding = _embed_texts([normalized_query])[0]
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
) -> KnowledgePromptContext:
    query_tokens = _candidate_tokens(normalized_query)
    query_embedding = _embed_texts([normalized_query])[0]
    candidate_scores: dict[str, dict[str, float]] = {}
    bind = session.get_bind()
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
) -> KnowledgePromptContext:
    if not tenant_id:
        return KnowledgePromptContext(text="", citations=[])
    # KB prompt injection must remain project-scoped; never fall back to tenant-wide retrieval.
    if not project_id:
        return KnowledgePromptContext(text="", citations=[])
    normalized_query = str(query or "").strip()
    if not normalized_query:
        return KnowledgePromptContext(text="", citations=[])
    bind = session.get_bind()
    if bind.dialect.name == "postgresql":
        try:
            return _postgres_hybrid_context(
                session=session,
                tenant_id=tenant_id,
                project_id=project_id,
                normalized_query=normalized_query,
                max_items=max_items,
                max_chars=max_chars,
            )
        except Exception:  # noqa: BLE001
            pass
    return _sqlite_fallback_context(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        normalized_query=normalized_query,
        max_items=max_items,
        max_chars=max_chars,
    )
