from __future__ import annotations

from datetime import datetime, timezone
import logging

from orchestrator.core.clarification_questions import ClarificationQuestion, ClarificationQuestionSet
from orchestrator.core.followup_context_service import upsert_followup_context
from orchestrator.core.discord.thread_context import normalize_issue_key
from orchestrator.tools.discord_api import DiscordApiError

logger = logging.getLogger(__name__)


def send_discord_thread_followup(
    *,
    session,
    settings,
    tenant,
    channel_id: str,
    reply_to_message_id: str,
    content: str,
    components: list[dict] | None = None,
    discord_api_client_fn,
    project_ask_thread_channel_ids_for_tenant_fn,
    project_seed_followup_thread_channel_ids_for_tenant_fn,
    resolve_project_for_channel_fn,
    ask_thread_message_map_from_config_fn,
    resolve_thread_id_by_message_suffix_fn,
) -> None:
    client = discord_api_client_fn(session=session, settings=settings)
    ask_thread_ids = project_ask_thread_channel_ids_for_tenant_fn(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    seed_thread_ids = project_seed_followup_thread_channel_ids_for_tenant_fn(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    known_thread_ids = ask_thread_ids | seed_thread_ids
    if channel_id in known_thread_ids:
        client.post_message(channel_id=channel_id, content=content, components=components)
        return
    project = resolve_project_for_channel_fn(session=session, tenant=tenant, channel_id=channel_id)
    if project is not None:
        project_discord_config = dict(project.discord_config or {})
        ask_message_map = ask_thread_message_map_from_config_fn(project_discord_config)
        mapped_thread_id = ask_message_map.get(reply_to_message_id)
        if mapped_thread_id:
            try:
                client.post_message(channel_id=mapped_thread_id, content=content, components=components)
                return
            except DiscordApiError as exc:
                logger.exception(
                    "discord_thread_mapped_followup_failed tenant_id=%s channel_id=%s mapped_thread_id=%s reply_to_message_id=%s error=%s",
                    tenant.tenant_id,
                    channel_id,
                    mapped_thread_id,
                    reply_to_message_id,
                    exc,
                )

    thread_name = f"{tenant.tenant_id}-ask-{reply_to_message_id[-6:]}".replace(" ", "-")
    try:
        thread_channel_id = client.ensure_thread_for_message(
            channel_id=channel_id,
            message_id=reply_to_message_id,
            thread_name=thread_name[:100],
        )
        if project is not None:
            project_discord_config = dict(project.discord_config or {})
            raw_thread_ids = project_discord_config.get("ask_thread_channel_ids")
            thread_ids = (
                [str(value).strip() for value in raw_thread_ids if str(value).strip()]
                if isinstance(raw_thread_ids, list)
                else []
            )
            if thread_channel_id not in thread_ids:
                thread_ids.append(thread_channel_id)
            ask_message_map = ask_thread_message_map_from_config_fn(project_discord_config)
            ask_message_map[reply_to_message_id] = thread_channel_id
            project_discord_config["ask_thread_channel_ids"] = thread_ids[-200:]
            project_discord_config["ask_thread_by_message_id"] = dict(list(ask_message_map.items())[-500:])
            project.discord_config = project_discord_config
            project.updated_at = datetime.now(timezone.utc)
            tenant.updated_at = datetime.now(timezone.utc)
            session.commit()
        client.post_message(channel_id=thread_channel_id, content=content, components=components)
    except DiscordApiError as exc:
        logger.exception(
            "discord_thread_followup_failed tenant_id=%s channel_id=%s reply_to_message_id=%s error=%s",
            tenant.tenant_id,
            channel_id,
            reply_to_message_id,
            exc,
        )
        if project is not None:
            project_discord_config = dict(project.discord_config or {})
            raw_thread_ids = project_discord_config.get("ask_thread_channel_ids")
            thread_ids = (
                [str(value).strip() for value in raw_thread_ids if str(value).strip()]
                if isinstance(raw_thread_ids, list)
                else []
            )
            matched_thread_id = resolve_thread_id_by_message_suffix_fn(
                client=client,
                thread_ids=thread_ids,
                message_id=reply_to_message_id,
            )
            if matched_thread_id:
                ask_message_map = ask_thread_message_map_from_config_fn(project_discord_config)
                ask_message_map[reply_to_message_id] = matched_thread_id
                project_discord_config["ask_thread_by_message_id"] = dict(list(ask_message_map.items())[-500:])
                project.discord_config = project_discord_config
                project.updated_at = datetime.now(timezone.utc)
                tenant.updated_at = datetime.now(timezone.utc)
                session.commit()
                client.post_message(channel_id=matched_thread_id, content=content, components=components)
                return
        client.post_message(channel_id=channel_id, content=content, components=components)



def send_discord_ask_response_with_thread(
    *,
    session,
    settings,
    tenant,
    channel_id: str,
    user_id: str,
    content: str,
    components: list[dict] | None = None,
    issue_key: str | None = None,
    followup_context_type: str = "ask_thread",
    request_id: str | None = None,
    discord_api_client_fn,
    project_ask_thread_channel_ids_for_tenant_fn,
    resolve_project_for_channel_fn,
    ask_thread_message_map_from_config_fn,
    ask_reply_components_fn,
) -> None:
    client = discord_api_client_fn(session=session, settings=settings)
    ask_thread_channel_ids = project_ask_thread_channel_ids_for_tenant_fn(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    normalized_issue_key = normalize_issue_key(issue_key)

    def _persist_followup_context(*, project, root_channel_id: str, target_thread_channel_id: str, root_message_id: str | None) -> None:
        normalized_followup_context_type = str(followup_context_type or "ask_thread").strip() or "ask_thread"
        if normalized_followup_context_type == "pm_interview":
            origin_command = "pm"
        elif normalized_followup_context_type == "ask_thread":
            origin_command = "ask"
        else:
            origin_command = normalized_followup_context_type
        upsert_followup_context(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=str(getattr(project, "project_id", "") or "").strip() or None,
            context_type=normalized_followup_context_type,
            channel_id=root_channel_id,
            thread_channel_id=target_thread_channel_id,
            root_message_id=root_message_id,
            owner_user_id=user_id,
            origin_command=origin_command,
            issue_key=normalized_issue_key,
            request_id=str(request_id or "").strip() or None,
        )

    if channel_id in ask_thread_channel_ids:
        project = resolve_project_for_channel_fn(session=session, tenant=tenant, channel_id=channel_id)
        _persist_followup_context(
            project=project,
            root_channel_id=channel_id,
            target_thread_channel_id=channel_id,
            root_message_id=None,
        )
        session.commit()
        client.post_message(
            channel_id=channel_id,
            content=content,
            components=components or ask_reply_components_fn(),
        )
        return

    posted = client.post_message(
        channel_id=channel_id,
        content=content,
        components=components or ask_reply_components_fn(),
    )
    posted_message_id = str(posted.get("id") or "").strip()
    if not posted_message_id:
        raise RuntimeError("Discord message post succeeded but response did not include message ID")
    thread_name = f"{tenant.tenant_id}-ask-{posted_message_id[-6:]}".replace(" ", "-")
    thread_channel_id = client.create_thread_from_message(
        channel_id=channel_id,
        message_id=posted_message_id,
        name=thread_name[:100],
    )
    project = resolve_project_for_channel_fn(session=session, tenant=tenant, channel_id=channel_id)
    if project is not None:
        project_discord_config = dict(project.discord_config or {})
        raw_thread_ids = project_discord_config.get("ask_thread_channel_ids")
        thread_ids = (
            [str(value).strip() for value in raw_thread_ids if str(value).strip()]
            if isinstance(raw_thread_ids, list)
            else []
        )
        if thread_channel_id not in thread_ids:
            thread_ids.append(thread_channel_id)
        ask_message_map = ask_thread_message_map_from_config_fn(project_discord_config)
        ask_message_map[posted_message_id] = thread_channel_id
        project_discord_config["ask_thread_channel_ids"] = thread_ids[-200:]
        project_discord_config["ask_thread_by_message_id"] = dict(list(ask_message_map.items())[-500:])
        project.discord_config = project_discord_config
        project.updated_at = datetime.now(timezone.utc)
    elif normalized_issue_key and followup_context_type == "decision_gate":
        logger.info(
            "discord_followup_context_project_unresolved tenant_id=%s thread_channel_id=%s issue_key=%s context_type=%s",
            tenant.tenant_id,
            thread_channel_id,
            normalized_issue_key,
            followup_context_type,
        )
    _persist_followup_context(
        project=project,
        root_channel_id=channel_id,
        target_thread_channel_id=thread_channel_id,
        root_message_id=posted_message_id,
    )
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    intro_message = "Continue here with follow-up questions."
    if str(followup_context_type or "").strip().lower() == "pm_interview":
        intro_message = "Continue here with PM follow-up questions."
    client.post_message(
        channel_id=thread_channel_id,
        content=f"<@{user_id}> {intro_message}",
    )



def send_discord_seed_followup_with_thread(
    *,
    session,
    settings,
    tenant,
    channel_id: str,
    user_id: str,
    content: str,
    request_id: str,
    questions: tuple[ClarificationQuestion, ...],
    discord_api_client_fn,
    project_seed_followup_thread_channel_ids_for_tenant_fn,
    resolve_project_for_channel_fn,
) -> None:
    client = discord_api_client_fn(session=session, settings=settings)
    seed_thread_channel_ids = project_seed_followup_thread_channel_ids_for_tenant_fn(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    question_set = ClarificationQuestionSet.from_values(questions)
    if channel_id in seed_thread_channel_ids:
        project = resolve_project_for_channel_fn(session=session, tenant=tenant, channel_id=channel_id)
        upsert_followup_context(
            session=session,
            tenant_id=tenant.tenant_id,
            project_id=str(getattr(project, "project_id", "") or "").strip() or None,
            context_type="seed_followup",
            channel_id=channel_id,
            thread_channel_id=channel_id,
            owner_user_id=user_id,
            origin_command="issues",
            request_id=str(request_id or "").strip() or None,
            metadata={
                "request_id": str(request_id or "").strip() or None,
                "questions": question_set.to_payload(),
                "channel_ids": [channel_id],
            },
        )
        session.commit()
        question_block = "\n".join(question_set.render_lines(numbered=True)) if question_set else "No additional questions."
        client.post_message(
            channel_id=channel_id,
            content=(
                f"{content}\n\n"
                f"<@{user_id}> Continue here with details so I can refine and update the seeded tickets.\n"
                f"{question_block}"
            ),
        )
        return

    posted = client.post_message(channel_id=channel_id, content=content)
    posted_message_id = str(posted.get("id") or "").strip()
    if not posted_message_id:
        raise RuntimeError("Discord message post succeeded but response did not include message ID")

    thread_name = f"{tenant.tenant_id}-issues-{posted_message_id[-6:]}".replace(" ", "-")
    thread_channel_id = client.create_thread_from_message(
        channel_id=channel_id,
        message_id=posted_message_id,
        name=thread_name[:100],
    )

    project = resolve_project_for_channel_fn(session=session, tenant=tenant, channel_id=channel_id)
    if project is not None:
        project_discord_config = dict(project.discord_config or {})
        raw_seed_thread_ids = project_discord_config.get("seed_followup_thread_channel_ids")
        seed_thread_ids = (
            [str(value).strip() for value in raw_seed_thread_ids if str(value).strip()]
            if isinstance(raw_seed_thread_ids, list)
            else []
        )
        if thread_channel_id not in seed_thread_ids:
            seed_thread_ids.append(thread_channel_id)
        project_discord_config["seed_followup_thread_channel_ids"] = seed_thread_ids[-200:]
        project.discord_config = project_discord_config
        project.updated_at = datetime.now(timezone.utc)
    upsert_followup_context(
        session=session,
        tenant_id=tenant.tenant_id,
        project_id=str(getattr(project, "project_id", "") or "").strip() or None,
        context_type="seed_followup",
        channel_id=channel_id,
        thread_channel_id=thread_channel_id,
        root_message_id=posted_message_id,
        owner_user_id=user_id,
        origin_command="issues",
        request_id=str(request_id or "").strip() or None,
        metadata={
            "request_id": str(request_id or "").strip() or None,
            "questions": question_set.to_payload(),
            "channel_ids": [channel_id, thread_channel_id],
        },
    )
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    question_block = "\n".join(question_set.render_lines(numbered=True)) if question_set else "No additional questions."
    client.post_message(
        channel_id=thread_channel_id,
        content=(
            f"<@{user_id}> Continue here with details so I can refine and update the seeded tickets.\n"
            f"{question_block}"
        ),
    )
