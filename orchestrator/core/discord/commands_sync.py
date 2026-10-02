from __future__ import annotations

import logging
from typing import Callable

from sqlalchemy import select
from sqlalchemy import text
from sqlalchemy.orm import Session

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.discord.command_sync_status import (
    discord_command_sync_service_instance_id,
    get_discord_command_sync_status,
    mark_discord_command_sync_failure,
    mark_discord_command_sync_success,
)
from orchestrator.core.platform.secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    resolve_platform_secret_ref,
)
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Tenant
from orchestrator.storage.run_queue_events import is_postgres_database_url
from orchestrator.tools.discord_api import DiscordApiClient, DiscordApiError

logger = logging.getLogger(__name__)


def build_discord_guild_commands() -> list[dict]:
    return [
        {
            "name": "help",
            "description": "Show supported commands",
        },
        {
            "name": "status",
            "description": "Show tenant status, queue depth, and active runs",
        },
        {
            "name": "runs",
            "description": "List recent runs",
            "options": [
                {
                    "type": 4,  # INTEGER
                    "name": "limit",
                    "description": "How many runs to return (1-50)",
                    "required": False,
                }
            ],
        },
        {
            "name": "run",
            "description": "Queue a run for an issue key",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Jira issue key, e.g. EXAMPLE-24",
                    "required": True,
                    "autocomplete": True,
                }
            ],
        },
        {
            "name": "cancel",
            "description": "Cancel a run by ID",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "run_id",
                    "description": "Run ID to cancel",
                    "required": True,
                }
            ],
        },
        {
            "name": "retry",
            "description": "Retry using latest run for issue key or an explicit run ID",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "target",
                    "description": "Issue key or run ID",
                    "required": True,
                    "autocomplete": True,
                }
            ],
        },
        {
            "name": "policy",
            "description": "Show policy guidance",
        },
        {
            "name": "link",
            "description": "Show Jira and latest PR links for an issue key",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Jira issue key",
                    "required": True,
                    "autocomplete": True,
                }
            ],
        },
        {
            "name": "ask",
            "description": "Ask a question about board status/issues",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "question",
                    "description": "Question about board status, blockers, or priorities",
                    "required": True,
                },
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Optional issue key to scope the answer",
                    "required": False,
                    "autocomplete": True,
                },
            ],
        },
        {
            "name": "pm",
            "description": "Create or refine one PM-owned product feature issue",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "question",
                    "description": "Product request or PM clarification",
                    "required": True,
                },
            ],
        },
        {
            "name": "architect",
            "description": "Ask the architect for system design guidance",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "question",
                    "description": "Architecture question",
                    "required": True,
                },
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Optional issue key to scope the answer",
                    "required": False,
                    "autocomplete": True,
                },
            ],
        },
        {
            "name": "engineer",
            "description": "Ask the engineer for implementation guidance",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "question",
                    "description": "Engineering question",
                    "required": True,
                },
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Optional issue key to scope the answer",
                    "required": False,
                    "autocomplete": True,
                },
            ],
        },
        {
            "name": "tester",
            "description": "Ask the tester for QA and validation guidance",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "question",
                    "description": "Testing question",
                    "required": True,
                },
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Optional issue key to scope the answer",
                    "required": False,
                    "autocomplete": True,
                },
            ],
        },
        {
            "name": "security",
            "description": "Ask the security reviewer for risk guidance",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "question",
                    "description": "Security question",
                    "required": True,
                },
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Optional issue key to scope the answer",
                    "required": False,
                    "autocomplete": True,
                },
            ],
        },
        {
            "name": "reviewer",
            "description": "Ask the reviewer for change-review guidance",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "question",
                    "description": "Review question",
                    "required": True,
                },
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Optional issue key to scope the answer",
                    "required": False,
                    "autocomplete": True,
                },
            ],
        },
        {
            "name": "gap",
            "description": "Compare Jira acceptance criteria vs current code signals",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Jira issue key, e.g. EXAMPLE-24",
                    "required": True,
                    "autocomplete": True,
                }
            ],
        },
        {
            "name": "bug",
            "description": "Create a Jira bug from Discord context",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "summary",
                    "description": "Short bug summary",
                    "required": True,
                },
                {
                    "type": 3,  # STRING
                    "name": "details",
                    "description": "Repro/context details",
                    "required": False,
                },
                {
                    "type": 3,  # STRING
                    "name": "issue_key",
                    "description": "Optional related Jira issue key",
                    "required": False,
                    "autocomplete": True,
                },
                {
                    "type": 11,  # ATTACHMENT
                    "name": "attachment_1",
                    "description": "Optional screenshot/log attachment",
                    "required": False,
                },
                {
                    "type": 11,  # ATTACHMENT
                    "name": "attachment_2",
                    "description": "Optional screenshot/log attachment",
                    "required": False,
                },
                {
                    "type": 11,  # ATTACHMENT
                    "name": "attachment_3",
                    "description": "Optional screenshot/log attachment",
                    "required": False,
                },
            ],
        },
        {
            "name": "issues",
            "description": "Batch-create or refine PM parent feature issues",
            "options": [
                {
                    "type": 1,  # SUB_COMMAND
                    "name": "seed",
                    "description": "Split markdown into multiple PM parent issues",
                    "options": [
                        {
                            "type": 3,  # STRING
                            "name": "spec",
                            "description": "Markdown batch brief to turn into PM parent issues",
                            "required": True,
                        }
                    ],
                },
                {
                    "type": 1,  # SUB_COMMAND
                    "name": "followup",
                    "description": "Answer outstanding PM clarification questions for a batch",
                    "options": [
                        {
                            "type": 3,  # STRING
                            "name": "answers",
                            "description": "Follow-up answers for the pending PM batch",
                            "required": True,
                        }
                    ],
                },
            ],
        },
        {
            "name": "request",
            "description": "Request allowlist access for a permission set",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "permission",
                    "description": "Permission set to request",
                    "required": True,
                    "choices": [
                        {
                            "name": "Run controls (!run, !cancel, !retry)",
                            "value": "run_controls",
                        },
                        {
                            "name": "PM batch seeding (!issues seed)",
                            "value": "seed_issues",
                        },
                        {
                            "name": "All sensitive commands",
                            "value": "all_sensitive",
                        },
                    ],
                },
                {
                    "type": 3,  # STRING
                    "name": "reason",
                    "description": "Optional reason for access request",
                    "required": False,
                },
            ],
        },
        {
            "name": "reply",
            "type": 3,  # MESSAGE
        },
    ]


def sync_discord_guild_commands(
    *,
    settings: Settings | None = None,
    session_factory=None,  # noqa: ANN001
    session: Session | None = None,
    client_factory: Callable[..., DiscordApiClient] = DiscordApiClient,
    secret_resolver: Callable[..., str | None] = resolve_platform_secret_ref,
) -> bool:
    resolved_settings = settings or get_settings()
    resolved_session_factory = session_factory or create_session_factory()
    service_instance_id = discord_command_sync_service_instance_id()

    bot_token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    owns_session = session is None
    sync_session = session if session is not None else resolved_session_factory()
    lock_acquired = False
    try:
        lock_acquired = _try_acquire_command_sync_lock(
            session=sync_session, settings=resolved_settings
        )
        if not lock_acquired:
            logger.info("discord_command_sync_skipped reason=lock_busy")
            current = get_discord_command_sync_status(session=sync_session)
            return bool(current.synced and current.healthy)

        guild_ids = _configured_tenant_guild_ids(session=sync_session)

        if not bot_token_ref:
            mark_discord_command_sync_failure(
                session=sync_session,
                reason="missing_bot_token",
                bot_token_configured=False,
                guild_id_configured=bool(guild_ids),
                service_instance_id=service_instance_id,
            )
            sync_session.commit()
            logger.info("discord_command_sync_skipped reason=missing_bot_token_ref")
            return False

        bot_token = (
            secret_resolver(
                sync_session,
                secret_ref=bot_token_ref,
                encryption_key=resolved_settings.secrets_encryption_key,
            )
            or ""
        ).strip()
        if not bot_token:
            mark_discord_command_sync_failure(
                session=sync_session,
                reason="missing_bot_token",
                bot_token_configured=False,
                guild_id_configured=bool(guild_ids),
                service_instance_id=service_instance_id,
            )
            sync_session.commit()
            logger.info(
                "discord_command_sync_skipped reason=missing_bot_token secret_ref=%s",
                bot_token_ref,
            )
            return False

        if not guild_ids:
            mark_discord_command_sync_failure(
                session=sync_session,
                reason="missing_guild_id",
                bot_token_configured=True,
                guild_id_configured=False,
                service_instance_id=service_instance_id,
            )
            sync_session.commit()
            logger.info("discord_command_sync_skipped reason=missing_guild_id")
            return False

        current_guild_id = guild_ids[0]
        try:
            client = client_factory(bot_token=bot_token)
            application_id = client.get_application_id()
            commands = build_discord_guild_commands()
            synced: list[dict] = []
            for guild_id in guild_ids:
                current_guild_id = guild_id
                synced = client.overwrite_guild_commands(
                    application_id=application_id,
                    guild_id=guild_id,
                    commands=commands,
                )
        except (DiscordApiError, ValueError) as exc:
            mark_discord_command_sync_failure(
                session=sync_session,
                reason="discord_api_error",
                error=str(exc),
                bot_token_configured=True,
                guild_id_configured=True,
                guild_id=current_guild_id,
                service_instance_id=service_instance_id,
            )
            sync_session.commit()
            logger.warning("discord_command_sync_failed error=%s", exc)
            return False

        mark_discord_command_sync_success(
            session=sync_session,
            bot_token_configured=True,
            guild_id_configured=True,
            guild_id=guild_ids[0],
            application_id=application_id,
            command_count=len(synced),
            service_instance_id=service_instance_id,
        )
        sync_session.commit()
        logger.info(
            "discord_command_sync_complete guild_ids=%s application_id=%s command_count=%s",
            ",".join(guild_ids),
            application_id,
            len(synced),
        )
        return True
    finally:
        if lock_acquired:
            _release_command_sync_lock(session=sync_session, settings=resolved_settings)
        if owns_session:
            sync_session.close()


def _configured_tenant_guild_ids(*, session: Session) -> list[str]:
    guild_ids: list[str] = []
    tenants = session.execute(
        select(Tenant)
        .where(Tenant.is_enabled.is_(True))
        .order_by(Tenant.tenant_id.asc())
    ).scalars()
    for tenant in tenants:
        discord_config = getattr(tenant, "discord_config", None) or {}
        guild_id = str(discord_config.get("guild_id") or "").strip()
        if guild_id and guild_id not in guild_ids:
            guild_ids.append(guild_id)
    return guild_ids


def _try_acquire_command_sync_lock(*, session: Session, settings: Settings) -> bool:
    if not is_postgres_database_url(settings.database_url):
        return True
    lock_key = int(getattr(settings, "discord_command_sync_lock_key", 947102033130))
    row = session.execute(
        text("SELECT pg_try_advisory_lock(:lock_key)"), {"lock_key": lock_key}
    ).scalar()
    return bool(row is True)


def _release_command_sync_lock(*, session: Session, settings: Settings) -> None:
    if not is_postgres_database_url(settings.database_url):
        return
    lock_key = int(getattr(settings, "discord_command_sync_lock_key", 947102033130))
    try:
        session.execute(
            text("SELECT pg_advisory_unlock(:lock_key)"), {"lock_key": lock_key}
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            "discord_command_sync_lock_release_failed lock_key=%s", lock_key
        )
