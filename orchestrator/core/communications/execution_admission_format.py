from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.decision_state_machine import (
    ExecutionAdmissionDecision,
    ExecutionAdmissionReason,
)


@dataclass(frozen=True)
class DiscordAdmissionConflictPresentation:
    detail: str


@dataclass(frozen=True)
class JiraAdmissionPresentation:
    response_fields: dict[str, str | None]
    notification_detail: str | None


def present_discord_admission_conflict(
    *,
    admission: ExecutionAdmissionDecision,
) -> DiscordAdmissionConflictPresentation:
    guidance = str(admission.guidance or "").strip()
    if admission.reason is ExecutionAdmissionReason.MISSING_READY_LABEL and admission.ready_label:
        return DiscordAdmissionConflictPresentation(detail=f"{guidance} ({admission.ready_label})")
    return DiscordAdmissionConflictPresentation(detail=guidance)


def present_jira_admission(
    *,
    admission: ExecutionAdmissionDecision,
) -> JiraAdmissionPresentation:
    if admission.can_enqueue:
        return JiraAdmissionPresentation(response_fields={}, notification_detail=None)
    response_fields: dict[str, str | None] = {
        "reason": admission.reason_code,
        "guidance": admission.guidance,
    }
    if admission.reason is ExecutionAdmissionReason.DECISION_GATE_REQUIRED:
        response_fields["decision_gate_reason"] = admission.detail
    if admission.reason is ExecutionAdmissionReason.MISSING_READY_LABEL:
        response_fields["ready_label"] = admission.ready_label
    notification_detail = (
        f"decision_gate_reason={admission.detail}"
        if admission.reason is ExecutionAdmissionReason.DECISION_GATE_REQUIRED and admission.detail
        else admission.detail
    )
    return JiraAdmissionPresentation(
        response_fields=response_fields,
        notification_detail=notification_detail,
    )


def format_discord_admission_conflict_detail(*, admission: ExecutionAdmissionDecision) -> str:
    return present_discord_admission_conflict(admission=admission).detail


def build_jira_admission_response_fields(
    *,
    admission: ExecutionAdmissionDecision,
) -> dict[str, str | None]:
    return present_jira_admission(admission=admission).response_fields


def build_jira_admission_notification_detail(*, admission: ExecutionAdmissionDecision) -> str | None:
    return present_jira_admission(admission=admission).notification_detail
