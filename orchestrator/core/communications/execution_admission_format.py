from __future__ import annotations

from orchestrator.core.execution_admission import ExecutionAdmissionDecision, ExecutionAdmissionReason


def format_discord_admission_conflict_detail(*, admission: ExecutionAdmissionDecision) -> str:
    guidance = str(admission.guidance or "").strip()
    if admission.reason is ExecutionAdmissionReason.MISSING_READY_LABEL and admission.ready_label:
        return f"{guidance} ({admission.ready_label})"
    return guidance


def build_jira_admission_response_fields(
    *,
    admission: ExecutionAdmissionDecision,
) -> dict[str, str | None]:
    if admission.can_enqueue:
        return {}
    response_fields: dict[str, str | None] = {
        "reason": admission.reason_code,
        "guidance": admission.guidance,
    }
    if admission.reason is ExecutionAdmissionReason.DECISION_GATE_REQUIRED:
        response_fields["decision_gate_reason"] = admission.detail
    if admission.reason is ExecutionAdmissionReason.MISSING_READY_LABEL:
        response_fields["ready_label"] = admission.ready_label
    return response_fields


def build_jira_admission_notification_detail(*, admission: ExecutionAdmissionDecision) -> str | None:
    if admission.reason is ExecutionAdmissionReason.DECISION_GATE_REQUIRED and admission.detail:
        return f"decision_gate_reason={admission.detail}"
    return admission.detail
