from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.codex_invocation import CodexInvocationContext, invoke_codex_json
from orchestrator.core.codex_runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.knowledge_base import create_knowledge_asset
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.storage.models import (
    DecisionAnswer,
    DecisionCase,
    DecisionCycle,
    DecisionEvidence,
    KnowledgeAsset,
    Project,
    Tenant,
)
from orchestrator.tools.project_repo_checkout import project_repo_dir

DECISION_CYCLE_COMMENT_MARKER = "<!-- decision-cycle:"
DECISION_ANSWER_CONTEXT_START = "<!-- decision-answer-context:start -->"
DECISION_ANSWER_CONTEXT_END = "<!-- decision-answer-context:end -->"
DECISION_KNOWLEDGE_SOURCE_TYPE = "decision_answer"
DECISION_KNOWLEDGE_PENDING_STATUS = "pending_review"


@dataclass(frozen=True)
class DecisionReplyCaptureResult:
    case: DecisionCase
    cycle: DecisionCycle
    accepted_question_ids: tuple[str, ...]
    answered_question_ids: tuple[str, ...]
    evidence_id: str


def is_machine_generated_decision_comment(*, text: str | None) -> bool:
    normalized = str(text or "").strip()
    if not normalized:
        return False
    return normalized.startswith(DECISION_CYCLE_COMMENT_MARKER)


def active_case_and_cycle_for_issue(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
) -> tuple[DecisionCase | None, DecisionCycle | None]:
    case = session.execute(
        select(DecisionCase).where(
            DecisionCase.tenant_id == tenant_id,
            DecisionCase.issue_key == issue_key,
        )
    ).scalar_one_or_none()
    if case is None or not case.active_cycle_id:
        return case, None
    cycle = session.get(DecisionCycle, case.active_cycle_id)
    if cycle is None or cycle.status != "open":
        return case, None
    return case, cycle


def list_cycle_answers(*, session: Session, cycle_id: str) -> list[DecisionAnswer]:
    return session.execute(
        select(DecisionAnswer)
        .where(DecisionAnswer.cycle_id == cycle_id)
        .order_by(DecisionAnswer.created_at.asc())
    ).scalars().all()


def accepted_cycle_answers(*, session: Session, cycle_id: str) -> list[DecisionAnswer]:
    return session.execute(
        select(DecisionAnswer)
        .where(
            DecisionAnswer.cycle_id == cycle_id,
            DecisionAnswer.status == "accepted",
        )
        .order_by(DecisionAnswer.accepted_at.asc(), DecisionAnswer.created_at.asc())
    ).scalars().all()


def append_decision_answer_context(
    *,
    issue_description: str | None,
    answers: list[DecisionAnswer],
) -> str | None:
    accepted_answers = [
        answer
        for answer in answers
        if str(answer.status or "").strip() == "accepted"
        and str(answer.normalized_answer or "").strip()
    ]
    base = str(issue_description or "").strip()
    if not accepted_answers:
        return base or None
    lines = [
        DECISION_ANSWER_CONTEXT_START,
        "## Accepted Decision Answers",
    ]
    for answer in accepted_answers:
        lines.append(f"[{answer.question_id}] {answer.question_text}")
        lines.append(f"Answer: {str(answer.normalized_answer or '').strip()}")
    lines.append(DECISION_ANSWER_CONTEXT_END)
    block = "\n".join(lines)
    if not base:
        return block
    return f"{base}\n\n{block}".strip()


def accepted_question_ids_for_cycle(*, session: Session, cycle_id: str) -> set[str]:
    return {
        str(answer.question_id).strip()
        for answer in accepted_cycle_answers(session=session, cycle_id=cycle_id)
        if str(answer.question_id).strip()
    }


def _reply_dedupe_key(
    *,
    cycle_id: str,
    source_transport: str,
    source_ref: str | None,
    reply_text: str,
) -> str:
    if source_ref:
        return f"{source_transport}:{cycle_id}:{source_ref}"
    digest = hashlib.sha256(reply_text.encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"{source_transport}:{cycle_id}:text:{digest}"


def _normalize_question_payload(cycle: DecisionCycle) -> list[dict[str, str]]:
    unresolved_ids = {
        str(question_id).strip()
        for question_id in cycle.unresolved_question_ids_json
        if str(question_id).strip()
    }
    payload: list[dict[str, str]] = []
    for item in cycle.question_set_json:
        question_id = str(item.get("id") or "").strip()
        question_text = str(item.get("text") or "").strip()
        if not question_id or not question_text:
            continue
        if unresolved_ids and question_id not in unresolved_ids:
            continue
        payload.append(
            {
                "id": question_id,
                "kind": str(item.get("kind") or "").strip() or "decision_gate",
                "text": question_text,
            }
        )
    return payload


def _extract_reply_matches(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project | None,
    issue_key: str,
    cycle: DecisionCycle,
    reply_text: str,
    existing_answers: list[DecisionAnswer],
) -> list[dict[str, Any]]:
    questions = _normalize_question_payload(cycle)
    if not questions:
        return []
    runtime = build_codex_runtime(session=session, settings=settings)
    working_dir = "."
    if project is not None:
        try:
            working_dir = project_repo_dir(project)
        except Exception:  # noqa: BLE001
            working_dir = "."
    payload = invoke_codex_json(
        runtime=runtime,
        context=CodexInvocationContext(
            channel="system",
            tenant_id=tenant.tenant_id,
            project_id=project.project_id if project is not None else None,
            command="policy.reply",
            stage="decision_reply",
            working_dir=working_dir,
            issue_key=issue_key,
            reasoning_effort="low",
        ),
        system_prompt=render_prompt("policy/decision_reply_system.j2"),
        user_prompt=render_prompt(
            "policy/decision_reply_user.j2",
            issue_key=issue_key,
            questions_json=json.dumps(questions),
            existing_answers_json=json.dumps(
                [
                    {
                        "question_id": answer.question_id,
                        "status": answer.status,
                        "answer": answer.normalized_answer,
                    }
                    for answer in existing_answers
                ]
            ),
            reply_text=reply_text,
        ),
    )
    answers_raw = payload.get("answers")
    if not isinstance(answers_raw, list):
        raise CodexRuntimeError("Decision reply extraction did not return an answers array")
    normalized: list[dict[str, Any]] = []
    for item in answers_raw:
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("question_id") or "").strip()
        if not question_id:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in {"ignored", "answered", "accepted"}:
            status = "answered"
        answer_text = str(item.get("answer") or "").strip()
        if status in {"answered", "accepted"} and not answer_text:
            continue
        normalized.append(
            {
                "question_id": question_id,
                "status": status,
                "answer": answer_text,
                "notes": str(item.get("notes") or "").strip() or None,
            }
        )
    return normalized


def _question_lookup(cycle: DecisionCycle) -> dict[str, dict[str, str]]:
    lookup: dict[str, dict[str, str]] = {}
    for item in cycle.question_set_json:
        question_id = str(item.get("id") or "").strip()
        if not question_id:
            continue
        lookup[question_id] = {
            "text": str(item.get("text") or "").strip(),
            "kind": str(item.get("kind") or "").strip() or "decision_gate",
        }
    return lookup


def _stage_accepted_answer_as_knowledge(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    case_id: str,
    cycle_id: str,
    answer: DecisionAnswer,
    evidence_id: str,
    now: datetime,
) -> None:
    if not project_id or answer.status != "accepted":
        return
    source_ref = f"{issue_key}:{answer.question_id}"
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
            f"Question ID: {answer.question_id}\n"
            f"Question: {answer.question_text}\n"
            f"Accepted answer: {str(answer.normalized_answer or '').strip()}\n"
        )
        existing.status = DECISION_KNOWLEDGE_PENDING_STATUS
        existing.metadata_json = {
            **existing_metadata,
            "case_id": case_id,
            "cycle_id": cycle_id,
            "question_id": answer.question_id,
            "evidence_ids": sorted({*existing_evidence_ids, evidence_id}),
            "accepted_at": now.isoformat(),
        }
        existing.updated_at = now
        return
    create_knowledge_asset(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        source_type=DECISION_KNOWLEDGE_SOURCE_TYPE,
        title=f"{issue_key}: {answer.question_text[:160]}",
        mime_type="text/plain",
        source_ref=source_ref,
        source_timestamp=now,
        text_content=(
            f"Issue: {issue_key}\n"
            f"Question ID: {answer.question_id}\n"
            f"Question: {answer.question_text}\n"
            f"Accepted answer: {str(answer.normalized_answer or '').strip()}\n"
        ),
        metadata_json={
            "case_id": case_id,
            "cycle_id": cycle_id,
            "question_id": answer.question_id,
            "evidence_ids": [evidence_id],
            "accepted_at": now.isoformat(),
        },
        status=DECISION_KNOWLEDGE_PENDING_STATUS,
        commit=False,
    )


def capture_decision_reply(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant: Tenant,
    project: Project | None,
    issue_key: str,
    reply_text: str,
    source_transport: str,
    source_ref: str | None = None,
    actor_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> DecisionReplyCaptureResult:
    case, cycle = active_case_and_cycle_for_issue(
        session=session,
        tenant_id=tenant.tenant_id,
        issue_key=issue_key,
    )
    if case is None or cycle is None:
        raise ValueError(f"No active decision cycle exists for {issue_key}")
    now = datetime.now(timezone.utc)
    existing_answers = list_cycle_answers(session=session, cycle_id=cycle.cycle_id)
    extracted_answers = _extract_reply_matches(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        issue_key=issue_key,
        cycle=cycle,
        reply_text=reply_text,
        existing_answers=existing_answers,
    )
    lookup = _question_lookup(cycle)
    dedupe_key = _reply_dedupe_key(
        cycle_id=cycle.cycle_id,
        source_transport=source_transport,
        source_ref=source_ref,
        reply_text=reply_text,
    )
    existing_evidence = session.execute(
        select(DecisionEvidence).where(
            DecisionEvidence.cycle_id == cycle.cycle_id,
            DecisionEvidence.dedupe_key == dedupe_key,
        )
    ).scalar_one_or_none()
    if existing_evidence is None:
        evidence = DecisionEvidence(
            evidence_id=uuid4().hex,
            dedupe_key=dedupe_key,
            case_id=case.case_id,
            cycle_id=cycle.cycle_id,
            tenant_id=tenant.tenant_id,
            project_id=project.project_id if project is not None else None,
            issue_key=issue_key,
            source_transport=source_transport,
            source_ref=source_ref,
            actor_ref=actor_ref,
            raw_text=reply_text,
            question_ids_json=[
                item["question_id"]
                for item in extracted_answers
                if str(item.get("status") or "").strip().lower() in {"answered", "accepted"}
            ],
            normalized_answers_json=extracted_answers,
            metadata_json=dict(metadata or {}),
            created_at=now,
        )
        session.add(evidence)
        session.flush()
    else:
        evidence = existing_evidence

    accepted_question_ids: list[str] = []
    answered_question_ids: list[str] = []
    for item in extracted_answers:
        question_id = str(item.get("question_id") or "").strip()
        if not question_id or question_id not in lookup:
            continue
        status = str(item.get("status") or "").strip().lower()
        if status not in {"answered", "accepted"}:
            continue
        answer_row = session.execute(
            select(DecisionAnswer).where(
                DecisionAnswer.cycle_id == cycle.cycle_id,
                DecisionAnswer.question_id == question_id,
            )
        ).scalar_one_or_none()
        if answer_row is None:
            answer_row = DecisionAnswer(
                answer_id=uuid4().hex,
                case_id=case.case_id,
                cycle_id=cycle.cycle_id,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id if project is not None else None,
                issue_key=issue_key,
                question_id=question_id,
                question_kind=lookup[question_id]["kind"],
                question_text=lookup[question_id]["text"],
                status="open",
                normalized_answer=None,
                source_transport=None,
                source_ref=None,
                evidence_ids_json=[],
                metadata_json={},
                answered_at=None,
                accepted_at=None,
                created_at=now,
                updated_at=now,
            )
            session.add(answer_row)
        answer_row.status = "accepted" if status == "accepted" else "answered"
        answer_row.normalized_answer = str(item.get("answer") or "").strip()
        answer_row.source_transport = source_transport
        answer_row.source_ref = source_ref
        answer_row.metadata_json = {
            **dict(answer_row.metadata_json or {}),
            "notes": item.get("notes"),
        }
        answer_row.answered_at = answer_row.answered_at or now
        if status == "accepted":
            answer_row.accepted_at = now
        answer_row.updated_at = now
        existing_ids = [str(value).strip() for value in answer_row.evidence_ids_json if str(value).strip()]
        if evidence.evidence_id not in existing_ids:
            answer_row.evidence_ids_json = [*existing_ids, evidence.evidence_id]
        answered_question_ids.append(question_id)
        if status == "accepted":
            accepted_question_ids.append(question_id)
            _stage_accepted_answer_as_knowledge(
                session=session,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id if project is not None else None,
                issue_key=issue_key,
                case_id=case.case_id,
                cycle_id=cycle.cycle_id,
                answer=answer_row,
                evidence_id=evidence.evidence_id,
                now=now,
            )

    session.flush()
    return DecisionReplyCaptureResult(
        case=case,
        cycle=cycle,
        accepted_question_ids=tuple(sorted(set(accepted_question_ids))),
        answered_question_ids=tuple(sorted(set(answered_question_ids))),
        evidence_id=evidence.evidence_id,
    )
