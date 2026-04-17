from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.followup_context_service import upsert_followup_context
from orchestrator.storage.models import FollowupContext


def clarification_question_text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("question", "stakeholder_question", "original_question"):
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
    return str(value or "").strip()


def clarification_question_reason(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    reason = str(value.get("why_it_matters") or value.get("reason") or "").strip()
    return reason or None


def normalize_clarification_questions(*, questions: list[object]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for raw_question in questions:
        question_text = clarification_question_text(raw_question)
        if not question_text:
            continue
        entry = {"question": question_text}
        reason = clarification_question_reason(raw_question)
        if reason:
            entry["why_it_matters"] = reason
        normalized.append(entry)
    return normalized


def clarification_state_fingerprint(*, questions: list[object]) -> str:
    normalized_questions = normalize_clarification_questions(questions=questions)
    payload = json.dumps(normalized_questions, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ClarificationProjectionSpec:
    tenant_id: str
    project_id: str | None
    context_type: str
    issue_key: str
    request_id: str | None
    questions: list[object]
    metadata: dict[str, Any] = field(default_factory=dict)
    channel_id: str | None = None
    thread_channel_id: str | None = None
    root_message_id: str | None = None
    owner_user_id: str | None = None
    origin_command: str | None = None
    transport: str | None = None
    reply_scope: str | None = None


@dataclass(frozen=True)
class ClarificationProjectionResult:
    followup_context: FollowupContext
    fingerprint: str
    already_projected: bool
    metadata: dict[str, Any]


def resolve_active_clarification_context(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
    context_type: str,
    transport: str | None = None,
    reply_scope: str | None = None,
) -> FollowupContext | None:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_issue_key = str(issue_key or "").strip().upper()
    normalized_context_type = str(context_type or "").strip()
    normalized_transport = str(transport or "").strip()
    normalized_reply_scope = str(reply_scope or "").strip()
    if not normalized_tenant_id or not normalized_issue_key or not normalized_context_type:
        return None
    rows = session.execute(
        select(FollowupContext)
        .where(
            FollowupContext.tenant_id == normalized_tenant_id,
            FollowupContext.issue_key == normalized_issue_key,
            FollowupContext.context_type == normalized_context_type,
            FollowupContext.status == "active",
        )
        .order_by(FollowupContext.updated_at.desc())
    ).scalars().all()
    for row in rows:
        metadata = dict(getattr(row, "metadata_json", {}) or {})
        if normalized_transport and str(metadata.get("transport") or "").strip() != normalized_transport:
            continue
        if normalized_reply_scope and str(metadata.get("reply_scope") or "").strip() != normalized_reply_scope:
            continue
        return row
    return None


def has_matching_active_clarification_state(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
    context_type: str,
    questions: list[object],
    transport: str | None = None,
    reply_scope: str | None = None,
) -> bool:
    followup_context = resolve_active_clarification_context(
        session=session,
        tenant_id=tenant_id,
        issue_key=issue_key,
        context_type=context_type,
        transport=transport,
        reply_scope=reply_scope,
    )
    if followup_context is None:
        return False
    metadata = dict(getattr(followup_context, "metadata_json", {}) or {})
    existing_fingerprint = str(metadata.get("question_state_fingerprint") or "").strip()
    if not existing_fingerprint:
        existing_questions = metadata.get("questions")
        if not isinstance(existing_questions, list):
            return False
        existing_fingerprint = clarification_state_fingerprint(questions=list(existing_questions))
    return existing_fingerprint == clarification_state_fingerprint(questions=questions)


def upsert_clarification_projection(
    *,
    session: Session,
    spec: ClarificationProjectionSpec,
) -> ClarificationProjectionResult:
    fingerprint = clarification_state_fingerprint(questions=spec.questions)
    existing = resolve_active_clarification_context(
        session=session,
        tenant_id=spec.tenant_id,
        issue_key=spec.issue_key,
        context_type=spec.context_type,
        transport=spec.transport,
        reply_scope=spec.reply_scope,
    )
    metadata = dict(getattr(existing, "metadata_json", {}) or {})
    metadata.update(dict(spec.metadata or {}))
    if spec.transport is not None:
        metadata["transport"] = spec.transport
    if spec.reply_scope is not None:
        metadata["reply_scope"] = spec.reply_scope
    metadata["question_state_fingerprint"] = fingerprint
    if "questions" not in metadata:
        metadata["questions"] = list(spec.questions)
    already_projected = False
    existing_fingerprint = str(metadata.get("question_state_fingerprint") or "").strip()
    if existing is not None:
        if not existing_fingerprint:
            existing_questions = metadata.get("questions")
            if isinstance(existing_questions, list):
                existing_fingerprint = clarification_state_fingerprint(questions=list(existing_questions))
        already_projected = existing_fingerprint == fingerprint
    followup_context = upsert_followup_context(
        session=session,
        tenant_id=spec.tenant_id,
        project_id=spec.project_id,
        context_type=spec.context_type,
        channel_id=spec.channel_id,
        thread_channel_id=spec.thread_channel_id,
        root_message_id=spec.root_message_id,
        owner_user_id=spec.owner_user_id,
        origin_command=spec.origin_command,
        issue_key=spec.issue_key,
        request_id=spec.request_id,
        metadata=metadata,
    )
    return ClarificationProjectionResult(
        followup_context=followup_context,
        fingerprint=fingerprint,
        already_projected=already_projected,
        metadata=metadata,
    )
