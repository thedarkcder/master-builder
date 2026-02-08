from __future__ import annotations

import logging
from typing import Callable

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.secret_manager import resolve_secret_ref
from orchestrator.storage.db import create_session_factory
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
                    "description": "Jira issue key, e.g. MAB-24",
                    "required": True,
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
                }
            ],
        },
        {
            "name": "issues",
            "description": "Seed Jira issues from markdown spec",
            "options": [
                {
                    "type": 1,  # SUB_COMMAND
                    "name": "seed",
                    "description": "Split markdown into Jira task issues",
                    "options": [
                        {
                            "type": 3,  # STRING
                            "name": "spec",
                            "description": "Markdown spec to split into tasks",
                            "required": True,
                        }
                    ],
                }
            ],
        },
    ]


def sync_discord_guild_commands(
    *,
    settings: Settings | None = None,
    session_factory=None,  # noqa: ANN001
    client_factory: Callable[..., DiscordApiClient] = DiscordApiClient,
    secret_resolver: Callable[..., str | None] = resolve_secret_ref,
) -> bool:
    resolved_settings = settings or get_settings()
    resolved_session_factory = session_factory or create_session_factory()

    bot_token_ref = resolved_settings.discord_bot_token_secret_ref.strip()
    if not bot_token_ref:
        logger.info("discord_command_sync_skipped reason=missing_bot_token_ref")
        return False

    with resolved_session_factory() as session:
        bot_token = (
            secret_resolver(
                session,
                secret_ref=bot_token_ref,
                encryption_key=resolved_settings.secrets_encryption_key,
            )
            or ""
        ).strip()
        if not bot_token:
            logger.info("discord_command_sync_skipped reason=missing_bot_token secret_ref=%s", bot_token_ref)
            return False

        guild_id = resolved_settings.discord_guild_id.strip()
        if not guild_id:
            guild_ref = resolved_settings.discord_guild_id_secret_ref.strip()
            if guild_ref:
                guild_id = (
                    secret_resolver(
                        session,
                        secret_ref=guild_ref,
                        encryption_key=resolved_settings.secrets_encryption_key,
                    )
                    or ""
                ).strip()
        if not guild_id:
            logger.info("discord_command_sync_skipped reason=missing_guild_id")
            return False

        try:
            client = client_factory(bot_token=bot_token)
            application_id = client.get_application_id()
            commands = build_discord_guild_commands()
            synced = client.overwrite_guild_commands(
                application_id=application_id,
                guild_id=guild_id,
                commands=commands,
            )
        except (DiscordApiError, ValueError) as exc:
            logger.warning("discord_command_sync_failed error=%s", exc)
            return False

    logger.info(
        "discord_command_sync_complete guild_id=%s application_id=%s command_count=%s",
        guild_id,
        application_id,
        len(synced),
    )
    return True
