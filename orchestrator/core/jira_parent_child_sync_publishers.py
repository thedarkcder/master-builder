from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.clarification_projection_service import (
    ClarificationProjectionSpec,
    jira_comment_evidence_id,
    matching_active_jira_clarification_evidence_id,
    upsert_clarification_projection,
)
from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.followup_context_service import FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION, FOLLOWUP_CONTEXT_PM_INTERVIEW, upsert_followup_context
from orchestrator.core.jira_parent_child_sync_shared import (
    extract_created_comment_id,
    normalize_sync_labels,
    pm_interview_jira_followup_request_id,
    pm_interview_jira_reply_scope,
    pm_interview_jira_transport,
    system_comment_marker,
)
from orchestrator.core.jira_links import JiraRemoteLinkSpec
from orchestrator.core.parent_feature_brief_store import resolve_parent_feature_brief, resolve_parent_feature_case
from orchestrator.core.parent_planning_clarification_service import ClarificationPublishEffects
from orchestrator.core.platform_secret_service import PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF, resolve_platform_secret_ref
from orchestrator.core.pm_interview_service import (
    PM_INTERVIEW_PARENT_BRIEF_CHANNEL_ID,
    PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT,
    PM_INTERVIEW_STATUS_QUESTION_PENDING,
    upsert_pm_interview_case,
)
from orchestrator.storage.models import FollowupContext, Project
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError
from orchestrator.tools.atlassian_oauth import JiraIssueDetail

logger = logging.getLogger(__name__)

DISCORD_MESSAGE_CONTENT_LIMIT = 2000


def update_issue_sync_label(
    *,
    oauth,
    issue_detail: JiraIssueDetail,
    target_label: str,
) -> None:
    next_labels = normalize_sync_labels(issue_detail.labels, target_label=target_label)
    current_labels = [str(label or "").strip() for label in issue_detail.labels if str(label or "").strip()]
    if {label.casefold() for label in current_labels} == {label.casefold() for label in next_labels}:
        return
    oauth.client.replace_issue_labels(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=issue_detail.key,
        labels=next_labels,
    )


def _sync_note(*, body: str) -> str:
    return f"{system_comment_marker()} {body}".strip()


def extract_jira_issue_mention_target(*, payload: dict[str, Any]) -> tuple[str | None, str | None]:
    issue = payload.get("issue") if isinstance(payload, dict) else None
    fields = issue.get("fields") if isinstance(issue, dict) and isinstance(issue.get("fields"), dict) else {}
    for field_name in ("reporter", "assignee"):
        actor = fields.get(field_name)
        if not isinstance(actor, dict):
            continue
        account_id = str(actor.get("accountId") or "").strip() or None
        display_name = str(actor.get("displayName") or "").strip() or None
        if account_id:
            return account_id, display_name
    return None, None


def build_jira_question_list_items(*, questions: tuple[ClarificationQuestion, ...]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for question in questions:
        list_item_content: list[dict[str, Any]] = [
            {
                "type": "paragraph",
                "content": [{"type": "text", "text": question.question}],
            }
        ]
        if question.why_it_matters:
            list_item_content.append(
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": f"Why it matters: {question.why_it_matters}"}],
                }
            )
        items.append({"type": "listItem", "content": list_item_content})
    return items


def jira_question_comment_adf(
    *,
    issue_key: str,
    questions: tuple[ClarificationQuestion, ...],
    mention_account_id: str | None,
    mention_display_name: str | None,
    intro_text: str | None = None,
) -> dict[str, Any]:
    intro_content: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": f"{system_comment_marker()} ",
        }
    ]
    if mention_account_id:
        intro_content.append(
            {
                "type": "mention",
                "attrs": {
                    "id": mention_account_id,
                    "text": f"@{mention_display_name or 'reporter'}",
                },
            }
        )
        intro_content.append({"type": "text", "text": " "})
    intro_content.append(
        {
            "type": "text",
            "text": intro_text
            or (
                f"Master Builder needs product clarification on {issue_key} before PM planning can continue. "
                "Please reply on this Jira issue with answers to the questions below."
            ),
        }
    )
    question_items = build_jira_question_list_items(questions=questions)
    content: list[dict[str, Any]] = [
        {"type": "paragraph", "content": intro_content},
        {"type": "orderedList", "content": question_items},
    ]
    return {"type": "doc", "version": 1, "content": content}


def persist_parent_brief_jira_followup(
    *,
    session: Session,
    tenant,
    project_id: str | None,
    parent_issue_key: str,
    questions: tuple[ClarificationQuestion, ...],
    posted_comment_id: str | None,
) -> None:
    parent_case = resolve_parent_feature_case(
        session=session,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
        parent_issue_key=parent_issue_key,
    )
    existing_request_id = str(getattr(parent_case, "request_id", "") or "").strip() or None
    existing_source_kind = str(getattr(parent_case, "source_kind", "") or "").strip()
    pm_request_id = (
        existing_request_id
        if existing_request_id and existing_source_kind != PM_INTERVIEW_SOURCE_KIND_PARENT_BRIEF_SNAPSHOT
        else f"pm-parent-interview:{parent_issue_key}"
    )
    owner_user_id = str(getattr(parent_case, "owner_user_id", "") or "").strip() or None
    root_message_id = str(getattr(parent_case, "root_message_id", "") or "").strip() or posted_comment_id or None
    interview_case = upsert_pm_interview_case(
        session=session,
        tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
        project_id=project_id,
        request_id=pm_request_id,
        source_kind="jira_parent",
        channel_id=str(getattr(parent_case, "channel_id", "") or "").strip() or PM_INTERVIEW_PARENT_BRIEF_CHANNEL_ID,
        thread_channel_id=str(getattr(parent_case, "thread_channel_id", "") or "").strip() or None,
        root_message_id=root_message_id,
        owner_user_id=owner_user_id,
        parent_issue_key=parent_issue_key,
        source_text=str(getattr(parent_case, "source_text", "") or "") or f"Parent issue {parent_issue_key} requires PM clarification.",
        status=PM_INTERVIEW_STATUS_QUESTION_PENDING,
        brief=resolve_parent_feature_brief(
            session=session,
            tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
            parent_issue_key=parent_issue_key,
            include_incomplete=True,
        ),
        notes={
            "source": "jira_parent_brief_normalization",
            "normalization_questions": ClarificationQuestionSet(questions=questions).to_payload(),
        },
    )
    upsert_clarification_projection(
        session=session,
        spec=ClarificationProjectionSpec(
            tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
            project_id=project_id,
            context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
            issue_key=parent_issue_key,
            request_id=pm_interview_jira_followup_request_id(parent_issue_key=parent_issue_key),
            channel_id=parent_issue_key,
            thread_channel_id=None,
            root_message_id=posted_comment_id,
            owner_user_id=owner_user_id or None,
            origin_command="pm",
            transport=pm_interview_jira_transport(),
            reply_scope=pm_interview_jira_reply_scope(),
            questions=questions,
            metadata={
                "parent_issue_key": parent_issue_key,
                "questions": ClarificationQuestionSet(questions=questions).to_payload(),
                "source": "jira_parent_brief_normalization",
                "pm_request_id": str(getattr(interview_case, "request_id", "") or "").strip() or pm_request_id,
                "jira_comment_id": posted_comment_id,
            },
        ),
    )


def post_parent_brief_questions_to_jira(
    *,
    session: Session,
    tenant,
    project_id: str | None,
    issue_key: str,
    payload: dict[str, Any],
    questions: tuple[ClarificationQuestion, ...],
    settings,  # noqa: ANN001
    create_jira_comment_fn,
) -> tuple[dict[str, Any] | None, str | None]:
    mention_account_id, mention_display_name = extract_jira_issue_mention_target(payload=payload)
    comment = jira_question_comment_adf(
        issue_key=issue_key,
        questions=questions,
        mention_account_id=mention_account_id,
        mention_display_name=mention_display_name,
    )
    created_comment, error = create_jira_comment_fn(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        comment=comment,
        settings=settings,
    )
    if error is None:
        persist_parent_brief_jira_followup(
            session=session,
            tenant=tenant,
            project_id=project_id,
            parent_issue_key=issue_key,
            questions=questions,
            posted_comment_id=extract_created_comment_id(created_comment),
        )
    return created_comment, error


def post_engineering_clarification_questions_to_jira(
    *,
    session: Session,
    tenant,
    issue_key: str,
    payload: dict[str, Any],
    questions: tuple[ClarificationQuestion, ...],
    settings,  # noqa: ANN001
    create_jira_comment_fn,
) -> tuple[dict[str, Any] | None, str | None]:
    mention_account_id, mention_display_name = extract_jira_issue_mention_target(payload=payload)
    comment = jira_question_comment_adf(
        issue_key=issue_key,
        questions=questions,
        mention_account_id=mention_account_id,
        mention_display_name=mention_display_name,
        intro_text=(
            f"Engineering child tickets need product clarification on {issue_key} before engineering can resume. "
            "Please reply on this Jira issue with answers to the questions below."
        ),
    )
    return create_jira_comment_fn(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        comment=comment,
        settings=settings,
    )


def post_sync_note(
    *,
    session: Session,
    tenant,
    issue_key: str,
    settings,  # noqa: ANN001
    body: str,
    post_jira_comment_fn,
) -> tuple[bool, str | None]:
    return post_jira_comment_fn(
        session=session,
        tenant=tenant,
        issue_key=issue_key,
        comment=_sync_note(body=body),
        settings=settings,
    )


def engineering_decision_note(
    *,
    question: str,
    owner: str,
    approval_path: str,
    rationale: str,
    status_line: str,
) -> str:
    lines = [
        "Implementation decision record",
        f"- Question: {question.strip()}",
        f"- Decision owner: {owner.strip()}",
        f"- Approval path: {approval_path.strip()}",
        f"- Status: {status_line.strip()}",
    ]
    if rationale.strip():
        lines.append(f"- Rationale: {rationale.strip()}")
    return "\n".join(lines)


def active_issue_followup_contexts(
    *,
    session: Session,
    tenant_id: str,
    issue_key: str,
    context_type: str,
) -> list[FollowupContext]:
    normalized_tenant_id = str(tenant_id or "").strip()
    normalized_issue_key = str(issue_key or "").strip().upper()
    normalized_context_type = str(context_type or "").strip()
    if not normalized_tenant_id or not normalized_issue_key or not normalized_context_type:
        return []
    return session.execute(
        select(FollowupContext)
        .where(
            FollowupContext.tenant_id == normalized_tenant_id,
            FollowupContext.issue_key == normalized_issue_key,
            FollowupContext.context_type == normalized_context_type,
            FollowupContext.status == "active",
        )
        .order_by(FollowupContext.updated_at.desc())
    ).scalars().all()


def mark_issues_sync_blocked(
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
        update_issue_sync_label(
            oauth=oauth,
            issue_detail=detail,
            target_label="sync-blocked",
        )

def upsert_jira_remote_link(
    *,
    oauth,
    issue_key: str,
    spec: JiraRemoteLinkSpec,
) -> dict[str, Any]:
    return oauth.client.upsert_remote_issue_link(
        access_token=oauth.access_token,
        cloud_id=oauth.connection.cloud_id,
        issue_id_or_key=issue_key,
        global_id=spec.global_id,
        relationship=spec.relationship,
        title=spec.title,
        url=spec.url,
    )


def format_parent_brief_questions_for_discord(
    *,
    parent_issue_key: str,
    questions: tuple[object, ...] | list[object],
) -> str:
    question_lines = "\n".join(
        ClarificationQuestionSet.from_values(questions).render_lines(include_reasons=True)
    )
    return (
        f"Decision needed for `{parent_issue_key}` before I can finish backlog planning.\n"
        "Please reply in this thread with the missing product behavior:\n"
        f"{question_lines}"
    ).strip()


def chunk_discord_message_content(
    content: str,
    *,
    limit: int = DISCORD_MESSAGE_CONTENT_LIMIT,
) -> tuple[str, ...]:
    normalized_content = content.strip()
    if not normalized_content:
        raise ValueError("Discord message content cannot be empty")
    if limit < 1:
        raise ValueError("Discord message content limit must be positive")

    chunks: list[str] = []
    current = ""
    for line in normalized_content.splitlines():
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    if not chunks:
        raise ValueError("Discord message content cannot be empty")
    return tuple(chunks)


def post_discord_message_content(
    *,
    client: DiscordApiClient,
    channel_id: str,
    content: str,
) -> list[dict]:
    responses: list[dict] = []
    for chunk in chunk_discord_message_content(content):
        responses.append(client.post_message(channel_id=channel_id, content=chunk))
    return responses


def post_parent_brief_questions_to_discord(
    *,
    session: Session,
    settings,  # noqa: ANN001
    tenant,
    project_id: str | None,
    parent_issue_key: str,
    questions: tuple[object, ...] | list[object],
) -> bool:
    question_set = ClarificationQuestionSet.from_values(questions)
    if not question_set:
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
        raise RuntimeError(
            f"Discord clarification projection requires a configured parent channel for {parent_issue_key}"
        )
    token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not token_ref:
        raise RuntimeError("Discord clarification projection requires PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF")
    bot_token = resolve_platform_secret_ref(
        session,
        secret_ref=token_ref,
        encryption_key=settings.secrets_encryption_key,
    )
    if not bot_token:
        raise RuntimeError(
            f"Discord clarification projection could not resolve bot token for {parent_issue_key}"
        )
    mention_prefix = f"<@{owner_user_id}> " if owner_user_id else ""
    message = f"{mention_prefix}{format_parent_brief_questions_for_discord(parent_issue_key=parent_issue_key, questions=question_set.questions)}".strip()
    try:
        client = DiscordApiClient(bot_token=bot_token)
        posted_root_message_id = None
        thread_channel_id = existing_thread_channel_id
        if thread_channel_id:
            post_discord_message_content(client=client, channel_id=thread_channel_id, content=message)
        else:
            posted = client.post_message(
                channel_id=root_channel_id,
                content=f"Parent feature `{parent_issue_key}` needs PM clarification before backlog planning can continue.",
            )
            posted_root_message_id = str((posted or {}).get("id") or "").strip() or None
            if not posted_root_message_id:
                raise RuntimeError(
                    f"Discord clarification projection did not return a root message id for {parent_issue_key}"
                )
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
            post_discord_message_content(client=client, channel_id=thread_channel_id, content=message)
    except (DiscordApiError, ValueError) as exc:
        logger.warning(
            "jira_parent_brief_question_discord_post_failed tenant_id=%s parent_issue_key=%s channel_id=%s",
            getattr(tenant, "tenant_id", None),
            parent_issue_key,
            root_channel_id,
            exc_info=True,
        )
        raise RuntimeError(
            f"Discord clarification projection failed for {parent_issue_key}: {exc}"
        ) from exc
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
            include_incomplete=True,
        ),
        notes={
            "source": "jira_parent_brief_normalization",
            "normalization_questions": question_set.to_payload(),
        },
    )
    upsert_clarification_projection(
        session=session,
        spec=ClarificationProjectionSpec(
            tenant_id=str(getattr(tenant, "tenant_id", "") or ""),
            project_id=project_id,
            context_type=FOLLOWUP_CONTEXT_PM_INTERVIEW,
            issue_key=parent_issue_key,
            request_id=str(getattr(interview_case, "request_id", "") or "").strip() or None,
            channel_id=root_channel_id,
            thread_channel_id=thread_channel_id if thread_channel_id != root_channel_id else None,
            root_message_id=str(getattr(interview_case, "root_message_id", "") or "").strip() or None,
            owner_user_id=owner_user_id or None,
            origin_command="pm",
            questions=question_set.questions,
            metadata={
                "parent_issue_key": parent_issue_key,
                "questions": question_set.to_payload(),
                "source": "jira_parent_brief_normalization",
            },
        ),
    )
    if project is not None:
        tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    return True


class JiraEngineeringClarificationPublisher:
    def __init__(
        self,
        *,
        session: Session,
        context,
        settings,  # noqa: ANN001
        metadata: dict[str, Any],
        create_jira_comment_fn,
    ) -> None:
        self._session = session
        self._context = context
        self._settings = settings
        self._metadata = dict(metadata)
        self._create_jira_comment_fn = create_jira_comment_fn

    def active_clarification_effects(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> ClarificationPublishEffects | None:
        jira_comment_id = matching_active_jira_clarification_evidence_id(
            session=self._session,
            tenant_id=self._context.tenant_id,
            issue_key=issue_key,
            context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
            questions=questions,
        )
        if jira_comment_id is None:
            return None
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=False,
            discord_followup_created=False,
            jira_comment_id=jira_comment_id,
        )

    def publish_clarification(
        self,
        *,
        issue_key: str,
        questions: tuple[ClarificationQuestion, ...],
    ) -> ClarificationPublishEffects:
        projection = upsert_clarification_projection(
            session=self._session,
            spec=ClarificationProjectionSpec(
                tenant_id=self._context.tenant_id,
                project_id=self._context.project_id,
                context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
                issue_key=issue_key,
                request_id=f"engineering-clarification:{issue_key}",
                origin_command="clarify",
                questions=questions,
                metadata=self._metadata,
            ),
        )
        created_comment = None
        error = None
        jira_comment_id = jira_comment_evidence_id(projection.followup_context)
        if not jira_comment_id:
            created_comment, error = post_engineering_clarification_questions_to_jira(
                session=self._session,
                tenant=self._context.tenant,
                issue_key=issue_key,
                payload=dict(self._context.payload or {}),
                questions=questions,
                settings=self._settings,
                create_jira_comment_fn=self._create_jira_comment_fn,
            )
            if error is None and created_comment is not None:
                jira_comment_id = extract_created_comment_id(created_comment)
                if not jira_comment_id:
                    raise RuntimeError(f"Jira engineering clarification projection for {issue_key} did not return a comment id")
                metadata = dict(projection.metadata)
                metadata["jira_comment_id"] = jira_comment_id
                upsert_followup_context(
                    session=self._session,
                    tenant_id=self._context.tenant_id,
                    project_id=self._context.project_id,
                    context_type=FOLLOWUP_CONTEXT_ENGINEERING_CLARIFICATION,
                    origin_command="clarify",
                    issue_key=issue_key,
                    request_id=f"engineering-clarification:{issue_key}",
                    metadata=metadata,
                )
        if not jira_comment_id:
            message = error or f"Jira engineering clarification projection did not create a comment for {issue_key}"
            raise RuntimeError(message)
        jira_comment_created = error is None and created_comment is not None
        return ClarificationPublishEffects(
            state_recorded=True,
            jira_comment_created=jira_comment_created,
            discord_followup_created=False,
            jira_comment_id=jira_comment_id,
        )
