from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from orchestrator.api.discord.ingress.seed_runtime import seed_issues_with_codex
from orchestrator.api.discord.seed.issue_service import list_child_issue_previews_for_parent
from orchestrator.api.webhooks.contracts import (
    extract_changed_fields,
    post_jira_comment,
)
from orchestrator.api.webhooks.jira_webhook_types import JiraWebhookContext, jira_webhook_response
from orchestrator.core.agent_runtime_resolver import build_runtime_for_selector
from orchestrator.core.codex_agents import classify_engineering_clarification_with_codex
from orchestrator.core.codex_invocation import CodexInvocationContext
from orchestrator.core.codex_runtime import CodexRuntimeError
from orchestrator.core.followup_context_service import (
    FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    close_followup_contexts,
    resolve_issue_followup_context,
    upsert_followup_context,
)
from orchestrator.api.jira_oauth.connection_service import tenant_jira_oauth_context
from orchestrator.tools.jira_oauth import JiraIssueDetail, JiraOAuthError

logger = logging.getLogger(__name__)

_SYSTEM_COMMENT_MARKER = "[mb-system]"
_SYNC_LABELS = {"sync-current", "sync-stale", "sync-blocked"}
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


def _material_parent_changed_fields(payload: dict[str, Any]) -> list[str]:
    changed_fields = extract_changed_fields(payload)
    material: list[str] = []
    seen: set[str] = set()
    for field in changed_fields:
        normalized = str(field or "").strip().casefold()
        if normalized == "status":
            continue
        if normalized == "labels":
            continue
        if normalized in _PARENT_SYNC_MATERIAL_FIELDS or normalized in {"summary", "description"}:
            if normalized not in seen:
                seen.add(normalized)
                material.append(normalized)
    return material


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
) -> tuple[bool, str | None]:
    return post_jira_comment(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        comment=_sync_note(body=body),
        settings=settings,
    )


def _load_child_details(*, oauth, project_key: str, parent_issue_key: str) -> list[JiraIssueDetail]:
    child_previews = list_child_issue_previews_for_parent(
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


def handle_parent_feature_sync(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    normalized_labels = {str(label).strip().casefold() for label in context.issue_labels or []}
    if context.webhook_event != "issue_updated" or "pm-parent" not in normalized_labels:
        return None
    material_changed_fields = _material_parent_changed_fields(context.payload)
    if not material_changed_fields:
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="pm_parent_non_material_change",
            changed_fields=[],
            webhook_event=context.webhook_event,
        )
    oauth = tenant_jira_oauth_context(session=session, tenant=context.tenant, settings=settings)
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
    )
    if not child_details:
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=context.issue_key,
            settings=settings,
            body="Parent feature changed, but there are no engineering child tickets to refresh.",
        )
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="pm_parent_no_children",
            changed_fields=material_changed_fields,
            webhook_event=context.webhook_event,
        )
    prompt_markdown = _build_parent_resync_prompt(
        parent_detail=parent_detail,
        child_details=child_details,
        changed_fields=material_changed_fields,
    )
    force_issue_keys = [context.issue_key, *[detail.key for detail in child_details]]
    try:
        _, seed_data = seed_issues_with_codex(
            session=session,
            tenant=context.tenant,
            prompt_markdown=prompt_markdown,
            scoped_project_id=context.project.project_id if context.project is not None else None,
            force_issue_keys=force_issue_keys,
            allow_create=False,
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
        )
        for detail in child_details:
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=detail.key,
                settings=settings,
                body=f"Blocked because parent feature {context.issue_key} changed and refresh failed.",
            )
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="pm_parent_sync_failed",
            changed_fields=material_changed_fields,
            stale_child_keys=[detail.key for detail in child_details],
            webhook_event=context.webhook_event,
        )

    updated_children = [
        *[str(value).strip().upper() for value in seed_data.get("updated_children", []) if str(value).strip()],
        *[str(value).strip().upper() for value in seed_data.get("created_children", []) if str(value).strip()],
    ]
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
        )
        for detail in child_details:
            _post_sync_note(
                session=session,
                tenant=context.tenant,
                issue_key=detail.key,
                settings=settings,
                body=f"Still blocked because parent feature {context.issue_key} needs clarification before refresh can complete.",
            )
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="pm_parent_sync_blocked",
            changed_fields=material_changed_fields,
            stale_child_keys=[detail.key for detail in child_details],
            questions=questions,
            webhook_event=context.webhook_event,
        )

    _post_sync_note(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        settings=settings,
        body=(
            f"Parent feature sync complete after Jira edit. "
            f"Changed fields: {', '.join(material_changed_fields)}. "
            f"Updated engineering child tickets: {', '.join(updated_children) if updated_children else 'none'}."
        ),
    )
    for child_key in updated_children:
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=child_key,
            settings=settings,
            body=f"Refreshed from parent feature {context.issue_key} after Jira product update.",
        )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="pm_parent_sync_completed",
        changed_fields=material_changed_fields,
        updated_children=updated_children,
        parent_revision=seed_data.get("parent_revision"),
        children_sync_status=seed_data.get("children_sync_status"),
        webhook_event=context.webhook_event,
    )


def handle_engineering_clarification_command(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    if context.comment_command != "clarify" or context.project is None:
        return None
    question = str(context.comment_command_argument or "").strip()
    if not question:
        return jira_webhook_response(context, enqueued=False, reason="invalid_comment_command")
    oauth = tenant_jira_oauth_context(session=session, tenant=context.tenant, settings=settings)
    child_detail = oauth.client.get_issue_detail(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=context.issue_key,
    )
    child_labels = {str(label).strip().casefold() for label in child_detail.labels}
    if "engineering-child" not in child_labels:
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="clarify_requires_engineering_child",
            webhook_event=context.webhook_event,
        )
    parent_issue_key = _extract_parent_issue_key(child_detail=child_detail)
    if not parent_issue_key:
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="engineering_child_parent_missing",
            webhook_event=context.webhook_event,
        )
    parent_detail = oauth.client.get_issue_detail(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=parent_issue_key,
    )
    runtime = build_runtime_for_selector(
        session=session,
        settings=settings,
        tenant_id=context.tenant_id,
        project_id=context.project.project_id,
        selector="discord.pm_answer",
    )
    try:
        translation = classify_engineering_clarification_with_codex(
            runtime=runtime,
            parent_issue_key=parent_issue_key,
            parent_summary=parent_detail.summary,
            parent_description=parent_detail.description,
            child_issue_key=child_detail.key,
            child_summary=child_detail.summary,
            child_description=child_detail.description,
            question=question,
            invocation_context=CodexInvocationContext(
                channel="jira",
                tenant_id=context.tenant_id,
                project_id=context.project.project_id,
                command="clarify",
                stage="pm-translation",
                working_dir=".",
                issue_key=child_detail.key,
            ),
        )
    except CodexRuntimeError as exc:
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="clarification_translation_failed",
            error=str(exc),
            webhook_event=context.webhook_event,
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
        )
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="clarification_not_product_behavior",
            classification=classification,
            detail_reason=reason,
            webhook_event=context.webhook_event,
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
        "project_id": context.project.project_id,
        "project_key": _project_key_for_issue(parent_issue_key),
        "affected_child_keys": sorted(affected_child_keys),
        "questions": question_entries,
        "parent_updated": False,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    upsert_followup_context(
        session=session,
        tenant_id=context.tenant_id,
        project_id=context.project.project_id,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
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
    )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="comment_command_clarify",
        classification=classification,
        parent_issue_key=parent_issue_key,
        affected_child_keys=sorted(affected_child_keys),
        stakeholder_question=stakeholder_question,
        webhook_event=context.webhook_event,
    )


def handle_engineering_clarification_reply(
    *,
    context: JiraWebhookContext,
    session: Session,
    settings,  # noqa: ANN001
) -> dict | None:
    if context.project is None or context.webhook_event not in {"comment_created", "comment_updated"}:
        return None
    followup_context = resolve_issue_followup_context(
        session=session,
        tenant_id=context.tenant_id,
        issue_key=context.issue_key,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
    )
    if followup_context is None:
        return None
    comment = context.payload.get("comment")
    comment_body = ""
    if isinstance(comment, dict):
        body = comment.get("body")
        if isinstance(body, str):
            comment_body = body
    if not comment_body:
        from orchestrator.api.webhooks.contracts import extract_jira_comment_text

        comment_body = str(extract_jira_comment_text(context.payload) or "")
    if not comment_body or is_system_generated_comment(text=comment_body):
        return None

    metadata = dict(getattr(followup_context, "metadata_json", {}) or {})
    affected_child_keys = [
        str(value).strip().upper() for value in metadata.get("affected_child_keys", []) if str(value).strip()
    ]
    if not affected_child_keys:
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="clarification_context_missing_children",
            webhook_event=context.webhook_event,
        )

    oauth = tenant_jira_oauth_context(session=session, tenant=context.tenant, settings=settings)
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
        _, seed_data = seed_issues_with_codex(
            session=session,
            tenant=context.tenant,
            prompt_markdown=prompt_markdown,
            scoped_project_id=context.project.project_id,
            force_issue_keys=[context.issue_key, *affected_child_keys],
            allow_create=False,
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
        )
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="engineering_clarification_refresh_failed",
            stale_child_keys=affected_child_keys,
            webhook_event=context.webhook_event,
        )

    if bool(seed_data.get("requires_input")):
        _mark_issues_sync_blocked(oauth=oauth, issue_keys=[context.issue_key, *affected_child_keys])
        metadata["parent_updated"] = bool(seed_data.get("updated_parent") or seed_data.get("created_parent"))
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        upsert_followup_context(
            session=session,
            tenant_id=context.tenant_id,
            project_id=context.project.project_id,
            context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
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
        )
        return jira_webhook_response(
            context,
            enqueued=False,
            reason="engineering_clarification_still_open",
            questions=questions,
            stale_child_keys=affected_child_keys,
            webhook_event=context.webhook_event,
        )

    close_followup_contexts(
        session=session,
        tenant_id=context.tenant_id,
        context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
        issue_key=context.issue_key,
    )
    session.commit()
    updated_children = [
        *[str(value).strip().upper() for value in seed_data.get("updated_children", []) if str(value).strip()],
        *[str(value).strip().upper() for value in seed_data.get("created_children", []) if str(value).strip()],
    ]
    _post_sync_note(
        session=session,
        tenant=context.tenant,
        issue_key=context.issue_key,
        settings=settings,
        body=(
            "Clarification reply applied to the parent feature and engineering child tickets are current again. "
            f"Updated children: {', '.join(updated_children) if updated_children else 'none'}."
        ),
    )
    for child_key in affected_child_keys:
        _post_sync_note(
            session=session,
            tenant=context.tenant,
            issue_key=child_key,
            settings=settings,
            body=f"Product clarification from parent feature {context.issue_key} has been applied. Sync is current again.",
        )
    return jira_webhook_response(
        context,
        enqueued=False,
        reason="engineering_clarification_resolved",
        parent_issue_key=context.issue_key,
        updated_children=updated_children,
        parent_revision=seed_data.get("parent_revision"),
        children_sync_status=seed_data.get("children_sync_status"),
        webhook_event=context.webhook_event,
    )
