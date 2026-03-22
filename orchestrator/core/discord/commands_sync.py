from __future__ import annotations

import logging
from typing import Callable

from orchestrator.core.config import Settings, get_settings
from orchestrator.core.discord.command_sync_status import (
    mark_discord_command_sync_failure,
    mark_discord_command_sync_success,
)
from orchestrator.core.platform_secret_service import (
    PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF,
    PLATFORM_SECRET_DISCORD_GUILD_ID_REF,
    resolve_platform_secret_ref,
)
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
            "description": "Ask a product question and get a structured PM brief",
            "options": [
                {
                    "type": 3,  # STRING
                    "name": "question",
                    "description": "Product or prioritization question",
                    "required": True,
                },
                {
                    "type": 3,  # STRING
                    "name": "action",
                    "description": "Optional PM mode (for example: approve)",
                    "required": False,
                    "choices": [
                        {
                            "name": "Ask",
                            "value": "ask",
                        },
                        {
                            "name": "Approve",
                            "value": "approve",
                        },
                    ],
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
                    "description": "Jira issue key, e.g. MAB-24",
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
                            "name": "Issue seeding (!issues seed)",
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
                }
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
    client_factory: Callable[..., DiscordApiClient] = DiscordApiClient,
    secret_resolver: Callable[..., str | None] = resolve_platform_secret_ref,
) -> bool:
    resolved_settings = settings or get_settings()
    resolved_session_factory = session_factory or create_session_factory()

    bot_token_ref = PLATFORM_SECRET_DISCORD_BOT_TOKEN_REF
    if not bot_token_ref:
        mark_discord_command_sync_failure(
            reason="missing_bot_token",
            bot_token_configured=False,
            guild_id_configured=bool(str(resolved_settings.discord_guild_id or "").strip()),
        )
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
            mark_discord_command_sync_failure(
                reason="missing_bot_token",
                bot_token_configured=False,
                guild_id_configured=bool(str(resolved_settings.discord_guild_id or "").strip()),
            )
            logger.info("discord_command_sync_skipped reason=missing_bot_token secret_ref=%s", bot_token_ref)
            return False

        guild_id = resolved_settings.discord_guild_id.strip()
        if not guild_id:
            guild_id = (
                secret_resolver(
                    session,
                    secret_ref=PLATFORM_SECRET_DISCORD_GUILD_ID_REF,
                    encryption_key=resolved_settings.secrets_encryption_key,
                )
                or ""
            ).strip()
        if not guild_id:
            mark_discord_command_sync_failure(
                reason="missing_guild_id",
                bot_token_configured=True,
                guild_id_configured=False,
            )
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
            mark_discord_command_sync_failure(
                reason="discord_api_error",
                error=str(exc),
                bot_token_configured=True,
                guild_id_configured=True,
                guild_id=guild_id,
            )
            logger.warning("discord_command_sync_failed error=%s", exc)
            return False

    mark_discord_command_sync_success(
        bot_token_configured=True,
        guild_id_configured=True,
        guild_id=guild_id,
        application_id=application_id,
        command_count=len(synced),
    )
    logger.info(
        "discord_command_sync_complete guild_id=%s application_id=%s command_count=%s",
        guild_id,
        application_id,
        len(synced),
    )
    return True
