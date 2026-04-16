from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from sqlalchemy.orm import Session

from orchestrator.core.pm_interview_service import (
    PMInterviewAssessment,
    PMInterviewEvidence,
    assess_pm_interview_brief,
    normalize_pm_interview_evidence,
    plan_pm_interview_with_codex,
    resolve_pm_interview_case,
    upsert_pm_interview_case,
)
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.storage.models import PMInterviewCase


@dataclass(frozen=True)
class PMInterviewFollowupResult:
    interview_case: PMInterviewCase
    assessment: PMInterviewAssessment
    message: str


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

    existing_brief = getattr(existing_case, "brief_json", None) or {}
    existing_evidence = list(getattr(existing_case, "evidence_json", None) or [])
    current_assessment = assess_pm_interview_brief(
        brief=existing_brief,
        evidence=existing_evidence,
        status_hint=str(getattr(existing_case, "status", "") or "").strip() or None,
    )
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
    pm_payload = plan_pm_interview_with_codex(
        runtime=runtime,
        request_text=reply_text,
        brief=existing_brief,
        evidence=[*existing_evidence, *[item.to_payload() for item in new_evidence]],
        missing_slots=current_assessment.missing_slots,
        next_question=current_assessment.next_question,
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
            "status": final_assessment.status,
        },
    ]
    interview_case = upsert_pm_interview_case(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        request_id=request_id,
        source_kind=str(getattr(existing_case, "source_kind", "") or "").strip() or "jira_parent",
        channel_id=str(getattr(existing_case, "channel_id", "") or "").strip(),
        thread_channel_id=str(getattr(existing_case, "thread_channel_id", "") or "").strip() or None,
        root_message_id=str(getattr(existing_case, "root_message_id", "") or "").strip() or None,
        owner_user_id=str(getattr(existing_case, "owner_user_id", "") or "").strip() or None,
        parent_issue_key=str(getattr(existing_case, "parent_issue_key", "") or "").strip() or None,
        source_text=reply_text,
        status=final_assessment.status,
        brief=final_assessment.brief.to_payload(),
        evidence=[item.to_payload() for item in new_evidence],
        question_history=question_history,
        current_question=final_assessment.next_question,
        next_question=final_assessment.next_question,
        notes=dict(getattr(existing_case, "notes_json", None) or {}),
    )
    return PMInterviewFollowupResult(
        interview_case=interview_case,
        assessment=final_assessment,
        message=message,
    )
