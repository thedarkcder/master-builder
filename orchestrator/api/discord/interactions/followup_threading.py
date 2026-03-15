from __future__ import annotations

from datetime import datetime, timezone
import logging

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
    discord_api_client_fn,
    project_ask_thread_channel_ids_for_tenant_fn,
    resolve_project_for_channel_fn,
    ask_thread_message_map_from_config_fn,
    ask_reply_components_fn,
    put_thread_issue_key_fn,
) -> None:
    client = discord_api_client_fn(session=session, settings=settings)
    ask_thread_channel_ids = project_ask_thread_channel_ids_for_tenant_fn(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    normalized_issue_key = normalize_issue_key(issue_key)

    def _persist_tenant_thread_issue_binding(*, thread_channel_id: str) -> None:
        if not normalized_issue_key:
            return
        tenant.discord_config = put_thread_issue_key_fn(
            discord_config=tenant.discord_config,
            channel_id=thread_channel_id,
            issue_key=normalized_issue_key,
        )
        tenant.updated_at = datetime.now(timezone.utc)

    def _persist_thread_issue_binding(*, project, thread_channel_id: str) -> None:
        if project is None or not normalized_issue_key:
            return
        project.discord_config = put_thread_issue_key_fn(
            discord_config=project.discord_config,
            channel_id=thread_channel_id,
            issue_key=normalized_issue_key,
        )
        project.updated_at = datetime.now(timezone.utc)

    if channel_id in ask_thread_channel_ids:
        project = resolve_project_for_channel_fn(session=session, tenant=tenant, channel_id=channel_id)
        _persist_thread_issue_binding(project=project, thread_channel_id=channel_id)
        _persist_tenant_thread_issue_binding(thread_channel_id=channel_id)
        if normalized_issue_key:
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
        _persist_thread_issue_binding(project=project, thread_channel_id=thread_channel_id)
        project.updated_at = datetime.now(timezone.utc)
    elif normalized_issue_key:
        logger.info(
            "discord_ask_thread_issue_binding_skipped tenant_id=%s thread_channel_id=%s issue_key=%s reason=project_not_resolved",
            tenant.tenant_id,
            thread_channel_id,
            normalized_issue_key,
        )
    _persist_tenant_thread_issue_binding(thread_channel_id=thread_channel_id)
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()
    client.post_message(
        channel_id=thread_channel_id,
        content=f"<@{user_id}> Continue here with follow-up questions.",
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
    questions: list[str],
    discord_api_client_fn,
    project_seed_followup_thread_channel_ids_for_tenant_fn,
    resolve_project_for_channel_fn,
) -> None:
    client = discord_api_client_fn(session=session, settings=settings)
    seed_thread_channel_ids = project_seed_followup_thread_channel_ids_for_tenant_fn(
        session=session,
        tenant_id=tenant.tenant_id,
    )
    if channel_id in seed_thread_channel_ids:
        numbered_questions = [f"{idx}. {value}" for idx, value in enumerate(questions, start=1) if value.strip()]
        question_block = "\n".join(numbered_questions) if numbered_questions else "No additional questions."
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

    discord_config = dict(tenant.discord_config or {})
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

    raw_seed_followups = discord_config.get("seed_followups")
    if isinstance(raw_seed_followups, list):
        updated_followups: list[dict] = []
        now_iso = datetime.now(timezone.utc).isoformat()
        for item in raw_seed_followups:
            if not isinstance(item, dict):
                continue
            if str(item.get("request_id") or "").strip() != request_id:
                updated_followups.append(item)
                continue
            raw_channel_ids = item.get("channel_ids")
            channel_ids = (
                [str(value).strip() for value in raw_channel_ids if str(value).strip()]
                if isinstance(raw_channel_ids, list)
                else []
            )
            if channel_id not in channel_ids:
                channel_ids.append(channel_id)
            if thread_channel_id not in channel_ids:
                channel_ids.append(thread_channel_id)
            item["channel_ids"] = channel_ids
            item["updated_at"] = now_iso
            updated_followups.append(item)
        discord_config["seed_followups"] = updated_followups

    tenant.discord_config = discord_config
    tenant.updated_at = datetime.now(timezone.utc)
    session.commit()

    numbered_questions = [f"{idx}. {value}" for idx, value in enumerate(questions, start=1) if value.strip()]
    question_block = "\n".join(numbered_questions) if numbered_questions else "No additional questions."
    client.post_message(
        channel_id=thread_channel_id,
        content=(
            f"<@{user_id}> Continue here with details so I can refine and update the seeded tickets.\n"
            f"{question_block}"
        ),
    )
