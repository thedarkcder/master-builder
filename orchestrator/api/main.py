from fastapi import FastAPI

from orchestrator.api.routes_admin import router as admin_router
from orchestrator.api.routes_webhook import router as webhook_router
from orchestrator.core.config import get_settings
from orchestrator.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(title="master-builder orchestrator")
    app.include_router(admin_router)
    app.include_router(webhook_router)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
