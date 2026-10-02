from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from orchestrator.core.clarification.questions import ClarificationQuestion
from orchestrator.core.workflow.runtime import WorkflowAdvanceOutcome
from orchestrator.tools.atlassian_oauth import JiraIssueDetail

_SYSTEM_COMMENT_MARKER = "[mb-system]"
_SYNC_LABELS = {"sync-current", "sync-stale", "sync-blocked"}
_PARENT_BOARD_ENTRY_STATUSES = {"to do", "ready for agent"}
_PARENT_SYNC_MATERIAL_FIELDS = {
    "summary",
    "description",
    "acceptance criteria",
    "acceptance_criteria",
    "scope",
    "scope in",
    "scope out",
    "ui / design / references",
    "ui references",
    "risk",
    "risks",
    "dependency",
    "dependencies",
    "open questions",
    "open question",
}
_ISSUE_KEY_PATTERN = re.compile(r"([A-Z][A-Z0-9_]+-\d+)")
_PM_INTERVIEW_JIRA_TRANSPORT = "jira_issue_comment"
_PM_INTERVIEW_JIRA_REPLY_SCOPE = "issue_comment_stream_from_root"


def pm_interview_jira_transport() -> str:
    return _PM_INTERVIEW_JIRA_TRANSPORT


def pm_interview_jira_reply_scope() -> str:
    return _PM_INTERVIEW_JIRA_REPLY_SCOPE


def system_comment_marker() -> str:
    return _SYSTEM_COMMENT_MARKER


@dataclass(frozen=True)
class JiraParentChildSyncContext:
    request_id: str
    tenant_id: str
    tenant: Any
    project_id: str | None
    issue_key: str
    issue_labels: list[str]
    payload: dict[str, Any]
    webhook_event: str | None
    comment_command: str | None
    comment_command_argument: str | None
    workflow_id: str | None = None
    operation_id: str | None = None
    attempt: int | None = None
    attempt_id: str | None = None


@dataclass(frozen=True)
class JiraParentChildSyncResult:
    handled: bool
    reason: str | None = None
    extra: dict[str, object] = field(default_factory=dict)
    failed: bool = False


def jira_sync_result_from_advance_result(
    *,
    session,
    workflow_type,
    tenant_id: str,
    project_id: str | None,
    issue_key: str,
    result: WorkflowAdvanceOutcome,
) -> JiraParentChildSyncResult:
    _ = session, workflow_type, tenant_id, project_id, issue_key
    return JiraParentChildSyncResult(
        handled=result.handled,
        reason=result.reason,
        extra=dict(result.extra or {}),
        failed=result.failed,
    )


def pm_interview_jira_followup_request_id(*, parent_issue_key: str) -> str:
    return f"pm-interview-jira:{str(parent_issue_key or '').strip().upper()}"


def normalize_comment_id(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return None


def extract_created_comment_id(comment: dict[str, Any] | None) -> str | None:
    if not isinstance(comment, dict):
        return None
    return normalize_comment_id(comment.get("id"))


def is_system_generated_comment(*, text: str | None) -> bool:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return False
    return normalized_text.startswith(_SYSTEM_COMMENT_MARKER)


def project_key_for_issue(issue_key: str) -> str:
    normalized = str(issue_key or "").strip().upper()
    if "-" not in normalized:
        return normalized
    return normalized.split("-", 1)[0]


def normalize_sync_labels(labels: list[str], *, target_label: str) -> list[str]:
    seen: set[str] = set()
    normalized: list[str] = []
    for raw_label in labels:
        label = str(raw_label or "").strip()
        if not label or label.casefold() in _SYNC_LABELS:
            continue
        lowered = label.casefold()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(label)
    if target_label.casefold() not in seen:
        normalized.append(target_label)
    return normalized


def extract_parent_issue_key(*, child_detail: JiraIssueDetail) -> str | None:
    for line in str(child_detail.description or "").splitlines():
        match = _ISSUE_KEY_PATTERN.search(line.strip().upper())
        if match:
            candidate = match.group(1).strip().upper()
            if candidate != child_detail.key.upper():
                return candidate
    for label in child_detail.labels:
        normalized = str(label or "").strip().lower()
        if not normalized.startswith("parent-"):
            continue
        raw_key = normalized[len("parent-") :].strip("-")
        if not raw_key:
            continue
        parts = [part for part in raw_key.split("-") if part]
        if len(parts) < 2:
            continue
        return f"{'-'.join(parts[:-1]).upper()}-{parts[-1]}"
    return None


def material_parent_changed_fields(
    *, payload: dict[str, Any], extract_changed_fields_fn
) -> list[str]:  # noqa: ANN001
    changed_fields = extract_changed_fields_fn(payload)
    material: list[str] = []
    seen: set[str] = set()
    for changed_field in changed_fields:
        normalized = str(changed_field or "").strip().casefold()
        if normalized in {"status", "labels"}:
            continue
        if normalized in _PARENT_SYNC_MATERIAL_FIELDS or normalized in {
            "summary",
            "description",
        }:
            if normalized not in seen:
                seen.add(normalized)
                material.append(normalized)
    return material


def normalize_status_name(value: str | None) -> str:
    return str(value or "").strip().casefold()


def parent_board_entry_target_status(
    *, payload: dict[str, Any], extract_status_transition_fn
) -> str | None:  # noqa: ANN001
    from_status, to_status = extract_status_transition_fn(payload)
    normalized_to_status = normalize_status_name(to_status)
    if normalized_to_status not in _PARENT_BOARD_ENTRY_STATUSES:
        return None
    if normalize_status_name(from_status) == normalized_to_status:
        return None
    target_status = str(to_status or "").strip()
    return target_status or None


def build_child_snapshot_markdown(child_detail: JiraIssueDetail) -> str:
    return (
        f"### {child_detail.key}: {child_detail.summary}\n"
        f"Labels: {', '.join(child_detail.labels) if child_detail.labels else 'none'}\n"
        f"Description:\n{child_detail.description.strip() or 'No description provided.'}"
    )


def build_parent_resync_prompt(
    *,
    parent_detail: JiraIssueDetail,
    child_details: list[JiraIssueDetail],
    changed_fields: list[str],
) -> str:
    child_block = "\n\n".join(
        build_child_snapshot_markdown(detail) for detail in child_details
    )
    changed_lines = (
        "\n".join(f"- {field}" for field in changed_fields) or "- description"
    )
    return (
        "Refresh the existing Jira parent/child hierarchy from the latest PM parent issue edit.\n\n"
        f"## Parent feature\n"
        f"- Key: {parent_detail.key}\n"
        f"- Summary: {parent_detail.summary}\n"
        f"- Changed fields in Jira webhook:\n{changed_lines}\n\n"
        f"Description:\n{parent_detail.description.strip() or 'No description provided.'}\n\n"
        "## Existing engineering children\n"
        f"{child_block}\n\n"
        "Instructions:\n"
        "- Reconstruct the PM-owned parent issue from the live parent description.\n"
        "- Return only the engineering child tickets materially impacted by the parent change.\n"
        "- If no engineering child tickets need changes, return engineering_children as an empty array.\n"
        "- Preserve existing issue keys when known from the source context.\n"
        "- Do not create duplicates.\n"
        "- Keep the parent non-technical and the children technical.\n"
    )


def build_clarification_followup_prompt(
    *,
    parent_detail: JiraIssueDetail,
    child_details: list[JiraIssueDetail],
    metadata: dict[str, Any],
    reply_text: str,
) -> str:
    pending_questions = metadata.get("questions")
    pending_lines: list[str] = []
    if isinstance(pending_questions, list):
        for item in pending_questions:
            if not isinstance(item, dict):
                continue
            stakeholder_question = str(item.get("stakeholder_question") or "").strip()
            source_child_key = str(item.get("source_child_key") or "").strip().upper()
            original_question = str(item.get("original_question") or "").strip()
            if stakeholder_question:
                pending_lines.append(
                    f"- {source_child_key or 'child'} asked: {original_question}\n"
                    f"  Product clarification needed: {stakeholder_question}"
                )
    child_block = "\n\n".join(
        build_child_snapshot_markdown(detail) for detail in child_details
    )
    questions_block = (
        "\n".join(pending_lines) or "- No prior clarification prompts were captured."
    )
    return (
        "Update the existing Jira parent/child hierarchy using the PM clarification reply below.\n\n"
        f"## Parent feature\n"
        f"- Key: {parent_detail.key}\n"
        f"- Summary: {parent_detail.summary}\n"
        f"Description:\n{parent_detail.description.strip() or 'No description provided.'}\n\n"
        "## Pending product-behavior clarification\n"
        f"{questions_block}\n\n"
        "## PM / stakeholder reply\n"
        f"{reply_text.strip()}\n\n"
        "## Affected engineering children (refresh all of these)\n"
        f"{child_block}\n\n"
        "Instructions:\n"
        "- Update the parent issue first using the clarified behavior.\n"
        "- Refresh all listed engineering child tickets from the updated parent.\n"
        "- If no engineering child tickets need changes, return engineering_children as an empty array.\n"
        "- Do not create duplicates.\n"
        "- Keep the parent product-focused and the children technical.\n"
    )


def combined_child_updates(
    *, seed_data: dict[str, Any]
) -> tuple[list[str], list[str], list[str]]:
    updated_children = [
        str(value).strip().upper()
        for value in seed_data.get("updated_children", [])
        if str(value).strip()
    ]
    created_children = [
        str(value).strip().upper()
        for value in seed_data.get("created_children", [])
        if str(value).strip()
    ]
    changed_children = list(dict.fromkeys([*updated_children, *created_children]))
    return updated_children, created_children, changed_children


def sync_completion_note(
    *, updated_children: list[str], created_children: list[str]
) -> str:
    parts: list[str] = ["Parent feature sync complete after Jira edit."]
    if updated_children:
        parts.append(
            f"Refreshed engineering child tickets: {', '.join(updated_children)}."
        )
    if created_children:
        parts.append(
            f"Created engineering child tickets: {', '.join(created_children)}."
        )
    if not updated_children and not created_children:
        parts.append("No engineering child changes were required.")
    return " ".join(parts)


def build_parent_seed_prompt(*, parent_detail: JiraIssueDetail) -> str:
    return (
        "Create or refresh the engineering child tickets needed to deliver this PM parent feature.\n\n"
        f"## Parent feature\n"
        f"- Key: {parent_detail.key}\n"
        f"- Summary: {parent_detail.summary}\n\n"
        f"Description:\n{parent_detail.description.strip() or 'No description provided.'}\n\n"
        "Instructions:\n"
        "- Keep the parent issue product-focused.\n"
        "- Create the technical engineering child tickets needed for implementation.\n"
        "- Provide concrete acceptance and how-to-test details for each child ticket.\n"
        "- Preserve the existing parent issue key.\n"
        "- Do not create duplicate child tickets.\n"
    )


def fanout_completion_note(
    *,
    target_status: str,
    promoted_children: list[str],
    unchanged_children: list[str],
    skipped_children: list[str],
    failed_children: list[str],
) -> str:
    parts = [
        f"Parent feature moved onto the board. Promoted engineering child tickets to {target_status}: {', '.join(promoted_children)}."
        if promoted_children
        else f"Parent feature moved onto the board. No engineering child tickets needed promotion to {target_status}."
    ]
    if unchanged_children:
        parts.append(f"Already on board or terminal: {', '.join(unchanged_children)}.")
    if skipped_children:
        parts.append(
            f"Skipped non-engineering children: {', '.join(skipped_children)}."
        )
    if failed_children:
        parts.append(f"Failed to promote: {', '.join(failed_children)}.")
    return " ".join(parts)


def question_text(value: object) -> str:
    question = ClarificationQuestion.parse(value)
    return question.question if question is not None else ""


def question_why_it_matters(value: object) -> str | None:
    question = ClarificationQuestion.parse(value)
    return question.why_it_matters if question is not None else None
