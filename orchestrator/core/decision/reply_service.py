from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.decision.types import DecisionClassification, DecisionQuestionKind
from orchestrator.core.runtime.invocation import AgentInvocationContext, invoke_runtime_json
from orchestrator.core.runtime.payload_models import InteractionAction, InteractionResponse
from orchestrator.core.runtime.runtime import CodexRuntimeError, build_codex_runtime
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.decision.state_repository import existing_case_for_issue
from orchestrator.core.decision.question_state import unresolved_question_ids_for_question_set
from orchestrator.storage.models import (
    DecisionAnswer,
    DecisionCase,
    DecisionCycle,
    DecisionEvidence,
    Project,
    Tenant,
)
from orchestrator.core.decision.effect_service import (
    enqueue_cycle_comment_effect,
    enqueue_decision_answer_kb_effects,
)
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
    case = existing_case_for_issue(
        session=session,
        tenant_id=tenant_id,
        issue_key=issue_key,
    )
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


def latest_recorded_answers_for_issue(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
) -> list[DecisionAnswer]:
    rows = session.execute(
        select(DecisionAnswer)
        .where(
            DecisionAnswer.tenant_id == tenant_id,
            DecisionAnswer.issue_key == issue_key,
            DecisionAnswer.status.in_(("answered", "accepted")),
        )
        .order_by(DecisionAnswer.updated_at.desc(), DecisionAnswer.created_at.desc())
    ).scalars().all()
    latest_by_question_id: dict[str, DecisionAnswer] = {}
    for answer in rows:
        question_id = str(answer.question_id or "").strip()
        if not question_id or question_id in latest_by_question_id:
            continue
        latest_by_question_id[question_id] = answer
    return sorted(
        latest_by_question_id.values(),
        key=lambda answer: (
            answer.accepted_at or answer.answered_at or answer.created_at or datetime.min.replace(tzinfo=timezone.utc),
            str(answer.question_id or "").strip(),
        ),
    )


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
    return unresolved_question_ids_for_question_set(question_set=cycle.question_set_json)


def unresolved_question_ids_for_cycle(*, session: Session, cycle: DecisionCycle) -> tuple[str, ...]:
    accepted_ids = accepted_question_ids_for_cycle(session=session, cycle_id=cycle.cycle_id)
    return tuple(
        question_id
        for question_id in frozen_question_ids_for_cycle(cycle=cycle)
        if question_id not in accepted_ids
    )


def classification_for_cycle_questions(
    *,
    cycle: DecisionCycle,
    unresolved_question_ids: tuple[str, ...],
) -> DecisionClassification:
    unresolved = set(unresolved_question_ids)
    kinds = {
        str(item.get("kind") or "").strip()
        for item in cycle.question_set_json
        if str(item.get("id") or "").strip() in unresolved
    }
    has_dg = DecisionQuestionKind.DECISION_GATE.value in kinds
    has_gtd = DecisionQuestionKind.GTD.value in kinds
    if has_dg and has_gtd:
        return DecisionClassification.BOTH
    if has_dg:
        return DecisionClassification.DECISION_GATE
    if has_gtd:
        return DecisionClassification.GTD
    return DecisionClassification.CLEAR


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


def interpret_decision_reply(
    *,
    session: Session,
    settings: Any,
    tenant: Tenant,
    project: Project | None,
    issue_key: str,
    issue_summary: str | None = None,
    issue_description: str | None = None,
    cycle: DecisionCycle,
    reply_text: str,
    existing_answers: list[DecisionAnswer],
) -> InteractionResponse:
    questions = _normalize_question_payload(cycle)
    if not questions:
        raise CodexRuntimeError("Decision reply interpretation has no open questions")
    runtime = build_codex_runtime(session=session, settings=settings)
    working_dir = "."
    if project is not None:
        try:
            working_dir = project_repo_dir(project)
        except Exception:  # noqa: BLE001
            working_dir = "."
    payload = invoke_runtime_json(
        runtime=runtime,
        context=AgentInvocationContext(
            channel="system",
            tenant_id=tenant.tenant_id,
            project_id=project.project_id if project is not None else None,
            command="policy.reply",
            stage="decision_reply",
            working_dir=working_dir,
            issue_key=issue_key,
            reasoning_effort="low",
            db_session=session,
        ),
        system_prompt=render_prompt("policy/decision_reply_system.j2"),
        user_prompt=render_prompt(
            "policy/decision_reply_user.j2",
            issue_key=issue_key,
            issue_summary=issue_summary or "",
            issue_description=issue_description or "",
            questions_json=json.dumps(questions),
            existing_answers_json=json.dumps(
                [
                    {
                        "question_id": answer.question_id,
                        "question_text": answer.question_text,
                        "status": answer.status,
                        "answer": answer.normalized_answer,
                    }
                    for answer in existing_answers
                ]
            ),
            reply_text=reply_text,
        ),
    )
    try:
        interaction = InteractionResponse.from_payload(payload, context="Decision reply interaction")
        _validate_decision_reply_actions(interaction.actions)
        return interaction
    except RuntimeError as exc:
        raise CodexRuntimeError(f"Decision reply interpretation returned invalid payload: {exc}") from exc


def _validate_decision_reply_actions(actions: tuple[InteractionAction, ...]) -> None:
    allowed_action_types = {"capture_decision_answer", "recheck_gate"}
    for action in actions:
        if action.type not in allowed_action_types:
            raise RuntimeError(f"Decision reply returned unsupported action type {action.type!r}")
        if action.type == "recheck_gate":
            if action.payload:
                raise RuntimeError("Decision reply recheck_gate action payload must be empty")
            continue
        question_id = str(action.payload.get("question_id") or "").strip()
        status = str(action.payload.get("status") or "").strip().lower()
        answer = str(action.payload.get("answer") or "").strip()
        if not question_id:
            raise RuntimeError("Decision reply capture_decision_answer action missing question_id")
        if status not in {"answered", "accepted"}:
            raise RuntimeError("Decision reply capture_decision_answer action has invalid status")
        if not answer:
            raise RuntimeError("Decision reply capture_decision_answer action missing answer")


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


def _resolved_decision_answer_status(
    *,
    requested_status: str,
    question_id: str,
    question_kind: str,
    question_text: str,
    answer_text: str,
) -> str:
    _ = question_id, question_kind, question_text, answer_text
    normalized_status = str(requested_status or "").strip().lower()
    return normalized_status if normalized_status in {"answered", "accepted"} else "answered"


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
        if not question_id or question_id not in unresolved_ids:
            continue
        item_status = str(item.get("status") or "").strip().lower()
        if item_status == "accepted":
            continue
        question_text = str(item.get("text") or "").strip()
        answer = answer_lookup.get(question_id)
        metadata = dict(answer.metadata_json or {}) if answer is not None else {}
        note = str(metadata.get("notes") or "").strip()
        status = str(answer.status or "").strip().lower() if answer is not None else item_status or "open"
        if status == "accepted":
            continue
        row: dict[str, str] = {
            "question_id": question_id,
            "question_text": question_text,
            "status": status or "open",
        }
        if note:
            row["note"] = note
        detail = str(item.get("detail") or "").strip() if isinstance(item, dict) else ""
        if detail and not note:
            row["note"] = detail
        answer_text = str(answer.normalized_answer or "").strip() if answer is not None else ""
        if answer_text:
            row["answer"] = answer_text
        feedback.append(row)
    return tuple(feedback)


def unresolved_question_feedback_for_cycle(*, session: Session, cycle_id: str) -> tuple[dict[str, str], ...]:
    cycle = session.get(DecisionCycle, cycle_id)
    if cycle is None:
        return ()
    answers = list_cycle_answers(session=session, cycle_id=cycle.cycle_id)
    return _feedback_for_cycle_questions(cycle=cycle, answers=answers)


def sync_cycle_answers_from_planner(
    *,
    session: Session,
    tenant: Tenant,
    project: Project | None,
    case: DecisionCase,
    cycle: DecisionCycle,
    planner_question_states: list[dict[str, Any]],
    now: datetime,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    lookup = _question_lookup(cycle)
    existing_answers = {
        str(answer.question_id or "").strip(): answer
        for answer in list_cycle_answers(session=session, cycle_id=cycle.cycle_id)
        if str(answer.question_id or "").strip()
    }
    latest_evidence = session.execute(
        select(DecisionEvidence)
        .where(DecisionEvidence.cycle_id == cycle.cycle_id)
        .order_by(DecisionEvidence.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    accepted_question_ids: list[str] = []
    answered_question_ids: list[str] = []
    newly_accepted_rows: list[DecisionAnswer] = []

    for item in planner_question_states:
        question_id = str(item.get("question_id") or "").strip()
        if not question_id or question_id not in lookup:
            continue
        requested_status = str(item.get("status") or "").strip().lower()
        if requested_status not in {"open", "answered", "accepted"}:
            requested_status = "open"
        existing = existing_answers.get(question_id)
        if existing is None:
            existing = DecisionAnswer(
                answer_id=uuid4().hex,
                case_id=case.case_id,
                cycle_id=cycle.cycle_id,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id if project is not None else None,
                issue_key=case.issue_key,
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
            session.add(existing)
            existing_answers[question_id] = existing

        current_status = str(existing.status or "").strip().lower()
        detail = str(item.get("detail") or "").strip()
        answer_text = str(existing.normalized_answer or "").strip() or detail
        status = requested_status
        if status in {"answered", "accepted"} and answer_text:
            status = _resolved_decision_answer_status(
                requested_status=status,
                question_id=question_id,
                question_kind=lookup[question_id]["kind"],
                question_text=lookup[question_id]["text"],
                answer_text=answer_text,
            )
        if current_status == "accepted" and status != "accepted":
            status = "accepted"

        existing.metadata_json = {
            **dict(existing.metadata_json or {}),
            "notes": detail or None,
        }
        if latest_evidence is not None:
            evidence_ids = [str(value).strip() for value in existing.evidence_ids_json if str(value).strip()]
            if latest_evidence.evidence_id not in evidence_ids:
                existing.evidence_ids_json = [*evidence_ids, latest_evidence.evidence_id]
            existing.source_transport = latest_evidence.source_transport
            existing.source_ref = latest_evidence.source_ref

        if status in {"answered", "accepted"}:
            if answer_text:
                existing.normalized_answer = answer_text
            existing.answered_at = existing.answered_at or now
        if status == "accepted":
            if current_status != "accepted":
                newly_accepted_rows.append(existing)
            existing.accepted_at = existing.accepted_at or now
        existing.status = status
        existing.updated_at = now

        if status == "accepted":
            accepted_question_ids.append(question_id)
        elif status == "answered":
            answered_question_ids.append(question_id)

    effect_ids = enqueue_decision_answer_kb_effects(
        session=session,
        case=case,
        cycle=cycle,
        answers=newly_accepted_rows,
        evidence_id=latest_evidence.evidence_id if latest_evidence is not None else "",
        now=now,
    )
    return (
        tuple(sorted(set(accepted_question_ids))),
        tuple(sorted(set(answered_question_ids))),
        effect_ids,
    )


def capture_decision_reply(
    *,
    session: Session,
    settings: Any,
    tenant: Tenant,
    project: Project | None,
    issue_key: str,
    reply_text: str,
    source_transport: str,
    source_ref: str | None = None,
    actor_ref: str | None = None,
    metadata: dict[str, Any] | None = None,
    interpreted_reply: InteractionResponse | None = None,
) -> DecisionReplyCaptureResult:
    case, cycle = active_case_and_cycle_for_issue(
        session=session,
        tenant_id=tenant.tenant_id,
        issue_key=issue_key,
    )
    if case is None or cycle is None:
        raise ValueError(f"No active decision cycle exists for {issue_key}")
    now = datetime.now(timezone.utc)
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
            question_ids_json=[],
            normalized_answers_json=[],
            metadata_json=dict(metadata or {}),
            created_at=now,
        )
        session.add(evidence)
        session.flush()
    else:
        evidence = existing_evidence

    existing_answers = list_cycle_answers(session=session, cycle_id=cycle.cycle_id)
    interpretation = interpreted_reply or interpret_decision_reply(
        session=session,
        settings=settings,
        tenant=tenant,
        project=project,
        issue_key=issue_key,
        issue_summary=str(case.metadata_json.get("issue_summary") or "") if isinstance(case.metadata_json, dict) else "",
        issue_description=str(case.metadata_json.get("issue_description") or "") if isinstance(case.metadata_json, dict) else "",
        cycle=cycle,
        reply_text=reply_text,
        existing_answers=existing_answers,
    )
    _validate_decision_reply_actions(interpretation.actions)
    capture_actions = [action for action in interpretation.actions if action.type == "capture_decision_answer"]
    if not capture_actions:
        raise ValueError("Decision reply interaction has no answer evidence to capture")
    matches = [
        {
            "question_id": str(action.payload.get("question_id") or "").strip(),
            "status": str(action.payload.get("status") or "").strip(),
            "answer": str(action.payload.get("answer") or "").strip(),
            "notes": str(action.payload.get("notes") or "").strip() or None,
        }
        for action in capture_actions
    ]
    question_lookup = _question_lookup(cycle)
    answer_lookup = {
        str(answer.question_id or "").strip(): answer
        for answer in existing_answers
        if str(answer.question_id or "").strip()
    }
    accepted_question_ids: list[str] = []
    answered_question_ids: list[str] = []
    newly_accepted_rows: list[DecisionAnswer] = []
    normalized_answers_payload: list[dict[str, str]] = []

    for item in matches:
        question_id = str(item.get("question_id") or "").strip()
        if not question_id or question_id not in question_lookup:
            continue
        requested_status = str(item.get("status") or "").strip().lower()
        if requested_status == "ignored":
            continue
        if requested_status not in {"answered", "accepted"}:
            requested_status = "answered"
        answer_text = str(item.get("answer") or "").strip()
        if not answer_text:
            continue
        status = _resolved_decision_answer_status(
            requested_status=requested_status,
            question_id=question_id,
            question_kind=question_lookup[question_id]["kind"],
            question_text=question_lookup[question_id]["text"],
            answer_text=answer_text,
        )
        answer = answer_lookup.get(question_id)
        if answer is None:
            answer = DecisionAnswer(
                answer_id=uuid4().hex,
                case_id=case.case_id,
                cycle_id=cycle.cycle_id,
                tenant_id=tenant.tenant_id,
                project_id=project.project_id if project is not None else None,
                issue_key=issue_key,
                question_id=question_id,
                question_kind=question_lookup[question_id]["kind"],
                question_text=question_lookup[question_id]["text"],
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
            session.add(answer)
            answer_lookup[question_id] = answer

        current_status = str(answer.status or "").strip().lower()
        if current_status == "accepted" and status != "accepted":
            status = "accepted"

        notes = str(item.get("notes") or "").strip()
        answer.metadata_json = {
            **dict(answer.metadata_json or {}),
            "notes": notes or None,
        }
        evidence_ids = [str(value).strip() for value in answer.evidence_ids_json if str(value).strip()]
        if evidence.evidence_id not in evidence_ids:
            answer.evidence_ids_json = [*evidence_ids, evidence.evidence_id]
        answer.source_transport = source_transport
        answer.source_ref = source_ref
        answer.normalized_answer = answer_text
        answer.answered_at = answer.answered_at or now
        if status == "accepted":
            if current_status != "accepted":
                newly_accepted_rows.append(answer)
            answer.accepted_at = answer.accepted_at or now
            accepted_question_ids.append(question_id)
        else:
            answered_question_ids.append(question_id)
        answer.status = status
        answer.updated_at = now
        normalized_answers_payload.append(
            {
                "question_id": question_id,
                "status": status,
                "answer": answer_text,
            }
        )

    evidence.question_ids_json = sorted(
        {
            str(item.get("question_id") or "").strip()
            for item in normalized_answers_payload
            if str(item.get("question_id") or "").strip()
        }
    )
    evidence.normalized_answers_json = normalized_answers_payload

    comment_effect_id = enqueue_cycle_comment_effect(
        session=session,
        case=case,
        cycle=cycle,
        now=now,
    )
    kb_effect_ids = enqueue_decision_answer_kb_effects(
        session=session,
        case=case,
        cycle=cycle,
        answers=newly_accepted_rows,
        evidence_id=evidence.evidence_id,
        now=now,
    )
    # Flush captured answer state before the immediate recheck path runs so
    # planner sync updates the same rows instead of inserting duplicates.
    session.flush()
    updated_answers = list_cycle_answers(session=session, cycle_id=cycle.cycle_id)

    return DecisionReplyCaptureResult(
        case=case,
        cycle=cycle,
        accepted_question_ids=tuple(sorted(set(accepted_question_ids))),
        answered_question_ids=tuple(sorted(set(answered_question_ids))),
        evidence_id=evidence.evidence_id,
        effect_ids=tuple({comment_effect_id, *kb_effect_ids}),
        unresolved_question_feedback=_feedback_for_cycle_questions(cycle=cycle, answers=updated_answers),
    )
