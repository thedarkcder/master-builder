from contextlib import asynccontextmanager
import logging
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
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
from orchestrator.core.error_observability import emit_hard_error
from orchestrator.core.logging import configure_logging
from orchestrator.core.sentry import initialize_sentry
from orchestrator.storage.migrations import run_migrations

logger = logging.getLogger(__name__)
request_logger = logging.getLogger("master_builder.request")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)
    initialize_sentry(settings=settings)
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

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        except HTTPException as exc:
            status_code = exc.status_code
            raise
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            client_ip = request.client.host if request.client is not None else "-"
            message = (
                f"{client_ip} {request.method} {request.url.path} "
                f"Status: {status_code} Time: {duration_ms}ms"
            )
            request_logger.info(message)
            print(message, flush=True)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        error_ref = uuid4().hex[:8]
        logger.exception(
            "api_unhandled_exception method=%s path=%s error_ref=%s error=%s",
            request.method,
            request.url.path,
            error_ref,
            exc,
        )
        emit_hard_error(
            event="api_unhandled_exception",
            error_ref=error_ref,
            exc=exc,
            context={
                "method": request.method,
                "path": request.url.path,
            },
        )
        return JSONResponse(
            status_code=500,
            content={"detail": f"Internal server error. Ref: {error_ref}"},
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        if exc.status_code >= 500:
            logger.error(
                "api_http_exception_5xx method=%s path=%s status=%s detail=%s",
                request.method,
                request.url.path,
                exc.status_code,
                exc.detail,
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

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
