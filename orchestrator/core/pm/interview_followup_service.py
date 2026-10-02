from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from sqlalchemy.orm import Session

from orchestrator.core.pm.interview_service import (
    PMInterviewAssessment,
    PMInterviewEvidence,
    PMInterviewQuestion,
    assess_pm_interview_brief,
    format_pm_interview_question,
    normalize_pm_interview_evidence,
    pm_interview_case_from_row,
    pm_interview_question_from_payload,
    plan_pm_interview_with_runtime,
    resolve_pm_interview_case,
    upsert_pm_interview_case,
)
from orchestrator.core.runtime.runtime import CodexRuntimeError
from orchestrator.core.runtime.invocation import AgentInvocationContext
from orchestrator.storage.models import PMInterviewCase


@dataclass(frozen=True)
class PMInterviewFollowupResult:
    interview_case: PMInterviewCase
    assessment: PMInterviewAssessment
    message: str
    next_question: PMInterviewQuestion | None = None
    clarification_questions: tuple[str, ...] = ()


def _existing_followup_assessment(
    *,
    existing_case: PMInterviewCase,
    source_transport: str,
    source_ref: str | None,
) -> PMInterviewAssessment | None:
    normalized_source_ref = str(source_ref or "").strip()
    if not normalized_source_ref:
        return None
    existing_evidence = normalize_pm_interview_evidence(
        list(getattr(existing_case, "evidence_json", None) or [])
    )
    for item in existing_evidence:
        if item.evidence_type != "human_reply":
            continue
        if str(item.source_ref or "").strip() != normalized_source_ref:
            continue
        metadata = dict(item.metadata or {})
        if (
            str(metadata.get("source_transport") or "").strip()
            != str(source_transport or "").strip()
        ):
            continue
        return pm_interview_case_from_row(existing_case)
    return None


def continue_pm_interview_from_followup(
    *,
    session: Session,
    runtime,
    tenant_id: str,
    project_id: str | None,
    request_id: str,
    reply_text: str,
    source_transport: str,
    source_ref: str | None,
    actor_ref: str | None,
    invocation_context: AgentInvocationContext,
    project_keys: list[str],
    issues: list[dict[str, Any]] | None = None,
    status_counts: dict[str, int] | None = None,
    history: Sequence[Mapping[str, Any]] | None = None,
    github_context: Mapping[str, Any] | None = None,
    settings: Any | None = None,
) -> PMInterviewFollowupResult:
    existing_case = resolve_pm_interview_case(
        session=session,
        tenant_id=tenant_id,
        request_id=request_id,
    )
    if existing_case is None:
        raise ValueError("PM interview case not found")

    duplicate_assessment = _existing_followup_assessment(
        existing_case=existing_case,
        source_transport=source_transport,
        source_ref=source_ref,
    )
    if duplicate_assessment is not None:
        duplicate_message = (
            format_pm_interview_question(duplicate_assessment.next_question)
            if duplicate_assessment.next_question is not None
            else ""
        )
        return PMInterviewFollowupResult(
            interview_case=existing_case,
            assessment=duplicate_assessment,
            message=duplicate_message,
            next_question=duplicate_assessment.next_question,
            clarification_questions=(duplicate_message,) if duplicate_message else (),
        )

    existing_brief = getattr(existing_case, "brief_json", None) or {}
    existing_evidence = list(getattr(existing_case, "evidence_json", None) or [])
    current_assessment = pm_interview_case_from_row(existing_case)
    new_evidence = normalize_pm_interview_evidence(
        [
            PMInterviewEvidence(
                evidence_id=str(source_ref or uuid4().hex),
                evidence_type="human_reply",
                source_ref=source_ref,
                summary=f"{source_transport} follow-up reply",
                content=reply_text,
                metadata={
                    "source_transport": source_transport,
                    "actor_ref": actor_ref,
                },
                captured_at=datetime.now(timezone.utc),
            )
        ]
    )
    pm_payload = plan_pm_interview_with_runtime(
        runtime=runtime,
        request_text=reply_text,
        brief=existing_brief,
        evidence=[*existing_evidence, *[item.to_payload() for item in new_evidence]],
        missing_slots=current_assessment.missing_slots,
        current_question=current_assessment.next_question,
        project_keys=list(project_keys),
        issues=list(issues or []),
        status_counts=dict(status_counts or {}),
        invocation_context=invocation_context,
        history=history,
        github_context=github_context,
        sqlalchemy_session=session,
        settings=settings,
    )
    brief = pm_payload.get("brief")
    if not isinstance(brief, Mapping):
        brief = {}
    message = str(pm_payload.get("message") or "").strip()
    final_assessment = assess_pm_interview_brief(
        brief=brief,
        evidence=[*existing_evidence, *[item.to_payload() for item in new_evidence]],
        status_hint=str(pm_payload.get("status") or "").strip() or None,
    )
    explicit_next_question = pm_interview_question_from_payload(
        pm_payload.get("next_question")
    )
    if final_assessment.ready_to_write:
        assessment_to_store = final_assessment
        stored_next_question = None
        clarification_questions: tuple[str, ...] = ()
    elif explicit_next_question is None:
        raise CodexRuntimeError(
            "PM interview follow-up did not return a next clarification question"
        )
    else:
        assessment_to_store = replace(
            final_assessment, next_question=explicit_next_question
        )
        stored_next_question = explicit_next_question
        clarification_questions = (
            format_pm_interview_question(explicit_next_question),
        )
    question_history = [
        {
            "speaker": "user",
            "text": reply_text,
            "request_id": request_id,
            "source_transport": source_transport,
            "source_ref": source_ref,
        },
        {
            "speaker": "pm",
            "text": message,
            "request_id": request_id,
            "status": assessment_to_store.status,
        },
    ]
    interview_case = upsert_pm_interview_case(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        request_id=request_id,
        source_kind=str(getattr(existing_case, "source_kind", "") or "").strip()
        or "jira_parent",
        channel_id=str(getattr(existing_case, "channel_id", "") or "").strip(),
        thread_channel_id=str(
            getattr(existing_case, "thread_channel_id", "") or ""
        ).strip()
        or None,
        root_message_id=str(getattr(existing_case, "root_message_id", "") or "").strip()
        or None,
        owner_user_id=str(getattr(existing_case, "owner_user_id", "") or "").strip()
        or None,
        parent_issue_key=str(
            getattr(existing_case, "parent_issue_key", "") or ""
        ).strip()
        or None,
        source_text=reply_text,
        status=assessment_to_store.status,
        brief=assessment_to_store.brief.to_payload(),
        evidence=[item.to_payload() for item in new_evidence],
        question_history=question_history,
        current_question=stored_next_question,
        next_question=stored_next_question,
        notes=dict(getattr(existing_case, "notes_json", None) or {}),
    )
    return PMInterviewFollowupResult(
        interview_case=interview_case,
        assessment=assessment_to_store,
        message=message,
        next_question=stored_next_question,
        clarification_questions=clarification_questions,
    )
