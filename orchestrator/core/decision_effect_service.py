from __future__ import annotations

from datetime import datetime
from typing import Callable
from uuid import uuid4

from sqlalchemy import select

from orchestrator.core.decision_presentation import build_cycle_comment
from orchestrator.core.knowledge_base import create_knowledge_asset
from orchestrator.storage.models import DecisionAnswer, DecisionCase, DecisionCycle, DecisionEffectOutbox, KnowledgeAsset

DECISION_KNOWLEDGE_SOURCE_TYPE = "decision_answer"
DECISION_KNOWLEDGE_PENDING_STATUS = "pending_review"


def enqueue_or_get_effect(
    *,
    session,
    case: DecisionCase,
    cycle: DecisionCycle,
    effect_type: str,
    dedupe_key: str,
    payload: dict[str, object],
    now: datetime,
) -> DecisionEffectOutbox:
    existing = session.execute(
        select(DecisionEffectOutbox).where(
            DecisionEffectOutbox.tenant_id == case.tenant_id,
            DecisionEffectOutbox.dedupe_key == dedupe_key,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_json != payload:
            existing.payload_json = payload
            existing.status = "pending"
            existing.sent_at = None
            existing.last_error = None
            existing.updated_at = now
        return existing
    effect = DecisionEffectOutbox(
        effect_id=uuid4().hex,
        dedupe_key=dedupe_key,
        case_id=case.case_id,
        cycle_id=cycle.cycle_id,
        tenant_id=case.tenant_id,
        project_id=case.project_id,
        issue_key=case.issue_key,
        effect_type=effect_type,
        payload_json=payload,
        status="pending",
        attempt_count=0,
        next_attempt_at=None,
        sent_at=None,
        last_error=None,
        created_at=now,
        updated_at=now,
    )
    session.add(effect)
    return effect


def enqueue_cycle_comment_effect(
    *,
    session,
    case: DecisionCase,
    cycle: DecisionCycle,
    now: datetime,
) -> str:
    effect = enqueue_or_get_effect(
        session=session,
        case=case,
        cycle=cycle,
        effect_type="jira_comment",
        dedupe_key=f"jira-comment:{case.tenant_id}:{case.issue_key}:{cycle.cycle_id}",
        payload={"comment": build_cycle_comment(session=session, case=case, cycle=cycle)},
        now=now,
    )
    return effect.effect_id


def enqueue_decision_answer_kb_effects(
    *,
    session,
    case: DecisionCase,
    cycle: DecisionCycle,
    answers: list[DecisionAnswer],
    evidence_id: str,
    now: datetime,
) -> tuple[str, ...]:
    effect_ids: list[str] = []
    if not case.project_id:
        return ()
    for answer in answers:
        if str(answer.status or "").strip() != "accepted":
            continue
        payload = {
            "tenant_id": case.tenant_id,
            "project_id": case.project_id,
            "issue_key": case.issue_key,
            "case_id": case.case_id,
            "cycle_id": cycle.cycle_id,
            "question_id": answer.question_id,
            "question_text": answer.question_text,
            "answer": str(answer.normalized_answer or "").strip(),
            "evidence_id": evidence_id,
            "accepted_at": now.isoformat(),
        }
        effect = enqueue_or_get_effect(
            session=session,
            case=case,
            cycle=cycle,
            effect_type="kb_stage_decision_answer",
            dedupe_key=f"kb-stage:{case.tenant_id}:{case.issue_key}:{cycle.cycle_id}:{answer.question_id}:{evidence_id}",
            payload=payload,
            now=now,
        )
        effect_ids.append(effect.effect_id)
    return tuple(effect_ids)


def _stage_decision_answer_knowledge_effect(*, session, effect: DecisionEffectOutbox, occurred_at: datetime) -> tuple[bool, str | None]:
    payload = dict(effect.payload_json or {}) if isinstance(effect.payload_json, dict) else {}
    tenant_id = str(payload.get("tenant_id") or "").strip()
    project_id = str(payload.get("project_id") or "").strip()
    issue_key = str(payload.get("issue_key") or "").strip()
    question_id = str(payload.get("question_id") or "").strip()
    question_text = str(payload.get("question_text") or "").strip()
    answer_text = str(payload.get("answer") or "").strip()
    evidence_id = str(payload.get("evidence_id") or "").strip()
    if not tenant_id or not project_id or not issue_key or not question_id or not question_text or not answer_text:
        return False, "KB effect payload is incomplete"
    source_ref = f"{issue_key}:{question_id}"
    existing = session.execute(
        select(KnowledgeAsset).where(
            KnowledgeAsset.tenant_id == tenant_id,
            KnowledgeAsset.project_id == project_id,
            KnowledgeAsset.source_type == DECISION_KNOWLEDGE_SOURCE_TYPE,
            KnowledgeAsset.source_ref == source_ref,
            KnowledgeAsset.status != "deleted",
        )
    ).scalar_one_or_none()
    if existing is not None:
        existing_metadata = dict(existing.metadata_json or {}) if isinstance(existing.metadata_json, dict) else {}
        existing_evidence_ids = [
            str(value).strip()
            for value in existing_metadata.get("evidence_ids", [])
            if str(value).strip()
        ]
        existing.text_content = (
            f"Issue: {issue_key}\n"
            f"Question ID: {question_id}\n"
            f"Question: {question_text}\n"
            f"Accepted answer: {answer_text}\n"
        )
        existing.status = DECISION_KNOWLEDGE_PENDING_STATUS
        existing.metadata_json = {
            **existing_metadata,
            "case_id": str(payload.get("case_id") or "").strip() or None,
            "cycle_id": str(payload.get("cycle_id") or "").strip() or None,
            "question_id": question_id,
            "evidence_ids": sorted({*existing_evidence_ids, evidence_id}),
            "accepted_at": str(payload.get("accepted_at") or occurred_at.isoformat()),
        }
        existing.updated_at = occurred_at
        return True, None
    create_knowledge_asset(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        source_type=DECISION_KNOWLEDGE_SOURCE_TYPE,
        title=f"{issue_key}: {question_text[:160]}",
        mime_type="text/plain",
        source_ref=source_ref,
        source_timestamp=occurred_at,
        text_content=(
            f"Issue: {issue_key}\n"
            f"Question ID: {question_id}\n"
            f"Question: {question_text}\n"
            f"Accepted answer: {answer_text}\n"
        ),
        metadata_json={
            "case_id": str(payload.get("case_id") or "").strip() or None,
            "cycle_id": str(payload.get("cycle_id") or "").strip() or None,
            "question_id": question_id,
            "evidence_ids": [evidence_id] if evidence_id else [],
            "accepted_at": str(payload.get("accepted_at") or occurred_at.isoformat()),
        },
        status=DECISION_KNOWLEDGE_PENDING_STATUS,
        commit=False,
    )
    return True, None


def publish_decision_effects(
    *,
    session,
    effect_ids: tuple[str, ...],
    occurred_at: datetime,
    publish_jira_comment_fn: Callable[[str], tuple[bool, str | None]] | None = None,
) -> None:
    for effect_id in effect_ids:
        effect = session.get(DecisionEffectOutbox, effect_id)
        if effect is None or effect.status != "pending":
            continue
        if effect.effect_type == "jira_comment":
            if publish_jira_comment_fn is None:
                continue
            posted, error = publish_jira_comment_fn(str(effect.payload_json.get("comment") or ""))
        elif effect.effect_type == "kb_stage_decision_answer":
            posted, error = _stage_decision_answer_knowledge_effect(
                session=session,
                effect=effect,
                occurred_at=occurred_at,
            )
        else:
            continue
        if posted:
            effect.status = "sent"
            effect.sent_at = occurred_at
            effect.updated_at = occurred_at
        else:
            effect.status = "failed"
            effect.attempt_count += 1
            effect.last_error = error
            effect.updated_at = occurred_at
        session.commit()
