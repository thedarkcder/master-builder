from __future__ import annotations

from orchestrator.core.communications.enqueue_reason_contract import enqueue_reason_guidance
from orchestrator.core.decision_types import PrecheckOutcome


def normalize_backlog_pre_run_check_text(raw_value: str | None, *, max_chars: int = 240) -> str | None:
    if not isinstance(raw_value, str):
        return None
    normalized = " ".join(raw_value.strip().split())
    if not normalized:
        return None
    if len(normalized) > max_chars:
        if max_chars <= 3:
            return normalized[:max_chars]
        return f"{normalized[: max_chars - 3].rstrip()}..."
    return normalized


def format_jira_enqueue_skipped_message(
    *,
    issue_key: str,
    issue_status: str | None,
    reason: str,
    extra_detail: str | None = None,
) -> str:
    detail = f" ({extra_detail})" if extra_detail else ""
    guidance = enqueue_reason_guidance(reason)
    return (
        f"Jira webhook did not queue a run for `{issue_key}`.\n"
        f"Reason: `{reason}`{detail}\n"
        f"Guidance: {guidance}\n"
        f"Status: `{issue_status or 'unknown'}`"
    )


def format_backlog_pre_run_check_message(
    *,
    issue_key: str,
    board_id: int,
    issue_status: str | None,
    pre_run_check: dict[str, object],
) -> str:
    outcome = PrecheckOutcome.parse(pre_run_check.get("outcome"))
    ready_label = pre_run_check.get("ready_label")
    decision_gate_reason = normalize_backlog_pre_run_check_text(
        pre_run_check.get("decision_gate_reason")
        if isinstance(pre_run_check.get("decision_gate_reason"), str)
        else None
    )
    gtd_missing_criteria = [
        str(item).strip()
        for item in (pre_run_check.get("gtd_missing_criteria") or [])
        if str(item).strip()
    ]
    lines = [
        f"New issue `{issue_key}` was added to the backlog on board `{board_id}`.",
        "Run was not started (backlog-only event).",
    ]
    if issue_status:
        lines.append(f"Issue status: `{issue_status}`")
    if outcome is PrecheckOutcome.READY_FOR_AGENT:
        if isinstance(ready_label, str) and ready_label.strip():
            lines.append(f"Pre-run check: labeled `{ready_label.strip()}` and ready for agent.")
        else:
            lines.append("Pre-run check: ready for agent.")
    elif outcome is PrecheckOutcome.DECISION_GATE_REQUIRED:
        lines.append("Pre-run check: Decision Gate required before execution.")
        if decision_gate_reason:
            lines.append(f"Decision Gate reason: {decision_gate_reason}")
    elif outcome is PrecheckOutcome.GTD_REQUIRED:
        lines.append("Pre-run check: Good To Do details are incomplete.")
        if gtd_missing_criteria:
            lines.append("Missing GTD criteria: " + ", ".join(gtd_missing_criteria))
    elif outcome is PrecheckOutcome.MISSING_READY_LABEL:
        if isinstance(ready_label, str) and ready_label.strip():
            lines.append(f"Pre-run check: missing ready label `{ready_label.strip()}`.")
        else:
            lines.append("Pre-run check: missing ready label.")
    required_worker_label = str(pre_run_check.get("required_worker_label") or "").strip()
    if required_worker_label:
        lines.append(f"Required worker capability: `{required_worker_label}`.")
    return "\n".join(lines)
