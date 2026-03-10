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
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.storage.models import (
    DecisionAnswer,
    DecisionCase,
    DecisionCycle,
    DecisionEvidence,
    Project,
    Tenant,
)
from orchestrator.core.decision_effect_service import enqueue_decision_answer_kb_effects
from orchestrator.tools.project_repo_checkout import project_repo_dir

DECISION_CYCLE_COMMENT_MARKER = "<!-- decision-cycle:"


@dataclass(frozen=True)
class DecisionReplyCaptureResult:
    case: DecisionCase
    cycle: DecisionCycle
    accepted_question_ids: tuple[str, ...]
    answered_question_ids: tuple[str, ...]
    evidence_id: str
    effect_ids: tuple[str, ...]
    unresolved_question_feedback: tuple[dict[str, str], ...]


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


def recorded_cycle_answers(*, session: Session, cycle_id: str) -> list[DecisionAnswer]:
    return session.execute(
        select(DecisionAnswer)
        .where(
            DecisionAnswer.cycle_id == cycle_id,
            DecisionAnswer.status.in_(("answered", "accepted")),
        )
        .order_by(DecisionAnswer.created_at.asc())
    ).scalars().all()


def serialize_recorded_answers_for_policy(answers: list[DecisionAnswer]) -> list[dict[str, str]]:
    serialized: list[dict[str, str]] = []
    for answer in answers:
        status = str(answer.status or "").strip()
        value = str(answer.normalized_answer or "").strip()
        if status not in {"answered", "accepted"} or not value:
            continue
        serialized.append(
            {
                "question_id": str(answer.question_id or "").strip(),
                "question_kind": str(answer.question_kind or "").strip() or "decision_gate",
                "question_text": str(answer.question_text or "").strip(),
                "status": status,
                "answer": value,
            }
        )
    return serialized


def accepted_question_ids_for_cycle(*, session: Session, cycle_id: str) -> set[str]:
    return {
        str(answer.question_id).strip()
        for answer in accepted_cycle_answers(session=session, cycle_id=cycle_id)
        if str(answer.question_id).strip()
    }


def frozen_question_ids_for_cycle(*, cycle: DecisionCycle) -> list[str]:
    return [
        question_id
        for question_id in (
            str(item.get("id") or "").strip()
            for item in cycle.question_set_json
        )
        if question_id
    ]


def unresolved_question_ids_for_cycle(*, session: Session, cycle: DecisionCycle) -> tuple[str, ...]:
    accepted_ids = accepted_question_ids_for_cycle(session=session, cycle_id=cycle.cycle_id)
    return tuple(
        question_id
        for question_id in frozen_question_ids_for_cycle(cycle=cycle)
        if question_id not in accepted_ids
    )


def classification_for_cycle_questions(*, cycle: DecisionCycle, unresolved_question_ids: tuple[str, ...]) -> str:
    unresolved = set(unresolved_question_ids)
    kinds = {
        str(item.get("kind") or "").strip()
        for item in cycle.question_set_json
        if str(item.get("id") or "").strip() in unresolved
    }
    has_dg = "decision_gate" in kinds
    has_gtd = "gtd" in kinds
    if has_dg and has_gtd:
        return "both"
    if has_dg:
        return "decision_gate"
    if has_gtd:
        return "gtd"
    return "clear"


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
        notes_text = str(item.get("notes") or "").strip()
        if status in {"answered", "accepted"} and not answer_text and notes_text:
            answer_text = notes_text
        if status in {"answered", "accepted"} and not answer_text:
            continue
        normalized.append(
            {
                "question_id": question_id,
                "status": status,
                "answer": answer_text,
                "notes": notes_text or None,
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


def _feedback_for_cycle_questions(
    *,
    cycle: DecisionCycle,
    answers: list[DecisionAnswer],
) -> tuple[dict[str, str], ...]:
    answer_lookup = {
        str(answer.question_id or "").strip(): answer
        for answer in answers
        if str(answer.question_id or "").strip()
    }
    feedback: list[dict[str, str]] = []
    unresolved_ids = {
        str(question_id).strip()
        for question_id in cycle.unresolved_question_ids_json
        if str(question_id).strip()
    }
    for item in cycle.question_set_json:
        question_id = str(item.get("id") or "").strip()
        if not question_id or (unresolved_ids and question_id not in unresolved_ids):
            continue
        question_text = str(item.get("text") or "").strip()
        answer = answer_lookup.get(question_id)
        metadata = dict(answer.metadata_json or {}) if answer is not None else {}
        note = str(metadata.get("notes") or "").strip()
        status = str(answer.status or "").strip().lower() if answer is not None else "open"
        row: dict[str, str] = {
            "question_id": question_id,
            "question_text": question_text,
            "status": status or "open",
        }
        if note:
            row["note"] = note
        answer_text = str(answer.normalized_answer or "").strip() if answer is not None else ""
        if answer_text:
            row["answer"] = answer_text
        feedback.append(row)
    return tuple(feedback)


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
    accepted_answer_rows: list[DecisionAnswer] = []
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
        existing_ids = [str(value).strip() for value in answer_row.evidence_ids_json if str(value).strip()]
        if evidence.evidence_id not in existing_ids:
            answer_row.evidence_ids_json = [*existing_ids, evidence.evidence_id]
        existing_status = str(answer_row.status or "").strip().lower()
        if existing_status == "accepted":
            answer_row.updated_at = now
            accepted_question_ids.append(question_id)
            continue

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
        answered_question_ids.append(question_id)
        if status == "accepted":
            accepted_question_ids.append(question_id)
            accepted_answer_rows.append(answer_row)

    effect_ids = enqueue_decision_answer_kb_effects(
        session=session,
        case=case,
        cycle=cycle,
        answers=accepted_answer_rows,
        evidence_id=evidence.evidence_id,
        now=now,
    )
    session.flush()
    updated_answers = list_cycle_answers(session=session, cycle_id=cycle.cycle_id)
    return DecisionReplyCaptureResult(
        case=case,
        cycle=cycle,
        accepted_question_ids=tuple(sorted(set(accepted_question_ids))),
        answered_question_ids=tuple(sorted(set(answered_question_ids))),
        evidence_id=evidence.evidence_id,
        effect_ids=effect_ids,
        unresolved_question_feedback=_feedback_for_cycle_questions(
            cycle=cycle,
            answers=updated_answers,
        ),
    )
