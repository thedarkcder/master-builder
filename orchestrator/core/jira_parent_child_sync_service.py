from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.api.discord.seed.description import build_parent_feature_description
from orchestrator.core.runtime_invocation import AgentInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.specialist_planning import (
    PLANNING_STATE_COMPLETED,
    SpecialistPlanningRequest,
    build_runtime_seed_planning_package,
    run_specialist_planning_fanout,
)
from orchestrator.core.followup_context_service import (
    FOLLOWUP_CONTEXT_PM_INTERVIEW,
    FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    close_followup_contexts,
    resolve_issue_followup_context,
    upsert_followup_context,
)
from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
    PM_INTERVIEW_STATUS_QUESTION_PENDING,
    normalize_parent_feature_brief_with_runtime,
    persist_parent_feature_brief_snapshot,
    resolve_parent_feature_brief,
    resolve_parent_feature_case,
    upsert_pm_interview_case,
)
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.storage.models import Project
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError
from orchestrator.tools.jira_oauth import JiraIssueDetail

logger = logging.getLogger(__name__)

_SYSTEM_COMMENT_MARKER = "[mb-system]"
_SYNC_LABELS = {"sync-current", "sync-stale", "sync-blocked"}
_PARENT_BOARD_ENTRY_STATUSES = {"to do", "ready for agent"}
_CHILD_STATUSES_TO_LEAVE = {"to do", "ready for agent", "in progress", "testing", "done"}
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


@dataclass(frozen=True)
class JiraParentChildSyncResult:
    handled: bool
    reason: str | None = None
    extra: dict[str, object] = field(default_factory=dict)


def is_system_generated_comment(*, text: str | None) -> bool:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        return False
    return normalized_text.startswith(_SYSTEM_COMMENT_MARKER)


def _project_key_for_issue(issue_key: str) -> str:
    normalized = str(issue_key or "").strip().upper()
    if "-" not in normalized:
        return normalized
    return normalized.split("-", 1)[0]


def _normalize_sync_labels(labels: list[str], *, target_label: str) -> list[str]:
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


def _update_issue_sync_label(
    *,
    oauth,
    issue_detail: JiraIssueDetail,
    target_label: str,
) -> None:
    oauth.client.replace_issue_labels(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=issue_detail.key,
        labels=_normalize_sync_labels(issue_detail.labels, target_label=target_label),
    )


def _extract_parent_issue_key(*, child_detail: JiraIssueDetail) -> str | None:
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


def _material_parent_changed_fields(*, payload: dict[str, Any], extract_changed_fields_fn) -> list[str]:  # noqa: ANN001
    changed_fields = extract_changed_fields_fn(payload)
    material: list[str] = []
    seen: set[str] = set()
    for changed_field in changed_fields:
        normalized = str(changed_field or "").strip().casefold()
        if normalized in {"status", "labels"}:
            continue
        if normalized in _PARENT_SYNC_MATERIAL_FIELDS or normalized in {"summary", "description"}:
            if normalized not in seen:
                seen.add(normalized)
                material.append(normalized)
    return material


def _normalize_status_name(value: str | None) -> str:
    return str(value or "").strip().casefold()


def _parent_board_entry_target_status(*, payload: dict[str, Any], extract_status_transition_fn) -> str | None:  # noqa: ANN001
    from_status, to_status = extract_status_transition_fn(payload)
    normalized_to_status = _normalize_status_name(to_status)
    if normalized_to_status not in _PARENT_BOARD_ENTRY_STATUSES:
        return None
    if _normalize_status_name(from_status) == normalized_to_status:
        return None
    target_status = str(to_status or "").strip()
    return target_status or None


def _build_child_snapshot_markdown(child_detail: JiraIssueDetail) -> str:
    return (
        f"### {child_detail.key}: {child_detail.summary}\n"
        f"Labels: {', '.join(child_detail.labels) if child_detail.labels else 'none'}\n"
        f"Description:\n{child_detail.description.strip() or 'No description provided.'}"
    )


def _build_parent_resync_prompt(
    *,
    parent_detail: JiraIssueDetail,
    child_details: list[JiraIssueDetail],
    changed_fields: list[str],
) -> str:
    child_block = "\n\n".join(_build_child_snapshot_markdown(detail) for detail in child_details)
    changed_lines = "\n".join(f"- {field}" for field in changed_fields) or "- description"
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


def _build_clarification_followup_prompt(
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
    child_block = "\n\n".join(_build_child_snapshot_markdown(detail) for detail in child_details)
    questions_block = "\n".join(pending_lines) or "- No prior clarification prompts were captured."
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


def _sync_note(*, body: str) -> str:
    return f"{_SYSTEM_COMMENT_MARKER} {body}".strip()


def _post_sync_note(
    *,
    session: Session,
    tenant,
    issue_key: str,
    settings,  # noqa: ANN001
    body: str,
    post_jira_comment_fn,
) -> tuple[bool, str | None]:  # noqa: ANN001
    return post_jira_comment_fn(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        comment=_sync_note(body=body),
        settings=settings,
    )


def _load_child_details(
    *,
    oauth,
    project_key: str,
    parent_issue_key: str,
    list_child_issue_previews_for_parent_fn,
) -> list[JiraIssueDetail]:  # noqa: ANN001
    child_previews = list_child_issue_previews_for_parent_fn(
        oauth={
            "client": oauth.client,
            "access_token": oauth.access_token,
            "cloud_id": oauth.connection.cloud_id,
        },
        project_key=project_key,
        parent_issue_key=parent_issue_key,
    )
    details: list[JiraIssueDetail] = []
    for preview in child_previews:
        details.append(
            oauth.client.get_issue_detail(
                access_token=oauth.access_token,
                cloud_id=oauth.connection.cloud_id,
                issue_id_or_key=preview.key,
            )
        )
    return details


def _mark_issues_sync_blocked(
    *,
    oauth,
    issue_keys: list[str],
) -> None:
    for issue_key in issue_keys:
        detail = oauth.client.get_issue_detail(
            access_token=oauth.access_token,
            cloud_id=oauth.connection.cloud_id,
            issue_id_or_key=issue_key,
        )
        _update_issue_sync_label(
            oauth=oauth,
            issue_detail=detail,
            target_label="sync-blocked",
        )


def _combined_child_updates(*, seed_data: dict[str, Any]) -> tuple[list[str], list[str], list[str]]:
    updated_children = [
        str(value).strip().upper() for value in seed_data.get("updated_children", []) if str(value).strip()
    ]
    created_children = [
        str(value).strip().upper() for value in seed_data.get("created_children", []) if str(value).strip()
    ]
    combined: list[str] = []
    seen: set[str] = set()
    for issue_key in [*updated_children, *created_children]:
        if issue_key in seen:
            continue
        seen.add(issue_key)
        combined.append(issue_key)
    return updated_children, created_children, combined


def _sync_completion_note(*, updated_children: list[str], created_children: list[str]) -> str:
    parts = ["Parent feature sync complete after Jira edit."]
    if updated_children:
        parts.append(f"Refreshed engineering child tickets: {', '.join(updated_children)}.")
    if created_children:
        parts.append(f"Created engineering child tickets: {', '.join(created_children)}.")
    if not updated_children and not created_children:
        parts.append("No engineering child changes were required.")
    return " ".join(parts)


def _build_parent_seed_prompt(
    *,
    parent_detail: JiraIssueDetail,
) -> str:
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


def _resolve_parent_product_brief(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant_id: str,
    project_id: str | None,
    parent_detail: JiraIssueDetail,
    build_runtime_for_selector_fn,
    refresh: bool = False,
) -> tuple[dict[str, object], list[str]]:
    canonical_brief = None if refresh else resolve_parent_feature_brief(
        session=session,
        tenant_id=tenant_id,
        parent_issue_key=parent_detail.key,
    )
    if canonical_brief is not None:
        return canonical_brief.to_payload(), []
    runtime = build_runtime_for_selector_fn(
        session=session,
        settings=settings,
        tenant_id=tenant_id,
        project_id=project_id,
        selector="workflow.pm_parent_brief_normalization",
    )
    normalization = normalize_parent_feature_brief_with_runtime(
        runtime=runtime,
        parent_issue_key=parent_detail.key,
        parent_summary=parent_detail.summary,
        parent_description=parent_detail.description,
        invocation_context=AgentInvocationContext(
            channel="jira",
            tenant_id=tenant_id,
            project_id=project_id,
            command="pm",
            stage="pm_parent_brief_normalization",
            working_dir=".",
            issue_key=parent_detail.key,
        ),
    )
    brief_payload = dict(normalization.get("brief") or {})
    open_questions = [str(value).strip() for value in normalization.get("open_questions", []) if str(value).strip()]
    persist_parent_feature_brief_snapshot(
        session=session,
        tenant_id=tenant_id,
        project_id=project_id,
        parent_issue_key=parent_detail.key,
        source_text=parent_detail.description,
        brief=brief_payload,
        notes={
            "source": "jira_parent_brief_normalization",
            "parent_summary": parent_detail.summary,
        },
    )
    return brief_payload, open_questions


def _rewrite_parent_issue_from_brief(
    *,
    oauth,
    parent_detail: JiraIssueDetail,
    brief_payload: dict[str, object],
    sync_status: str,
    planning_state: str | None = None,
    architecture_summary: list[str] | None = None,
    architecture_diagram: str | None = None,
    open_questions: list[str] | None = None,
) -> None:
    normalized_open_questions = [
        str(value).strip()
        for value in (open_questions if open_questions is not None else brief_payload.get("open_questions", []))
        if str(value).strip()
    ]
    description = build_parent_feature_description(
        objective=str(brief_payload.get("objective") or "").strip(),
        user_value=str(brief_payload.get("user_value") or "").strip(),
        recommendation=str(brief_payload.get("recommendation") or "").strip(),
        scope_in=[
            str(value).strip()
            for value in brief_payload.get("scope_in", [])
            if str(value).strip()
        ],
        scope_out=[
            str(value).strip()
            for value in brief_payload.get("scope_out", [])
            if str(value).strip()
        ],
        acceptance_criteria=[
            str(value).strip()
            for value in brief_payload.get("acceptance_criteria", [])
            if str(value).strip()
        ],
        ui_references=[
            str(value).strip()
            for value in brief_payload.get("ui_references", [])
            if str(value).strip()
        ],
        success_outcomes=[
            str(value).strip()
            for value in brief_payload.get("success_outcomes", [])
            if str(value).strip()
        ],
        dependencies_and_risks=[
            *[
                str(value).strip()
                for value in brief_payload.get("constraints", [])
                if str(value).strip()
            ],
            *[
                str(value).strip()
                for value in brief_payload.get("risks", [])
                if str(value).strip()
            ],
        ],
        open_questions=normalized_open_questions,
        parent_revision="normalized-parent-brief",
        sync_status=sync_status,
        pm_status="pm_completed",
        planning_state=planning_state,
        architecture_summary=architecture_summary,
        architecture_diagram=architecture_diagram,
    )
    oauth.client.update_issue_fields(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=parent_detail.key,
        summary=parent_detail.summary,
        description=description,
        labels=list(parent_detail.labels or []),
    )


def _format_parent_brief_questions_for_discord(*, parent_issue_key: str, questions: list[str]) -> str:
    question_lines = "\n".join(f"- {value}" for value in questions if value.strip())
    return (
        f"Decision needed for `{parent_issue_key}` before I can finish backlog planning.\n"
        "Please reply in this thread with the missing product behavior:\n"
        f"{question_lines}"
    ).strip()


def _post_parent_brief_questions_to_discord(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant,
    project_id: str | None,
    parent_issue_key: str,
    questions: list[str],
) -> bool:
    if not questions:
        return False
    parent_case = resolve_parent_feature_case(
        session=session,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
        parent_issue_key=parent_issue_key,
    )
    project = session.get(Project, project_id) if project_id else None
    project_discord_config = dict(getattr(project, "discord_config", None) or {})
    project_channel_id = str(project_discord_config.get("channel_id") or "").strip() or str(
        (getattr(tenant, "discord_config", None) or {}).get("channel_id") or ""
    ).strip()
    existing_request_id = str(getattr(parent_case, "request_id", "") or "").strip() or None
    existing_source_kind = str(getattr(parent_case, "source_kind", "") or "").strip()
    reusable_request_id = (
        existing_request_id
        if existing_request_id and existing_source_kind != PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT
        else f"pm-parent-interview:{parent_issue_key}"
    )
    owner_user_id = str(getattr(parent_case, "owner_user_id", "") or "").strip() or None
    existing_root_channel_id = str(getattr(parent_case, "channel_id", "") or "").strip() or None
    existing_thread_channel_id = str(getattr(parent_case, "thread_channel_id", "") or "").strip() or None
    root_channel_id = existing_root_channel_id or project_channel_id or None
    if not root_channel_id:
        return False
    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not token_ref:
        return False
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        return False
    mention_prefix = f"<@{owner_user_id}> " if owner_user_id else ""
    message = f"{mention_prefix}{_format_parent_brief_questions_for_discord(parent_issue_key=parent_issue_key, questions=questions)}".strip()
    try:
        client = DiscordApiClient(bot_token=bot_token)
        posted_root_message_id = None
        thread_channel_id = existing_thread_channel_id
        if thread_channel_id:
            client.post_message(channel_id=thread_channel_id, content=message)
        else:
            posted = client.post_message(
                channel_id=root_channel_id,
                content=f"Parent feature `{parent_issue_key}` needs PM clarification before backlog planning can continue.",
            )
            posted_root_message_id = str((posted or {}).get("id") or "").strip() or None
            if not posted_root_message_id:
                return False
            thread_name = f"{getattr(tenant, 'tenant_id', 'tenant')}-pm-{parent_issue_key}".replace(" ", "-")[:100]
            thread_channel_id = client.create_thread_from_message(
                channel_id=root_channel_id,
                message_id=posted_root_message_id,
                name=thread_name,
            )
            if project is not None:
                raw_thread_ids = project_discord_config.get("ask_thread_channel_ids")
                thread_ids = (
                    [str(value).strip() for value in raw_thread_ids if str(value).strip()]
                    if isinstance(raw_thread_ids, list)
                    else []
                )
                if thread_channel_id not in thread_ids:
                    thread_ids.append(thread_channel_id)
                ask_message_map = dict(project_discord_config.get("ask_thread_by_message_id") or {})
                ask_message_map[posted_root_message_id] = thread_channel_id
                project_discord_config["ask_thread_channel_ids"] = thread_ids[-200:]
                project_discord_config["ask_thread_by_message_id"] = dict(list(ask_message_map.items())[-500:])
                project.discord_config = project_discord_config
                project.updated_at = datetime.now(timezone.utc)
            client.post_message(
                channel_id=thread_channel_id,
                content=message,
            )
    except (DiscordApiError, ValueError):
        logger.warning(
            "jira_parent_brief_question_discord_post_failed tenant_id=%s parent_issue_key=%s channel_id=%s",
            getattr(tenant, "tenant_id", None),
            parent_issue_key,
            root_channel_id,
            exc_info=True,
        )
        return False
    interview_case = upsert_pm_interview_case(
        session=session,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
        project_id=project_id,
        request_id=reusable_request_id,
        source_kind="jira_parent",
        channel_id=root_channel_id,
        thread_channel_id=thread_channel_id if thread_channel_id != root_channel_id else None,
        root_message_id=posted_root_message_id or str(getattr(parent_case, "root_message_id", "") or "").strip() or None,
        owner_user_id=owner_user_id,
        parent_issue_key=parent_issue_key,
        source_text=str(getattr(parent_case, "source_text", "") or "") or f"Parent issue {parent_issue_key} requires PM clarification.",
        status=PM_INTERVIEW_STATUS_QUESTION_PENDING,
        brief=resolve_parent_feature_brief(
            session=session,
            tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
            parent_issue_key=parent_issue_key,
        ),
        notes={
            "source": "jira_parent_brief_normalization",
            "normalization_questions": list(questions),
        },
    )
    upsert_followup_context(
        session=session,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
        project_id=project_id,
        context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
        channel_id=root_channel_id,
        thread_channel_id=thread_channel_id if thread_channel_id != root_channel_id else None,
        root_message_id=str(getattr(interview_case, "root_message_id", "") or "").strip() or None,
        owner_user_id=owner_user_id or None,
        origin_command="pm",
        issue_key=parent_issue_key,
        request_id=str(getattr(interview_case, "request_id", "") or "").strip() or None,
        metadata={
            "parent_issue_key": parent_issue_key,
            "questions": list(questions),
            "source": "jira_parent_brief_normalization",
        },
    )
    if project is not None:
        tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return True


def _fanout_completion_note(
    *,
    target_status: str,
    promoted_children: list[str],
    unchanged_children: list[str],
    skipped_children: list[str],
    failed_children: list[str],
) -> str:
    parts = [f"Parent feature moved onto the board. Promoted engineering child tickets to {target_status}: {', '.join(promoted_children)}." if promoted_children else f"Parent feature moved onto the board. No engineering child tickets needed promotion to {target_status}."]
    if unchanged_children:
        parts.append(f"Already on board or terminal: {', '.join(unchanged_children)}.")
    if skipped_children:
        parts.append(f"Skipped non-engineering children: {', '.join(skipped_children)}.")
    if failed_children:
        parts.append(f"Failed to promote: {', '.join(failed_children)}.")
    return " ".join(parts)


def handle_parent_feature_sync(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    tenant_jira_oauth_context_fn,
    extract_changed_fields_fn,
    extract_status_transition_fn,
    list_child_issue_previews_for_parent_fn,
    build_runtime_for_selector_fn,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    normalized_labels = {str(label).strip().casefold() for label in context.issue_labels or []}
    if context.webhook_event not in {"issue_created", "issue_updated"} or "pm-parent" not in normalized_labels:
        return JiraParentChildSyncResult(handled=False)
    if context.webhook_event == "issue_created":
        oauth = tenant_jira_oauth_context_fn(session=session, tenant=context.tenant, settings=settings)
        parent_detail = oauth.client.get_issue_detail(
            access_token=oauth.access_token,
            cloud_id=oauth.connection.cloud_id,
            issue_id_or_key=context.issue_key,
        )
        project_key = _project_key_for_issue(context.issue_key)
        product_brief, normalization_questions = _resolve_parent_product_brief(
            session=session,
            settings=settings,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            parent_detail=parent_detail,
            build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        )
        _rewrite_parent_issue_from_brief(
            oauth=oauth,
            parent_detail=parent_detail,
            brief_payload=product_brief,
            sync_status="sync-blocked" if normalization_questions else "children_syncing",
            planning_state="brief_normalized",
            open_questions=normalization_questions or None,
        )
        if normalization_questions:
            _update_issue_sync_label(
                oauth=oauth,
                issue_detail=parent_detail,
                target_label="sync-blocked",
            )
            _post_parent_brief_questions_to_discord(
                session=session,
                settings=settings,
                tenant=context.tenant,
                project_id=context.project_id,
                parent_issue_key=parent_detail.key,
                questions=normalization_questions,
            )
            question_block = " ".join(normalization_questions)
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=context.issue_key,
                settings=settings,
                body=(
                    "Parent feature was created in backlog, but brief normalization is blocked pending clarification. "
                    f"{question_block}"
                ),
                post_jira_comment_fn=post_jira_comment_fn,
            )
            return JiraParentChildSyncResult(
                handled=True,
                reason="pm_parent_issue_created_brief_blocked",
                extra={
                    "questions": normalization_questions,
                    "webhook_event": context.webhook_event,
                },
            )
        prompt_markdown = _build_parent_seed_prompt(parent_detail=parent_detail)
        planning_runtime = build_runtime_for_selector_fn(
            session=session,
            settings=settings,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            selector="workflow.pm_planning_architect",
        )
        planning_result = run_specialist_planning_fanout(
            runtime=planning_runtime,
            request=SpecialistPlanningRequest(
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                parent_issue_key=parent_detail.key,
                parent_summary=parent_detail.summary,
                parent_description=parent_detail.description,
                product_brief=product_brief,
                project_keys=(project_key,),
                related_issues=(),
                status_counts={parent_detail.status: 1},
                github_context={},
                conversation_history=(),
                working_dir=".",
            ),
            runtime_for_selector=lambda selector: build_runtime_for_selector_fn(
                session=session,
                settings=settings,
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                selector=selector,
            ),
        )
        planning_package = build_runtime_seed_planning_package(
            result=planning_result,
            behavior_slice=str(product_brief.get("objective") or parent_detail.summary).strip() or parent_detail.summary,
        )
        try:
            _, seed_data = seed_issues_with_runtime_fn(
                session=session,
                tenant=context.tenant,
                prompt_markdown=prompt_markdown,
                scoped_project_id=context.project_id,
                force_issue_keys=[context.issue_key],
                allow_create=True,
                allow_empty_children=planning_result.planning_state != PLANNING_STATE_COMPLETED,
                scoped_project_keys=[project_key],
                codex_working_dir=".",
                planning_package=planning_package,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "jira_parent_issue_created_seed_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
                context.request_id,
                context.tenant_id,
                context.issue_key,
                exc,
            )
            _update_issue_sync_label(
                oauth=oauth,
                issue_detail=parent_detail,
                target_label="sync-blocked",
            )
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=context.issue_key,
                settings=settings,
                body=f"Parent feature was created in backlog, but engineering child planning failed. Error: {exc}",
                post_jira_comment_fn=post_jira_comment_fn,
            )
            return JiraParentChildSyncResult(
                handled=True,
                reason="pm_parent_issue_created_seed_failed",
                extra={"webhook_event": context.webhook_event},
            )

        updated_children, created_children, changed_children = _combined_child_updates(seed_data=seed_data)
        if planning_result.planning_state != PLANNING_STATE_COMPLETED or bool(seed_data.get("requires_input")):
            _update_issue_sync_label(
                oauth=oauth,
                issue_detail=parent_detail,
                target_label="sync-blocked",
            )
            questions = list(planning_result.open_behavior_questions) or [
                str(value).strip() for value in seed_data.get("questions", []) if str(value).strip()
            ]
            question_block = " ".join(questions) if questions else "More product detail is required."
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=context.issue_key,
                settings=settings,
                body=(
                    "Parent feature was created in backlog, but engineering child planning is blocked pending clarification. "
                    f"{question_block}"
                ),
                post_jira_comment_fn=post_jira_comment_fn,
            )
            return JiraParentChildSyncResult(
                handled=True,
                reason="pm_parent_issue_created_seed_blocked",
                extra={
                    "questions": questions,
                    "parent_revision": seed_data.get("parent_revision"),
                    "children_sync_status": seed_data.get("children_sync_status"),
                    "webhook_event": context.webhook_event,
                },
            )

        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=(
                "Backlog parent feature planning complete. "
                f"{_sync_completion_note(updated_children=updated_children, created_children=created_children)}"
            ),
            post_jira_comment_fn=post_jira_comment_fn,
        )
        for child_key in changed_children:
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=child_key,
                settings=settings,
                body=f"Created or refreshed from parent feature {context.issue_key} during backlog planning.",
                post_jira_comment_fn=post_jira_comment_fn,
            )
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_parent_issue_created_seed_completed",
            extra={
                "updated_children": changed_children,
                "parent_revision": seed_data.get("parent_revision"),
                "children_sync_status": seed_data.get("children_sync_status"),
                "webhook_event": context.webhook_event,
            },
        )
    material_changed_fields = _material_parent_changed_fields(
        payload=context.payload,
        extract_changed_fields_fn=extract_changed_fields_fn,
    )
    if not material_changed_fields:
        board_entry_target_status = _parent_board_entry_target_status(
            payload=context.payload,
            extract_status_transition_fn=extract_status_transition_fn,
        )
        if board_entry_target_status:
            oauth = tenant_jira_oauth_context_fn(session=session, tenant=context.tenant, settings=settings)
            project_key = _project_key_for_issue(context.issue_key)
            child_details = _load_child_details(
                oauth=oauth,
                project_key=project_key,
                parent_issue_key=context.issue_key,
                list_child_issue_previews_for_parent_fn=list_child_issue_previews_for_parent_fn,
            )
            if not child_details:
                _post_sync_note(
                    session=session,
                    tenant=context.tenant,
                    issue_key=context.issue_key,
                    settings=settings,
                    body="Parent feature moved onto the board, but there are no engineering child tickets to promote.",
                    post_jira_comment_fn=post_jira_comment_fn,
                )
                return JiraParentChildSyncResult(
                    handled=True,
                    reason="pm_parent_board_entry_no_children",
                    extra={"target_status": board_entry_target_status, "webhook_event": context.webhook_event},
                )

            promoted_children: list[str] = []
            unchanged_children: list[str] = []
            skipped_children: list[str] = []
            failed_children: list[str] = []
            for child_detail in child_details:
                child_labels = {str(label).strip().casefold() for label in child_detail.labels}
                if "engineering-child" not in child_labels:
                    skipped_children.append(child_detail.key)
                    continue
                if _normalize_status_name(child_detail.status) in _CHILD_STATUSES_TO_LEAVE:
                    unchanged_children.append(child_detail.key)
                    continue
                try:
                    oauth.client.transition_issue(
                        access_token=oauth.access_token,
                        cloud_id=oauth.connection.cloud_id,
                        issue_id_or_key=child_detail.key,
                        target_status=board_entry_target_status,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception(
                        "jira_parent_board_entry_child_transition_failed request_id=%s tenant_id=%s parent_issue_key=%s child_issue_key=%s target_status=%s error=%s",
                        context.request_id,
                        context.tenant_id,
                        context.issue_key,
                        child_detail.key,
                        board_entry_target_status,
                        exc,
                    )
                    failed_children.append(child_detail.key)
                    continue
                promoted_children.append(child_detail.key)

            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=context.issue_key,
                settings=settings,
                body=_fanout_completion_note(
                    target_status=board_entry_target_status,
                    promoted_children=promoted_children,
                    unchanged_children=unchanged_children,
                    skipped_children=skipped_children,
                    failed_children=failed_children,
                ),
                post_jira_comment_fn=post_jira_comment_fn,
            )
            return JiraParentChildSyncResult(
                handled=True,
                reason="pm_parent_board_entry_fanout_completed" if not failed_children else "pm_parent_board_entry_fanout_partial",
                extra={
                    "target_status": board_entry_target_status,
                    "promoted_children": promoted_children,
                    "unchanged_children": unchanged_children,
                    "skipped_children": skipped_children,
                    "failed_children": failed_children,
                    "webhook_event": context.webhook_event,
                },
            )
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_parent_non_material_change",
            extra={"changed_fields": [], "webhook_event": context.webhook_event},
        )
    oauth = tenant_jira_oauth_context_fn(session=session, tenant=context.tenant, settings=settings)
    parent_detail = oauth.client.get_issue_detail(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=context.issue_key,
    )
    project_key = _project_key_for_issue(context.issue_key)
    child_details = _load_child_details(
        oauth=oauth,
        project_key=project_key,
        parent_issue_key=context.issue_key,
        list_child_issue_previews_for_parent_fn=list_child_issue_previews_for_parent_fn,
    )
    product_brief, normalization_questions = _resolve_parent_product_brief(
        session=session,
        settings=settings,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        parent_detail=parent_detail,
        build_runtime_for_selector_fn=build_runtime_for_selector_fn,
        refresh=True,
    )
    _rewrite_parent_issue_from_brief(
        oauth=oauth,
        parent_detail=parent_detail,
        brief_payload=product_brief,
        sync_status="sync-blocked" if normalization_questions else "children_syncing",
        planning_state="brief_normalized",
        open_questions=normalization_questions or None,
    )
    if normalization_questions:
        blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=blocked_issue_keys)
        _post_parent_brief_questions_to_discord(
            session=session,
            settings=settings,
            tenant=context.tenant,
            project_id=context.project_id,
            parent_issue_key=parent_detail.key,
            questions=normalization_questions,
        )
        question_block = " ".join(normalization_questions)
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=f"Parent feature changed but brief normalization is blocked pending clarification. {question_block}",
            post_jira_comment_fn=post_jira_comment_fn,
        )
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_parent_sync_brief_blocked",
            extra={
                "changed_fields": material_changed_fields,
                "questions": normalization_questions,
                "webhook_event": context.webhook_event,
            },
        )
    if not child_details:
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body="Parent feature changed, but there are no engineering child tickets to refresh.",
            post_jira_comment_fn=post_jira_comment_fn,
        )
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_parent_no_children",
            extra={"changed_fields": material_changed_fields, "webhook_event": context.webhook_event},
        )
    prompt_markdown = _build_parent_resync_prompt(
        parent_detail=parent_detail,
        child_details=child_details,
        changed_fields=material_changed_fields,
    )
    force_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
    try:
        _, seed_data = seed_issues_with_runtime_fn(
            session=session,
            tenant=context.tenant,
            prompt_markdown=prompt_markdown,
            scoped_project_id=context.project_id,
            force_issue_keys=force_issue_keys,
            allow_create=True,
            allow_empty_children=True,
            scoped_project_keys=[project_key],
            codex_working_dir=".",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "jira_parent_sync_failed request_id=%s tenant_id=%s issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=blocked_issue_keys)
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=(
                "Parent feature changed and child refresh failed. "
                f"Blocked {len(child_details)} engineering child ticket(s). Error: {exc}"
            ),
            post_jira_comment_fn=post_jira_comment_fn,
        )
        for detail in child_details:
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=detail.key,
                settings=settings,
                body=f"Blocked because parent feature {context.issue_key} changed and refresh failed.",
                post_jira_comment_fn=post_jira_comment_fn,
            )
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_parent_sync_failed",
            extra={
                "changed_fields": material_changed_fields,
                "stale_child_keys": [detail.key for detail in child_details],
                "webhook_event": context.webhook_event,
            },
        )

    updated_children, created_children, changed_children = _combined_child_updates(seed_data=seed_data)
    if bool(seed_data.get("requires_input")):
        blocked_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=blocked_issue_keys)
        questions = [str(value).strip() for value in seed_data.get("questions", []) if str(value).strip()]
        question_block = " ".join(questions) if questions else "More product detail is required."
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=f"Parent feature changed but child sync is blocked pending clarification. {question_block}",
            post_jira_comment_fn=post_jira_comment_fn,
        )
        for detail in child_details:
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=detail.key,
                settings=settings,
                body=f"Still blocked because parent feature {context.issue_key} needs clarification before refresh can complete.",
                post_jira_comment_fn=post_jira_comment_fn,
            )
        return JiraParentChildSyncResult(
            handled=True,
            reason="pm_parent_sync_blocked",
            extra={
                "changed_fields": material_changed_fields,
                "stale_child_keys": [detail.key for detail in child_details],
                "questions": questions,
                "webhook_event": context.webhook_event,
            },
        )

    _post_sync_note(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        settings=settings,
        body=f"{_sync_completion_note(updated_children=updated_children, created_children=created_children)} Changed fields: {', '.join(material_changed_fields)}.",
        post_jira_comment_fn=post_jira_comment_fn,
    )
    for child_key in changed_children:
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=child_key,
            settings=settings,
            body=f"Refreshed from parent feature {context.issue_key} after Jira product update.",
            post_jira_comment_fn=post_jira_comment_fn,
        )
    return JiraParentChildSyncResult(
        handled=True,
        reason="pm_parent_sync_completed",
        extra={
            "changed_fields": material_changed_fields,
            "updated_children": changed_children,
            "parent_revision": seed_data.get("parent_revision"),
            "children_sync_status": seed_data.get("children_sync_status"),
            "webhook_event": context.webhook_event,
        },
    )


def handle_engineering_clarification_command(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    tenant_jira_oauth_context_fn,
    build_runtime_for_selector_fn,
    classify_engineering_clarification_with_codex_fn,
    post_jira_comment_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    if context.comment_command != "clarify" or not context.project_id:
        return JiraParentChildSyncResult(handled=False)
    question = str(context.comment_command_argument or "").strip()
    if not question:
        return JiraParentChildSyncResult(handled=True, reason="invalid_comment_command")
    oauth = tenant_jira_oauth_context_fn(session=session, tenant=context.tenant, settings=settings)
    child_detail = oauth.client.get_issue_detail(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=context.issue_key,
    )
    child_labels = {str(label).strip().casefold() for label in child_detail.labels}
    if "engineering-child" not in child_labels:
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarify_requires_engineering_child",
            extra={"webhook_event": context.webhook_event},
        )
    parent_issue_key = _extract_parent_issue_key(child_detail=child_detail)
    if not parent_issue_key:
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_child_parent_missing",
            extra={"webhook_event": context.webhook_event},
        )
    parent_detail = oauth.client.get_issue_detail(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=parent_issue_key,
    )
    runtime = build_runtime_for_selector_fn(
        session=session,
        settings=settings,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        selector="discord.pm_answer",
        agent_role="pm",
        agent_name="pm_primary",
    )
    try:
        translation = classify_engineering_clarification_with_codex_fn(
            runtime=runtime,
            parent_issue_key=parent_issue_key,
            parent_summary=parent_detail.summary,
            parent_description=parent_detail.description,
            child_issue_key=child_detail.key,
            child_summary=child_detail.summary,
            child_description=child_detail.description,
            question=question,
            invocation_context=AgentInvocationContext(
                channel="jira",
                tenant_id=context.tenant_id,
                project_id=context.project_id,
                command="clarify",
                stage="pm-translation",
                working_dir=".",
                issue_key=child_detail.key,
            ),
        )
    except CodexRuntimeError as exc:
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarification_translation_failed",
            extra={"error": str(exc), "webhook_event": context.webhook_event},
        )
    classification = str(translation.get("classification") or "").strip().lower() or "technical_implementation"
    stakeholder_question = str(translation.get("stakeholder_question") or "").strip()
    child_block_note = str(translation.get("child_block_note") or "").strip()
    reason = str(translation.get("reason") or "").strip()
    if classification != "product_behavior" or not stakeholder_question:
        comment_text = child_block_note or "This question stays with engineering implementation and does not reopen the PM parent brief."
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=child_detail.key,
            settings=settings,
            body=comment_text,
            post_jira_comment_fn=post_jira_comment_fn,
        )
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarification_not_product_behavior",
            extra={
                "classification": classification,
                "detail_reason": reason,
                "webhook_event": context.webhook_event,
            },
        )

    _update_issue_sync_label(
        oauth=oauth,
        issue_detail=child_detail,
        target_label="sync-blocked",
    )
    existing_context = resolve_issue_followup_context(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=parent_issue_key,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    )
    metadata = dict(getattr(existing_context, "metadata_json", {}) or {})
    existing_questions = metadata.get("questions")
    question_entries = list(existing_questions) if isinstance(existing_questions, list) else []
    question_entries.append(
        {
            "source_child_key": child_detail.key,
            "original_question": question,
            "stakeholder_question": stakeholder_question,
            "requested_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    affected_child_keys = {
        *[str(value).strip().upper() for value in metadata.get("affected_child_keys", []) if str(value).strip()],
        child_detail.key.upper(),
    }
    merged_metadata = {
        **metadata,
        "parent_issue_key": parent_issue_key,
        "project_id": context.project_id,
        "project_key": _project_key_for_issue(parent_issue_key),
        "affected_child_keys": sorted(affected_child_keys),
        "questions": question_entries,
        "parent_updated": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    upsert_followup_context(
        session=session,
        tenant_id=context.tenant_id,
        project_id=context.project_id,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
        origin_command="clarify",
        issue_key=parent_issue_key,
        request_id=f"engineering-clarification:{parent_issue_key}",
        metadata=merged_metadata,
    )
    session.commit()
    _post_sync_note(
        session=session,
        tenant=context.tenant,
        issue_key=parent_issue_key,
        settings=settings,
        body=(
            f"Engineering needs a product clarification for child {child_detail.key}. "
            f"Reply on this parent issue with the decision: {stakeholder_question}"
        ),
        post_jira_comment_fn=post_jira_comment_fn,
    )
    _post_sync_note(
        session=session,
        tenant=context.tenant,
        issue_key=child_detail.key,
        settings=settings,
        body=(
            f"Blocked pending PM clarification on parent feature {parent_issue_key}. "
            f"{child_block_note or stakeholder_question}"
        ),
        post_jira_comment_fn=post_jira_comment_fn,
    )
    return JiraParentChildSyncResult(
        handled=True,
        reason="comment_command_clarify",
        extra={
            "classification": classification,
            "parent_issue_key": parent_issue_key,
            "affected_child_keys": sorted(affected_child_keys),
            "stakeholder_question": stakeholder_question,
            "webhook_event": context.webhook_event,
        },
    )


def handle_engineering_clarification_reply(
    *,
    context: JiraParentChildSyncContext,
    session: Session,
    settings,  # noqa: ANN001
    tenant_jira_oauth_context_fn,
    seed_issues_with_runtime_fn,
    post_jira_comment_fn,
    extract_jira_comment_text_fn,
) -> JiraParentChildSyncResult:  # noqa: ANN001
    if context.comment_command is not None:
        return JiraParentChildSyncResult(handled=False)
    if not context.project_id or context.webhook_event not in {"comment_created", "comment_updated"}:
        return JiraParentChildSyncResult(handled=False)
    followup_context = resolve_issue_followup_context(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    )
    if followup_context is None:
        return JiraParentChildSyncResult(handled=False)
    comment = context.payload.get("comment")
    comment_body = ""
    if isinstance(comment, dict):
        body = comment.get("body")
        if isinstance(body, str):
            comment_body = body
    if not comment_body:
        comment_body = str(extract_jira_comment_text_fn(context.payload) or "")
    if not comment_body or is_system_generated_comment(text=comment_body):
        return JiraParentChildSyncResult(handled=False)

    metadata = dict(getattr(followup_context, "metadata_json", {}) or {})
    affected_child_keys = [
        str(value).strip().upper() for value in metadata.get("affected_child_keys", []) if str(value).strip()
    ]
    if not affected_child_keys:
        return JiraParentChildSyncResult(
            handled=True,
            reason="clarification_context_missing_children",
            extra={"webhook_event": context.webhook_event},
        )

    oauth = tenant_jira_oauth_context_fn(session=session, tenant=context.tenant, settings=settings)
    parent_detail = oauth.client.get_issue_detail(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=context.issue_key,
    )
    child_details = [
        oauth.client.get_issue_detail(
            access_token=oauth.access_token,
            cloud_id=oauth.connection.cloud_id,
            issue_id_or_key=child_key,
        )
        for child_key in affected_child_keys
    ]
    prompt_markdown = _build_clarification_followup_prompt(
        parent_detail=parent_detail,
        child_details=child_details,
        metadata=metadata,
        reply_text=comment_body,
    )
    try:
        _, seed_data = seed_issues_with_runtime_fn(
            session=session,
            tenant=context.tenant,
            prompt_markdown=prompt_markdown,
            scoped_project_id=context.project_id,
            force_issue_keys=[context.issue_key, *affected_child_keys],
            allow_create=True,
            allow_empty_children=True,
            scoped_project_keys=[_project_key_for_issue(context.issue_key)],
            codex_working_dir=".",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "jira_engineering_clarification_reply_failed request_id=%s tenant_id=%s parent_issue_key=%s error=%s",
            context.request_id,
            context.tenant_id,
            context.issue_key,
            exc,
        )
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=[context.issue_key, *affected_child_keys])
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=f"Clarification reply was captured but parent/child refresh failed: {exc}",
            post_jira_comment_fn=post_jira_comment_fn,
        )
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_clarification_refresh_failed",
            extra={"stale_child_keys": affected_child_keys, "webhook_event": context.webhook_event},
        )

    if bool(seed_data.get("requires_input")):
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=[context.issue_key, *affected_child_keys])
        metadata["parent_updated"] = bool(seed_data.get("updated_parent") or seed_data.get("created_parent"))
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        upsert_followup_context(
            session=session,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
            origin_command="clarify",
            issue_key=context.issue_key,
            request_id=f"engineering-clarification:{context.issue_key}",
            metadata=metadata,
        )
        session.commit()
        questions = [str(value).strip() for value in seed_data.get("questions", []) if str(value).strip()]
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body=(
                "Clarification reply was recorded, but more product detail is still required before engineering can resume. "
                f"{' '.join(questions)}"
            ),
            post_jira_comment_fn=post_jira_comment_fn,
        )
        return JiraParentChildSyncResult(
            handled=True,
            reason="engineering_clarification_still_open",
            extra={
                "questions": questions,
                "stale_child_keys": affected_child_keys,
                "webhook_event": context.webhook_event,
            },
        )

    close_followup_contexts(
        session=session,
        tenant_id=context.tenant_id,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
        issue_key=context.issue_key,
    )
    session.commit()
    updated_children, created_children, changed_children = _combined_child_updates(seed_data=seed_data)
    _post_sync_note(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        settings=settings,
        body=(
            "Clarification reply applied to the parent feature and engineering child tickets are current again. "
            f"{'Refreshed children: ' + ', '.join(updated_children) + '.' if updated_children else ''} "
            f"{'Created children: ' + ', '.join(created_children) + '.' if created_children else ''} "
            f"{'No engineering child changes were required.' if not updated_children and not created_children else ''}"
        ).strip(),
        post_jira_comment_fn=post_jira_comment_fn,
    )
    for child_key in changed_children or affected_child_keys:
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=child_key,
            settings=settings,
            body=f"Product clarification from parent feature {context.issue_key} has been applied. Sync is current again.",
            post_jira_comment_fn=post_jira_comment_fn,
        )
    return JiraParentChildSyncResult(
        handled=True,
        reason="engineering_clarification_resolved",
        extra={
            "parent_issue_key": context.issue_key,
            "updated_children": changed_children,
            "parent_revision": seed_data.get("parent_revision"),
            "children_sync_status": seed_data.get("children_sync_status"),
            "webhook_event": context.webhook_event,
        },
    )
