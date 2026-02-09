from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from orchestrator.api.routes_admin import router as admin_router
from orchestrator.api.routes_discord import router as discord_router
from orchestrator.api.routes_runs import router as runs_router
from orchestrator.api.routes_webhook import router as webhook_router
from orchestrator.api.routes_webhook_discord import router as webhook_discord_router
from orchestrator.api.routes_webhook_discord_interactions import (
    router as webhook_discord_interactions_router,
)
from orchestrator.api.routes_webhook_github import router as webhook_github_router
from orchestrator.core.config import get_settings
from orchestrator.core.discord_commands_sync import sync_discord_guild_commands
from orchestrator.core.discord_gateway_listener import DiscordGatewayListener
from orchestrator.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]

    app = FastAPI(title="master-builder orchestrator")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(admin_router)
    app.include_router(discord_router)
    app.include_router(runs_router)
    app.include_router(webhook_router)
    app.include_router(webhook_discord_router)
    app.include_router(webhook_discord_interactions_router)
    app.include_router(webhook_github_router)
    gateway_listener = DiscordGatewayListener(settings=settings)

    @app.on_event("startup")
    def _startup_discord_command_sync() -> None:
        # Best-effort: failures are logged by sync_discord_guild_commands and must not block API startup.
        sync_discord_guild_commands(settings=settings)
        gateway_listener.start()

    @app.on_event("shutdown")
    def _shutdown_discord_gateway_listener() -> None:
        gateway_listener.stop()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
