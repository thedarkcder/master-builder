from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Mapping, Sequence
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from orchestrator.core.codex_agents import _invoke_discord_json_maybe_tools
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.codex_runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime_stage_session import RuntimeStageSession
from orchestrator.storage.models import PMInterviewCase

PM_INTERVIEW_STATUS_DRAFTING = "drafting"
PM_INTERVIEW_STATUS_QUESTION_PENDING = "question_pending"
PM_INTERVIEW_STATUS_RESEARCHING = "researching"
PM_INTERVIEW_STATUS_READY_TO_WRITE = "ready_to_write"
PM_INTERVIEW_STATUS_PM_COMPLETED = "pm_completed"
PM_INTERVIEW_STATUS_ABANDONED = "abandoned"
PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT = "parent_brief_snapshot"
PM_INTERVIEW_PARENT_BRIEF_CHANNEL_ID = "jira-parent-sync"

PM_INTERVIEW_ACTIVE_STATUSES = (
    PM_INTERVIEW_STATUS_DRAFTING,
    PM_INTERVIEW_STATUS_QUESTION_PENDING,
    PM_INTERVIEW_STATUS_RESEARCHING,
    PM_INTERVIEW_STATUS_READY_TO_WRITE,
)


@dataclass(frozen=True)
class PMInterviewSlotDefinition:
    slot_key: str
    question: str
    examples: tuple[str, ...]
    required: bool = True


@dataclass(frozen=True)
class PMInterviewQuestion:
    slot_key: str
    question: str
    examples: tuple[str, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "slot_key": self.slot_key,
            "question": self.question,
            "examples": list(self.examples),
        }


@dataclass(frozen=True)
class PMInterviewBrief:
    objective: str = ""
    user_value: str = ""
    target_user: str = ""
    primary_journey: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    scope_in: tuple[str, ...] = ()
    scope_out: tuple[str, ...] = ()
    ui_references: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    success_outcomes: tuple[str, ...] = ()
    recommendation: str = ""
    open_questions: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "user_value": self.user_value,
            "target_user": self.target_user,
            "primary_journey": self.primary_journey,
            "acceptance_criteria": list(self.acceptance_criteria),
            "scope_in": list(self.scope_in),
            "scope_out": list(self.scope_out),
            "ui_references": list(self.ui_references),
            "constraints": list(self.constraints),
            "risks": list(self.risks),
            "success_outcomes": list(self.success_outcomes),
            "recommendation": self.recommendation,
            "open_questions": list(self.open_questions),
            "next_steps": list(self.next_steps),
        }


@dataclass(frozen=True)
class PMInterviewEvidence:
    evidence_id: str
    evidence_type: str
    source_ref: str | None = None
    title: str | None = None
    summary: str | None = None
    content: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    captured_at: datetime | None = None

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "evidence_id": self.evidence_id,
            "evidence_type": self.evidence_type,
            "source_ref": self.source_ref,
            "title": self.title,
            "summary": self.summary,
            "content": self.content,
            "metadata": dict(self.metadata),
            "captured_at": self.captured_at.isoformat() if self.captured_at is not None else None,
        }
        return {
            key: value
            for key, value in payload.items()
            if value is not None and value != "" and value != [] and value != {}
        }


@dataclass(frozen=True)
class PMInterviewAssessment:
    status: str
    brief: PMInterviewBrief
    evidence: tuple[PMInterviewEvidence, ...]
    missing_slots: tuple[str, ...]
    next_question: PMInterviewQuestion | None
    ready_to_write: bool

    def to_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "brief": self.brief.to_payload(),
            "evidence": [item.to_payload() for item in self.evidence],
            "missing_slots": list(self.missing_slots),
            "next_question": self.next_question.to_payload() if self.next_question is not None else None,
            "ready_to_write": self.ready_to_write,
        }


@dataclass(frozen=True)
class PMInterviewCaseResolution:
    status: str
    interview_case: PMInterviewCase | None = None
    matches: tuple[PMInterviewCase, ...] = ()


_PM_INTERVIEW_SLOT_DEFINITIONS: tuple[PMInterviewSlotDefinition, ...] = (
    PMInterviewSlotDefinition(
        slot_key="objective",
        question="What outcome should this feature deliver?",
        examples=(
            "Share the app with friends",
            "Let a user invite teammates into a workspace",
            "Help customers pause and resume a subscription",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="user_value",
        question="Why does the user want this feature?",
        examples=(
            "They can bring in friends more easily",
            "It reduces friction during onboarding",
            "It helps them avoid contacting support",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="target_user",
        question="Who is this feature for?",
        examples=(
            "New users",
            "Paid account owners",
            "Team admins",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="primary_journey",
        question="Where should the user start this flow?",
        examples=(
            "From the profile screen",
            "From onboarding",
            "From billing settings",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="acceptance_criteria",
        question="What should happen when the feature works?",
        examples=(
            "The user can tap share and send a link",
            "The recipient can open the right app store page",
            "The user can see when the subscription pause takes effect",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="scope_in",
        question="What must be included in the first version?",
        examples=(
            "Share link only",
            "Invite by email and copy link",
            "Pause and resume with no rewards or referral credits",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="scope_out",
        question="What should stay out of scope for now?",
        examples=(
            "Rewards or referral tracking",
            "Support tooling changes",
            "Admin-only workflows",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="ui_references",
        question="Do you have a design or screen reference we should follow?",
        examples=(
            "Profile page",
            "Onboarding screen",
            "No design reference yet",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="constraints",
        question="Are there any product constraints we need to respect?",
        examples=(
            "Must work on iOS and Android",
            "Needs to ship this quarter",
            "Must avoid enterprise-managed accounts",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="risks",
        question="Any risks, edge cases, or dependencies we should account for?",
        examples=(
            "Privacy or consent concerns",
            "App store or platform policy limits",
            "Abuse or spam risk",
        ),
    ),
    PMInterviewSlotDefinition(
        slot_key="success_outcomes",
        question="How will we know the feature succeeded?",
        examples=(
            "More invites are sent",
            "Fewer users contact support",
            "Higher activation or retention",
        ),
    ),
)

_PM_INTERVIEW_SLOT_BY_KEY = {definition.slot_key: definition for definition in _PM_INTERVIEW_SLOT_DEFINITIONS}
_PM_INTERVIEW_REQUIRED_SLOT_KEYS = tuple(
    definition.slot_key for definition in _PM_INTERVIEW_SLOT_DEFINITIONS if definition.required
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_text(value: object) -> str:
    return str(value or "").strip()


def _normalized_text_list(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        return ()
    return tuple(item.strip() for item in (str(item) for item in items) if item.strip())


def _normalize_mapping(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json_safe_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe_value(item) for item in value]
    if isinstance(value, set):
        return [_json_safe_value(item) for item in sorted(value, key=str)]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _merge_text_or_list(current: object, update: object) -> object:
    if isinstance(update, str):
        normalized = _normalized_text(update)
        return normalized if normalized else current
    if isinstance(update, (list, tuple, set)):
        normalized = _normalized_text_list(update)
        return normalized if normalized else current
    return current


def _merge_scalar_text(current: str, update: object) -> str:
    normalized = _normalized_text(update)
    if normalized:
        return normalized
    if isinstance(update, (list, tuple, set)):
        normalized_items = _normalized_text_list(update)
        if normalized_items:
            return normalized_items[0]
    return current


def normalize_pm_interview_brief(payload: Mapping[str, Any] | PMInterviewBrief | None) -> PMInterviewBrief:
    if isinstance(payload, PMInterviewBrief):
        return payload
    mapping = _normalize_mapping(payload)
    return PMInterviewBrief(
        objective=_normalized_text(mapping.get("objective")),
        user_value=_normalized_text(mapping.get("user_value")),
        target_user=_normalized_text(mapping.get("target_user")),
        primary_journey=_normalized_text(mapping.get("primary_journey")),
        acceptance_criteria=_normalized_text_list(mapping.get("acceptance_criteria")),
        scope_in=_normalized_text_list(mapping.get("scope_in")),
        scope_out=_normalized_text_list(mapping.get("scope_out")),
        ui_references=_normalized_text_list(mapping.get("ui_references")),
        constraints=_normalized_text_list(mapping.get("constraints")),
        risks=_normalized_text_list(mapping.get("risks")),
        success_outcomes=_normalized_text_list(mapping.get("success_outcomes")),
        recommendation=_normalized_text(mapping.get("recommendation")),
        open_questions=_normalized_text_list(mapping.get("open_questions")),
        next_steps=_normalized_text_list(mapping.get("next_steps")),
    )


def merge_pm_interview_brief(
    *,
    current: PMInterviewBrief,
    updates: Mapping[str, Any] | PMInterviewBrief | None,
) -> PMInterviewBrief:
    normalized_updates = normalize_pm_interview_brief(updates)
    current_payload = current.to_payload()
    update_payload = normalized_updates.to_payload()
    merged_payload: dict[str, object] = {}
    for key, current_value in current_payload.items():
        update_value = update_payload.get(key)
        if key in {"objective", "user_value", "target_user", "primary_journey", "recommendation"}:
            merged_payload[key] = _merge_scalar_text(str(current_value), update_value)
            continue
        if key in {
            "acceptance_criteria",
            "scope_in",
            "scope_out",
            "ui_references",
            "constraints",
            "risks",
            "success_outcomes",
            "open_questions",
            "next_steps",
        }:
            merged_payload[key] = _merge_text_or_list(current_value, update_value)
            continue
        merged_payload[key] = update_value if update_value is not None else current_value
    return normalize_pm_interview_brief(merged_payload)


def _evidence_update_payloads(evidence: Sequence[Mapping[str, Any] | PMInterviewEvidence] | None) -> tuple[PMInterviewEvidence, ...]:
    if evidence is None:
        return ()
    normalized: list[PMInterviewEvidence] = []
    for item in evidence:
        if isinstance(item, PMInterviewEvidence):
            normalized.append(item)
            continue
        mapping = _normalize_mapping(item)
        normalized.append(
            PMInterviewEvidence(
                evidence_id=_normalized_text(mapping.get("evidence_id")) or uuid4().hex,
                evidence_type=_normalized_text(mapping.get("evidence_type")) or "stakeholder_answer",
                source_ref=_normalized_text(mapping.get("source_ref")) or None,
                title=_normalized_text(mapping.get("title")) or None,
                summary=_normalized_text(mapping.get("summary")) or None,
                content=_normalized_text(mapping.get("content")) or None,
                metadata=_json_safe_value(_normalize_mapping(mapping.get("metadata"))),
                captured_at=_coerce_datetime(mapping.get("captured_at")),
            )
        )
    return tuple(normalized)


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    normalized = _normalized_text(value)
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _evidence_updates(evidence: Sequence[PMInterviewEvidence]) -> dict[str, object]:
    updates: dict[str, object] = {}
    for item in evidence:
        metadata = item.metadata if isinstance(item.metadata, Mapping) else {}
        for source_key in ("brief_updates", "slot_values"):
            slot_values = metadata.get(source_key)
            if not isinstance(slot_values, Mapping):
                continue
            for key, value in slot_values.items():
                if key in _PM_INTERVIEW_SLOT_BY_KEY:
                    updates[key] = value
    return updates


def apply_pm_interview_evidence(
    *,
    brief: PMInterviewBrief,
    evidence: Sequence[PMInterviewEvidence],
) -> PMInterviewBrief:
    merged = brief
    for key, value in _evidence_updates(evidence).items():
        merged = merge_pm_interview_brief(current=merged, updates={key: value})
    return merged


def pm_interview_missing_slots(*, brief: PMInterviewBrief) -> tuple[str, ...]:
    missing: list[str] = []
    payload = brief.to_payload()
    for slot_key in _PM_INTERVIEW_REQUIRED_SLOT_KEYS:
        value = payload.get(slot_key)
        if isinstance(value, str) and value.strip():
            continue
        if isinstance(value, list) and any(str(item).strip() for item in value):
            continue
        missing.append(slot_key)
    return tuple(missing)


def build_pm_interview_question(slot_key: str) -> PMInterviewQuestion | None:
    slot = _PM_INTERVIEW_SLOT_BY_KEY.get(str(slot_key or "").strip())
    if slot is None:
        return None
    return PMInterviewQuestion(slot_key=slot.slot_key, question=slot.question, examples=slot.examples)


def select_next_pm_interview_question(*, missing_slots: Sequence[str]) -> PMInterviewQuestion | None:
    for slot_key in missing_slots:
        question = build_pm_interview_question(slot_key)
        if question is not None:
            return question
    return None


def format_pm_interview_question(question: PMInterviewQuestion | None) -> str:
    if question is None:
        return "The product brief looks complete."
    example_lines = "\n".join(f"- {example}" for example in question.examples)
    return f"{question.question}\nExamples:\n{example_lines}"


def assess_pm_interview_brief(
    *,
    brief: Mapping[str, Any] | PMInterviewBrief | None,
    evidence: Sequence[Mapping[str, Any] | PMInterviewEvidence] | None = None,
    status_hint: str | None = None,
) -> PMInterviewAssessment:
    normalized_brief = normalize_pm_interview_brief(brief)
    normalized_evidence = _evidence_update_payloads(evidence)
    merged_brief = apply_pm_interview_evidence(brief=normalized_brief, evidence=normalized_evidence)
    missing_slots = pm_interview_missing_slots(brief=merged_brief)

    requested_status = _normalized_text(status_hint).lower()
    if requested_status in {PM_INTERVIEW_STATUS_PM_COMPLETED, PM_INTERVIEW_STATUS_ABANDONED}:
        return PMInterviewAssessment(
            status=requested_status,
            brief=merged_brief,
            evidence=normalized_evidence,
            missing_slots=missing_slots,
            next_question=None,
            ready_to_write=requested_status == PM_INTERVIEW_STATUS_PM_COMPLETED,
        )

    pending_research = any(
        bool((item.metadata or {}).get("pending"))
        for item in normalized_evidence
        if item.evidence_type in {"web_research", "research", "context"}
    )
    if not missing_slots:
        status = PM_INTERVIEW_STATUS_READY_TO_WRITE
    elif pending_research or requested_status == PM_INTERVIEW_STATUS_RESEARCHING:
        status = PM_INTERVIEW_STATUS_RESEARCHING
    elif merged_brief.to_payload() == PMInterviewBrief().to_payload():
        status = PM_INTERVIEW_STATUS_DRAFTING
    else:
        status = PM_INTERVIEW_STATUS_QUESTION_PENDING

    next_question = select_next_pm_interview_question(missing_slots=missing_slots)
    return PMInterviewAssessment(
        status=status,
        brief=merged_brief,
        evidence=normalized_evidence,
        missing_slots=missing_slots,
        next_question=next_question,
        ready_to_write=status == PM_INTERVIEW_STATUS_READY_TO_WRITE,
    )


def pm_interview_case_from_row(row: PMInterviewCase) -> PMInterviewAssessment:
    return assess_pm_interview_brief(
        brief=getattr(row, "brief_json", None) or {},
        evidence=getattr(row, "evidence_json", None) or [],
        status_hint=str(getattr(row, "status", "") or "").strip().lower() or None,
    )


def resolve_pm_interview_case_match(
    *,
    session: Session,
    tenant_id: str,
    request_id: str | None = None,
    channel_id: str | None = None,
    thread_channel_id: str | None = None,
    root_message_id: str | None = None,
    owner_user_id: str | None = None,
) -> PMInterviewCaseResolution:
    normalized_tenant_id = _normalized_text(tenant_id)
    normalized_request_id = _normalized_text(request_id) or None
    normalized_channel_id = _normalized_text(channel_id) or None
    normalized_thread_channel_id = _normalized_text(thread_channel_id) or None
    normalized_root_message_id = _normalized_text(root_message_id) or None
    normalized_owner_user_id = _normalized_text(owner_user_id) or None
    if not normalized_tenant_id:
        return PMInterviewCaseResolution(status="no_match")

    identifier_filters = []
    if normalized_request_id:
        identifier_filters.append(PMInterviewCase.request_id == normalized_request_id)
    if normalized_thread_channel_id:
        identifier_filters.append(PMInterviewCase.thread_channel_id == normalized_thread_channel_id)
    if normalized_root_message_id:
        identifier_filters.append(PMInterviewCase.root_message_id == normalized_root_message_id)
    if normalized_channel_id and not identifier_filters:
        return PMInterviewCaseResolution(status="no_match")
    if not identifier_filters:
        return PMInterviewCaseResolution(status="no_match")

    query = select(PMInterviewCase).where(
        PMInterviewCase.tenant_id == normalized_tenant_id,
        PMInterviewCase.status.in_(PM_INTERVIEW_ACTIVE_STATUSES),
        or_(*identifier_filters),
    )
    rows = session.execute(query.order_by(PMInterviewCase.updated_at.desc())).scalars().all()
    if not rows:
        return PMInterviewCaseResolution(status="no_match")

    scored_rows: list[tuple[int, datetime, PMInterviewCase]] = []
    for row in rows:
        if normalized_request_id and _normalized_text(getattr(row, "request_id", "")) != normalized_request_id:
            continue
        if normalized_thread_channel_id and _normalized_text(getattr(row, "thread_channel_id", "")) != normalized_thread_channel_id:
            continue
        if normalized_root_message_id and _normalized_text(getattr(row, "root_message_id", "")) != normalized_root_message_id:
            continue
        if normalized_owner_user_id:
            row_owner = _normalized_text(getattr(row, "owner_user_id", "")) or None
            if row_owner is not None and row_owner != normalized_owner_user_id:
                continue

        score = 0
        if normalized_request_id and _normalized_text(getattr(row, "request_id", "")) == normalized_request_id:
            score += 100
        if normalized_root_message_id and _normalized_text(getattr(row, "root_message_id", "")) == normalized_root_message_id:
            score += 80
        if normalized_thread_channel_id and _normalized_text(getattr(row, "thread_channel_id", "")) == normalized_thread_channel_id:
            score += 60
        if normalized_channel_id and _normalized_text(getattr(row, "channel_id", "")) == normalized_channel_id:
            score += 20
        if normalized_owner_user_id and _normalized_text(getattr(row, "owner_user_id", "")) == normalized_owner_user_id:
            score += 10
        scored_rows.append((score, getattr(row, "updated_at", _now()), row))

    if not scored_rows:
        return PMInterviewCaseResolution(status="no_match")

    scored_rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
    top_score = scored_rows[0][0]
    top_rows = tuple(row for score, _updated_at, row in scored_rows if score == top_score)
    if len(top_rows) > 1:
        return PMInterviewCaseResolution(status="ambiguous", matches=top_rows)
    return PMInterviewCaseResolution(status="matched", interview_case=top_rows[0], matches=top_rows)


def resolve_pm_interview_case(
    *,
    session: Session,
    tenant_id: str,
    request_id: str | None = None,
    channel_id: str | None = None,
    thread_channel_id: str | None = None,
    root_message_id: str | None = None,
    owner_user_id: str | None = None,
) -> PMInterviewCase | None:
    resolution = resolve_pm_interview_case_match(
        session=session,
        tenant_id=tenant_id,
        request_id=request_id,
        channel_id=channel_id,
        thread_channel_id=thread_channel_id,
        root_message_id=root_message_id,
        owner_user_id=owner_user_id,
    )
    return resolution.interview_case if resolution.status == "matched" else None


def _get_existing_pm_interview_case(
    *,
    session: Session,
    tenant_id: str,
    request_id: str,
) -> PMInterviewCase | None:
    return session.execute(
        select(PMInterviewCase).where(
            PMInterviewCase.tenant_id == _normalized_text(tenant_id),
            PMInterviewCase.request_id == _normalized_text(request_id),
        )
    ).scalars().first()


def upsert_pm_interview_case(
    *,
    session: Session,
    tenant_id: str,
    project_id: str | None,
    request_id: str,
    source_kind: str,
    channel_id: str,
    source_text: str,
    status: str | None = None,
    thread_channel_id: str | None = None,
    root_message_id: str | None = None,
    owner_user_id: str | None = None,
    parent_issue_key: str | None = None,
    brief: Mapping[str, Any] | PMInterviewBrief | None = None,
    evidence: Sequence[Mapping[str, Any] | PMInterviewEvidence] | None = None,
    question_history: Sequence[Mapping[str, Any]] | None = None,
    notes: Mapping[str, Any] | None = None,
    current_question: Mapping[str, Any] | PMInterviewQuestion | None = None,
    next_question: Mapping[str, Any] | PMInterviewQuestion | None = None,
) -> PMInterviewCase:
    normalized_tenant_id = _normalized_text(tenant_id)
    normalized_project_id = _normalized_text(project_id) or None
    normalized_request_id = _normalized_text(request_id)
    normalized_source_kind = _normalized_text(source_kind) or "drafting"
    normalized_channel_id = _normalized_text(channel_id)
    if not normalized_tenant_id or not normalized_request_id or not normalized_channel_id:
        raise ValueError("tenant_id, request_id, and channel_id are required")

    existing = _get_existing_pm_interview_case(
        session=session,
        tenant_id=normalized_tenant_id,
        request_id=normalized_request_id,
    )
    current_brief = normalize_pm_interview_brief(getattr(existing, "brief_json", None) if existing else None)
    if brief is not None:
        current_brief = merge_pm_interview_brief(current=current_brief, updates=brief)
    normalized_evidence = _evidence_update_payloads(evidence)
    all_evidence = tuple((normalize_pm_interview_evidence(getattr(existing, "evidence_json", None) or []) + normalized_evidence))
    assessment = assess_pm_interview_brief(
        brief=current_brief,
        evidence=all_evidence,
        status_hint=status,
    )
    normalized_status = _normalized_text(status).lower() or assessment.status
    if normalized_status not in {
        PM_INTERVIEW_STATUS_DRAFTING,
        PM_INTERVIEW_STATUS_QUESTION_PENDING,
        PM_INTERVIEW_STATUS_RESEARCHING,
        PM_INTERVIEW_STATUS_READY_TO_WRITE,
        PM_INTERVIEW_STATUS_PM_COMPLETED,
        PM_INTERVIEW_STATUS_ABANDONED,
    }:
        normalized_status = assessment.status
    now = _now()
    case = existing
    if case is None:
        case = PMInterviewCase(
            case_id=uuid4().hex,
            tenant_id=normalized_tenant_id,
            project_id=normalized_project_id,
            request_id=normalized_request_id,
            parent_issue_key=_normalized_text(parent_issue_key) or None,
            source_kind=normalized_source_kind,
            status=normalized_status,
            channel_id=normalized_channel_id,
            thread_channel_id=_normalized_text(thread_channel_id) or None,
            root_message_id=_normalized_text(root_message_id) or None,
            owner_user_id=_normalized_text(owner_user_id) or None,
            source_text=_normalized_text(source_text),
            brief_json=assessment.brief.to_payload(),
            evidence_json=[item.to_payload() for item in all_evidence],
            question_history_json=[
                _json_safe_value(dict(item))
                for item in (question_history or [])
                if isinstance(item, Mapping)
            ],
            current_question_json=_question_payload(current_question) or (assessment.next_question.to_payload() if assessment.next_question else {}),
            next_question_json=_question_payload(next_question) or (assessment.next_question.to_payload() if assessment.next_question else {}),
            missing_slots_json=list(assessment.missing_slots),
            notes_json=_json_safe_value(dict(notes or {})),
            created_at=now,
            updated_at=now,
            closed_at=now if normalized_status in {PM_INTERVIEW_STATUS_PM_COMPLETED, PM_INTERVIEW_STATUS_ABANDONED} else None,
        )
        session.add(case)
        session.flush()
        return case

    case.project_id = normalized_project_id
    case.parent_issue_key = _normalized_text(parent_issue_key) or case.parent_issue_key
    case.source_kind = normalized_source_kind
    case.status = normalized_status
    case.channel_id = normalized_channel_id
    case.thread_channel_id = _normalized_text(thread_channel_id) or case.thread_channel_id
    case.root_message_id = _normalized_text(root_message_id) or case.root_message_id
    case.owner_user_id = _normalized_text(owner_user_id) or case.owner_user_id
    case.source_text = _normalized_text(source_text) or case.source_text
    case.brief_json = assessment.brief.to_payload()
    case.evidence_json = [item.to_payload() for item in all_evidence]
    existing_history = list(case.question_history_json or [])
    for item in question_history or []:
        if isinstance(item, Mapping):
            existing_history.append(_json_safe_value(dict(item)))
    case.question_history_json = existing_history
    case.current_question_json = _question_payload(current_question) or (assessment.next_question.to_payload() if assessment.next_question else case.current_question_json)
    case.next_question_json = _question_payload(next_question) or (assessment.next_question.to_payload() if assessment.next_question else case.next_question_json)
    case.missing_slots_json = list(assessment.missing_slots)
    merged_notes = dict(case.notes_json or {})
    merged_notes.update(_json_safe_value(dict(notes or {})))
    case.notes_json = merged_notes
    case.updated_at = now
    if normalized_status in {PM_INTERVIEW_STATUS_PM_COMPLETED, PM_INTERVIEW_STATUS_ABANDONED}:
        case.closed_at = now
    elif normalized_status == PM_INTERVIEW_STATUS_READY_TO_WRITE:
        case.closed_at = None
    session.flush()
    return case


def append_pm_interview_evidence(
    *,
    session: Session,
    tenant_id: str,
    request_id: str,
    evidence: Sequence[Mapping[str, Any] | PMInterviewEvidence],
) -> PMInterviewCase:
    case = _get_existing_pm_interview_case(session=session, tenant_id=tenant_id, request_id=request_id)
    if case is None:
        raise ValueError("PM interview case not found")
    existing_evidence = normalize_pm_interview_evidence(case.evidence_json or [])
    normalized_new_evidence = _evidence_update_payloads(evidence)
    assessment = assess_pm_interview_brief(
        brief=case.brief_json or {},
        evidence=existing_evidence + normalized_new_evidence,
        status_hint=case.status,
    )
    case.brief_json = assessment.brief.to_payload()
    case.evidence_json = [item.to_payload() for item in existing_evidence + normalized_new_evidence]
    case.missing_slots_json = list(assessment.missing_slots)
    case.current_question_json = assessment.next_question.to_payload() if assessment.next_question else case.current_question_json
    case.next_question_json = assessment.next_question.to_payload() if assessment.next_question else case.next_question_json
    case.status = assessment.status
    case.updated_at = _now()
    session.flush()
    return case


def mark_pm_interview_case_completed(
    *,
    session: Session,
    tenant_id: str,
    request_id: str,
    parent_issue_key: str | None = None,
    notes: Mapping[str, Any] | None = None,
) -> PMInterviewCase:
    case = _get_existing_pm_interview_case(session=session, tenant_id=tenant_id, request_id=request_id)
    if case is None:
        raise ValueError("PM interview case not found")
    case.status = PM_INTERVIEW_STATUS_PM_COMPLETED
    case.parent_issue_key = _normalized_text(parent_issue_key) or case.parent_issue_key
    merged_notes = dict(case.notes_json or {})
    merged_notes.update(_json_safe_value(dict(notes or {})))
    case.notes_json = merged_notes
    case.closed_at = _now()
    case.updated_at = _now()
    session.flush()
    return case


def mark_pm_interview_case_abandoned(
    *,
    session: Session,
    tenant_id: str,
    request_id: str,
    notes: Mapping[str, Any] | None = None,
) -> PMInterviewCase:
    case = _get_existing_pm_interview_case(session=session, tenant_id=tenant_id, request_id=request_id)
    if case is None:
        raise ValueError("PM interview case not found")
    case.status = PM_INTERVIEW_STATUS_ABANDONED
    merged_notes = dict(case.notes_json or {})
    merged_notes.update(_json_safe_value(dict(notes or {})))
    case.notes_json = merged_notes
    case.closed_at = _now()
    case.updated_at = _now()
    session.flush()
    return case


def normalize_pm_interview_evidence(
    payload: Sequence[Mapping[str, Any] | PMInterviewEvidence] | None,
) -> tuple[PMInterviewEvidence, ...]:
    return _evidence_update_payloads(payload)


def normalize_parent_feature_brief_with_runtime(
    *,
    session: Session | None = None,
    settings: Any | None = None,
    runtime: CodexRuntime,
    parent_issue_key: str,
    parent_summary: str,
    parent_description: str,
    invocation_context: AgentInvocationContext,
) -> dict[str, Any]:
    stage_session = RuntimeStageSession.create(
        runtime=runtime,
        context=invocation_context,
        policy_stage="pm_parent_brief_normalization",
        session=session,
        settings=settings,
        issue_key=parent_issue_key,
    )
    system_prompt = render_prompt("workflow/pm_parent_brief_normalization_system.j2")
    user_prompt = render_prompt(
        "workflow/pm_parent_brief_normalization_user.j2",
        parent_issue_key=parent_issue_key,
        parent_summary=parent_summary,
        parent_description=parent_description,
        **stage_session.tooling.governed_native_prompt_context(),
    )
    payload = stage_session.invoke_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return a parent brief normalization JSON object")
    brief_payload = payload.get("brief")
    if not isinstance(brief_payload, Mapping):
        raise CodexRuntimeError("Codex did not return a normalized parent brief object")
    normalized_brief = normalize_pm_interview_brief(brief_payload)
    assessment = assess_pm_interview_brief(brief=normalized_brief.to_payload(), evidence=())
    questions = _normalized_text_list(payload.get("open_questions"))
    if not assessment.ready_to_write and not questions and assessment.next_question is not None:
        questions = (format_pm_interview_question(assessment.next_question),)
    return {
        "brief": assessment.brief.to_payload(),
        "open_questions": list(questions),
        "ready_to_write": assessment.ready_to_write and not questions,
    }


def _question_payload(value: Mapping[str, Any] | PMInterviewQuestion | None) -> dict[str, Any]:
    if isinstance(value, PMInterviewQuestion):
        return value.to_payload()
    if isinstance(value, Mapping):
        payload = dict(value)
        slot_key = _normalized_text(payload.get("slot_key"))
        question = _normalized_text(payload.get("question"))
        examples = _normalized_text_list(payload.get("examples"))
        if slot_key and question:
            return {"slot_key": slot_key, "question": question, "examples": list(examples)}
    return {}


def plan_pm_interview_with_codex(
    *,
    runtime: CodexRuntime,
    request_text: str,
    brief: Mapping[str, Any] | PMInterviewBrief | None,
    evidence: Sequence[Mapping[str, Any] | PMInterviewEvidence] | None,
    missing_slots: Sequence[str],
    next_question: PMInterviewQuestion | None,
    project_keys: list[str],
    issues: list[dict],
    status_counts: dict[str, int],
    invocation_context: AgentInvocationContext,
    history: Sequence[Mapping[str, Any]] | None = None,
    github_context: Mapping[str, Any] | None = None,
    sqlalchemy_session: Session | None = None,
    settings: Any | None = None,
) -> dict[str, Any]:
    normalized_brief = normalize_pm_interview_brief(brief)
    normalized_evidence = [item.to_payload() for item in normalize_pm_interview_evidence(evidence)]
    normalized_history = [dict(item) for item in history or [] if isinstance(item, Mapping)]
    next_question_payload = next_question.to_payload() if next_question is not None else None

    user_prompt = render_prompt(
        "discord/pm_interview_user.j2",
        request_text=request_text,
        brief_json=json.dumps(normalized_brief.to_payload()),
        evidence_json=json.dumps(normalized_evidence),
        missing_slots_json=json.dumps(list(missing_slots)),
        next_question_json=json.dumps(next_question_payload or {}),
        next_question_examples_json=json.dumps(list(next_question.examples) if next_question is not None else []),
        project_keys_json=json.dumps(project_keys),
        status_counts_json=json.dumps(status_counts),
        github_context_json=json.dumps(dict(github_context or {})),
        history_json=json.dumps(normalized_history),
        issues_json=json.dumps(issues[:40]),
    )
    payload = _invoke_discord_json_maybe_tools(
        runtime=runtime,
        context=invocation_context,
        system_prompt=render_prompt("discord/pm_interview_system.j2"),
        user_prompt=user_prompt,
        tool_stage="discord_pm_interview",
        sqlalchemy_session=sqlalchemy_session,
        settings=settings,
        max_tool_hops=10,
    )
    if not isinstance(payload, dict):
        raise CodexRuntimeError("Codex did not return a PM interview JSON object")
    message = _normalized_text(payload.get("message"))
    if not message:
        raise CodexRuntimeError("Codex did not return a PM interview message")
    brief_payload = payload.get("brief")
    if brief_payload is None:
        brief_payload = normalized_brief.to_payload()
    if not isinstance(brief_payload, Mapping):
        raise CodexRuntimeError("Codex did not return a PM interview brief object")

    normalized_payload = dict(payload)
    normalized_payload["message"] = message
    normalized_payload["brief"] = normalize_pm_interview_brief(brief_payload).to_payload()
    normalized_payload["status"] = _normalized_text(payload.get("status")).lower() or (
        PM_INTERVIEW_STATUS_READY_TO_WRITE if not missing_slots else PM_INTERVIEW_STATUS_QUESTION_PENDING
    )
    normalized_payload["missing_slots"] = list(missing_slots)
    normalized_payload["next_question"] = next_question_payload
    normalized_payload["next_question_examples"] = list(next_question.examples) if next_question is not None else []
    normalized_payload["ready_to_write"] = bool(payload.get("ready_to_write")) if "ready_to_write" in payload else not missing_slots
    normalized_payload["evidence"] = normalized_evidence
    return normalized_payload
