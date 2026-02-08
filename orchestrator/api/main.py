from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from orchestrator.api.routes_admin import router as admin_router
from orchestrator.api.routes_discord import router as discord_router
from orchestrator.api.routes_runs import router as runs_router
from orchestrator.api.routes_webhook import router as webhook_router
from orchestrator.core.config import get_settings
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

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
