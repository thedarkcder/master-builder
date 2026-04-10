from __future__ import annotations

from orchestrator.core.execution_admission import ExecutionAdmissionDecision


def format_discord_admission_conflict_detail(*, admission: ExecutionAdmissionDecision) -> str:
    guidance = str(admission.guidance or "").strip()
    if admission.reason_code == "missing_ready_label" and admission.ready_label:
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
    if admission.reason_code == "decision_gate_required":
        response_fields["decision_gate_reason"] = admission.detail
    if admission.reason_code == "missing_ready_label":
        response_fields["ready_label"] = admission.ready_label
    return response_fields


def build_jira_admission_notification_detail(*, admission: ExecutionAdmissionDecision) -> str | None:
    if admission.reason_code == "decision_gate_required" and admission.detail:
        return f"decision_gate_reason={admission.detail}"
    return admission.detail
