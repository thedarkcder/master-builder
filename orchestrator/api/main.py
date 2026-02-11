from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from orchestrator.api.routes.admin import router as admin_router
from orchestrator.api.routes.admin_auth import router as admin_auth_router
from orchestrator.api.routes.admin_secrets import router as admin_secrets_router
from orchestrator.api.routes.discord import (
    register_discord_command_executor,
    router as discord_router,
)
from orchestrator.api.routes.runs import router as runs_router
from orchestrator.api.routes.webhook import router as webhook_router
from orchestrator.api.routes.webhook_discord import router as webhook_discord_router
from orchestrator.api.routes.webhook_discord_interactions import (
    router as webhook_discord_interactions_router,
)
from orchestrator.api.routes.webhook_github import router as webhook_github_router
from orchestrator.core.config import get_settings
from orchestrator.core.discord.commands_sync import sync_discord_guild_commands
from orchestrator.core.discord.gateway_listener import DiscordGatewayListener
from orchestrator.core.logging import configure_logging
from orchestrator.storage.migrations import run_migrations


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.auto_migrate_on_startup:
            run_migrations()
        register_discord_command_executor()
        # Best-effort: failures are logged by sync_discord_guild_commands and must not block API startup.
        sync_discord_guild_commands(settings=settings)
        gateway_listener.start()
        try:
            yield
        finally:
            gateway_listener.stop()

    app = FastAPI(title="master-builder orchestrator", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(admin_router)
    app.include_router(admin_auth_router)
    app.include_router(admin_secrets_router)
    app.include_router(discord_router)
    app.include_router(runs_router)
    app.include_router(webhook_router)
    app.include_router(webhook_discord_router)
    app.include_router(webhook_discord_interactions_router)
    app.include_router(webhook_github_router)
    register_discord_command_executor()
    gateway_listener = DiscordGatewayListener(settings=settings)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
