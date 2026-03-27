from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from orchestrator.storage.models import FollowupContext

ACTIVE_FOLLOWUP_CONTEXT_STATUS = "active"
CLOSED_FOLLOWUP_CONTEXT_STATUS = "closed"
CONSUMED_FOLLOWUP_CONTEXT_STATUS = "consumed"

FOLLOWUP_CONTEXT_DECISION_GATE = "decision_gate"
FOLLOWUP_CONTEXT_ASK_THREAD = "ask_thread"
FOLLOWUP_CONTEXT_SEED_FOLLOWUP = "seed_followup"
FOLLOWUP_CONTEXT_ROOM_PM = "room_pm"
FOLLOWUP_CONTEXT_HUMAN_INPUT = "human_input"
FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION = "engineering_clarification"


@dataclass(frozen=True)
class FollowupReaction:
    kind: str
    command_text: str | None = None
    command_params: dict[str, str] | None = None
    request_id: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def upsert_followup_context(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    context_type: str,
    channel_id: str | None = None,
    thread_channel_id: str | None = None,
    root_message_id: str | None = None,
    issue_key: str | None = None,
    request_id: str | None = None,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    status: str = ACTIVE_FOLLOWUP_CONTEXT_STATUS,
) -> FollowupContext:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_context_type = str(context_type or "").strip()
    if not normalized_tenant_id or not normalized_context_type:
        raise ValueError("tenant_id and context_type are required")
    normalized_thread_channel_id = str(thread_channel_id or "").strip() or None
    normalized_channel_id = str(channel_id or "").strip() or None
    normalized_root_message_id = str(root_message_id or "").strip() or None
    normalized_issue_key = str(issue_key or "").strip().upper() or None
    normalized_request_id = str(request_id or "").strip() or None
    normalized_run_id = str(run_id or "").strip() or None
    normalized_project_id = str(project_id or "").strip() or None
    normalized_status = str(status or ACTIVE_FOLLOWUP_CONTEXT_STATUS).strip().lower()
    now = _now()

    filters = [
        FollowupContext.tenant_id == normalized_tenant_id,
        FollowupContext.context_type == normalized_context_type,
        FollowupContext.status == ACTIVE_FOLLOWUP_CONTEXT_STATUS,
    ]
    identifier_filters = []
    if normalized_thread_channel_id:
        identifier_filters.append(FollowupContext.thread_channel_id == normalized_thread_channel_id)
    if normalized_request_id:
        identifier_filters.append(FollowupContext.request_id == normalized_request_id)
    if normalized_root_message_id:
        identifier_filters.append(FollowupContext.root_message_id == normalized_root_message_id)
    if normalized_channel_id:
        identifier_filters.append(
            and_(
                FollowupContext.channel_id == normalized_channel_id,
                FollowupContext.thread_channel_id.is_(None),
            )
        )
    existing = None
    if identifier_filters:
        existing = (
            session.execute(
                select(FollowupContext)
                .where(*filters, or_(*identifier_filters))
                .order_by(FollowupContext.updated_at.desc())
                .limit(1)
            )
            .scalars()
            .first()
        )
    if existing is None:
        existing = FollowupContext(
            context_id=uuid4().hex,
            tenant_id=normalized_tenant_id,
            project_id=normalized_project_id,
            context_type=normalized_context_type,
            status=normalized_status,
            channel_id=normalized_channel_id,
            thread_channel_id=normalized_thread_channel_id,
            root_message_id=normalized_root_message_id,
            issue_key=normalized_issue_key,
            request_id=normalized_request_id,
            run_id=normalized_run_id,
            metadata_json=dict(metadata or {}),
            created_at=now,
            updated_at=now,
            closed_at=None,
        )
        session.add(existing)
        return existing

    existing.project_id = normalized_project_id
    existing.status = normalized_status
    existing.channel_id = normalized_channel_id
    existing.thread_channel_id = normalized_thread_channel_id
    existing.root_message_id = normalized_root_message_id
    existing.issue_key = normalized_issue_key
    existing.request_id = normalized_request_id
    existing.run_id = normalized_run_id
    existing.metadata_json = dict(metadata or {})
    existing.updated_at = now
    existing.closed_at = None if normalized_status == ACTIVE_FOLLOWUP_CONTEXT_STATUS else now
    return existing


def resolve_followup_context(
    *,
    session: Session,
    tenant_id: str,
    channel_id: str,
    root_message_id: str | None = None,
) -> FollowupContext | None:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_channel_id = str(channel_id or "").strip()
    normalized_root_message_id = str(root_message_id or "").strip() or None
    if not normalized_tenant_id or not normalized_channel_id:
        return None

    for predicate in (
        FollowupContext.thread_channel_id == normalized_channel_id,
        FollowupContext.root_message_id == normalized_root_message_id if normalized_root_message_id else None,
        FollowupContext.channel_id == normalized_channel_id,
    ):
        if predicate is None:
            continue
        rows = (
            session.execute(
                select(FollowupContext)
                .where(
                    FollowupContext.tenant_id == normalized_tenant_id,
                    FollowupContext.status == ACTIVE_FOLLOWUP_CONTEXT_STATUS,
                    predicate,
                )
                .order_by(FollowupContext.updated_at.desc())
            )
            .scalars()
            .all()
        )
        if not rows:
            continue
        if len(rows) > 1:
            raise ValueError(
                f"Multiple active follow-up contexts match tenant={normalized_tenant_id} channel={normalized_channel_id}"
            )
        return rows[0]
    return None


def resolve_discord_command_subject_key(
    *,
    session: Session,
    tenant_id: str,
    channel_id: str | None,
    user_id: str,
) -> str:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_channel_id = str(channel_id or "").strip() or None
    normalized_user_id = str(user_id or "").strip()
    if normalized_channel_id:
        context = resolve_followup_context(
            session=session,
            tenant_id=normalized_tenant_id,
            channel_id=normalized_channel_id,
        )
        if context is not None:
            context_id = str(getattr(context, "context_id", "") or "").strip()
            if context_id:
                return f"discord_followup:{context_id}"
        return f"discord_channel:{normalized_tenant_id}:{normalized_channel_id}"
    return f"discord_user:{normalized_tenant_id}:{normalized_user_id}"


def resolve_discord_interaction_subject_scope(
    *,
    session: Session,
    payload: dict,
    find_tenant_for_discord_channel,
) -> tuple[str | None, str | None, str]:  # noqa: ANN001
    channel_id = str(payload.get("channel_id") or "").strip()
    user_id = str(((payload.get("member") or {}).get("user") or {}).get("id") or "").strip()
    if not user_id:
        user_id = str((payload.get("user") or {}).get("id") or "").strip()

    tenant_id: str | None = None
    project_id: str | None = None
    if channel_id:
        tenant = find_tenant_for_discord_channel(session=session, channel_id=channel_id)
        if tenant is not None:
            tenant_id = str(getattr(tenant, "tenant_id", "") or "").strip() or None

    root_message_id = None
    data = payload.get("data")
    if isinstance(data, dict) and data.get("type") == 3:
        root_message_id = str(data.get("target_id") or "").strip() or None
    if root_message_id is None:
        message = payload.get("message")
        if isinstance(message, dict):
            root_message_id = str(message.get("id") or "").strip() or None

    if tenant_id and channel_id:
        context = resolve_followup_context(
            session=session,
            tenant_id=tenant_id,
            channel_id=channel_id,
            root_message_id=root_message_id,
        )
        if context is not None:
            project_id = str(getattr(context, "project_id", "") or "").strip() or None
            context_id = str(getattr(context, "context_id", "") or "").strip()
            if context_id:
                return tenant_id, project_id, f"discord_followup:{context_id}"
    if tenant_id and channel_id:
        return tenant_id, project_id, f"discord_channel:{tenant_id}:{channel_id}"
    if tenant_id and user_id:
        return tenant_id, project_id, f"discord_user:{tenant_id}:{user_id}"
    if channel_id:
        return None, None, f"discord_channel::{channel_id}"
    if user_id:
        return None, None, f"discord_user::{user_id}"
    return None, None, "discord_interaction:unknown"


def close_followup_contexts(
    *,
    session: Session,
    tenant_id: str,
    context_type: str | None = None,
    issue_key: str | None = None,
    request_id: str | None = None,
    thread_channel_id: str | None = None,
    status: str = CLOSED_FOLLOWUP_CONTEXT_STATUS,
) -> int:
    normalized_tenant_id = str(tenant_id or "").strip()
    if not normalized_tenant_id:
        return 0
    query = select(FollowupContext).where(
        FollowupContext.tenant_id == normalized_tenant_id,
        FollowupContext.status == ACTIVE_FOLLOWUP_CONTEXT_STATUS,
    )
    normalized_context_type = str(context_type or "").strip()
    if normalized_context_type:
        query = query.where(FollowupContext.context_type == normalized_context_type)
    normalized_issue_key = str(issue_key or "").strip().upper()
    if normalized_issue_key:
        query = query.where(FollowupContext.issue_key == normalized_issue_key)
    normalized_request_id = str(request_id or "").strip()
    if normalized_request_id:
        query = query.where(FollowupContext.request_id == normalized_request_id)
    normalized_thread_channel_id = str(thread_channel_id or "").strip()
    if normalized_thread_channel_id:
        query = query.where(FollowupContext.thread_channel_id == normalized_thread_channel_id)
    rows = session.execute(query).scalars().all()
    if not rows:
        return 0
    now = _now()
    normalized_status = str(status or CLOSED_FOLLOWUP_CONTEXT_STATUS).strip().lower()
    for row in rows:
        row.status = normalized_status
        row.closed_at = now
        row.updated_at = now
    return len(rows)


def resolve_issue_followup_context(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
    context_type: str | None = None,
) -> FollowupContext | None:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_issue_key = str(issue_key or "").strip().upper()
    normalized_context_type = str(context_type or "").strip()
    if not normalized_tenant_id or not normalized_issue_key:
        return None
    query = (
        select(FollowupContext)
        .where(
            FollowupContext.tenant_id == normalized_tenant_id,
            FollowupContext.status == ACTIVE_FOLLOWUP_CONTEXT_STATUS,
            FollowupContext.issue_key == normalized_issue_key,
        )
        .order_by(FollowupContext.updated_at.desc())
    )
    if normalized_context_type:
        query = query.where(FollowupContext.context_type == normalized_context_type)
    rows = session.execute(query).scalars().all()
    if not rows:
        return None
    if len(rows) > 1 and normalized_context_type:
        raise ValueError(
            f"Multiple active follow-up contexts match tenant={normalized_tenant_id} issue={normalized_issue_key} type={normalized_context_type}"
        )
    return rows[0]


def resolve_followup_reaction(
    *,
    raw_text: str,
    source_ref: str | None,
    followup_context: FollowupContext | None,
    room_mode: bool,
    room_source: str | None = None,
) -> FollowupReaction | None:
    normalized_text = str(raw_text or "").strip()
    if not normalized_text or normalized_text.startswith("!"):
        return None
    if followup_context is not None:
        if followup_context.context_type == FOLLOWUP_CONTEXT_DECISION_GATE and followup_context.issue_key:
            return FollowupReaction(
                kind="command",
                command_text="!reply",
                command_params={
                    "issue_key": str(followup_context.issue_key),
                    "reply_text": normalized_text,
                    "source_ref": str(source_ref or "").strip(),
                },
            )
        if followup_context.context_type == FOLLOWUP_CONTEXT_SEED_FOLLOWUP:
            return FollowupReaction(
                kind="command",
                command_text=f"!issues followup {normalized_text}",
                command_params={
                    "request_id": str(followup_context.request_id or "").strip(),
                },
            )
        if followup_context.context_type == FOLLOWUP_CONTEXT_ASK_THREAD:
            return FollowupReaction(
                kind="command",
                command_text=f"!ask {normalized_text}",
            )
        if followup_context.context_type == FOLLOWUP_CONTEXT_HUMAN_INPUT:
            return FollowupReaction(
                kind="human_input",
                request_id=str(followup_context.request_id or "").strip() or None,
            )
    if room_mode:
        params: dict[str, str] = {"room_mode": "true"}
        normalized_source = str(room_source or "").strip().lower()
        if normalized_source:
            params["room_source"] = normalized_source
        return FollowupReaction(
            kind="command",
            command_text=f"!pm {normalized_text}",
            command_params=params,
        )
    return None
