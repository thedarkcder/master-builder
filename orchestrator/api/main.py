from contextlib import asynccontextmanager
import logging
import time
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.responses import PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware

from orchestrator.api.discord.ingress.executor import register_discord_command_executor
from orchestrator.api.routes.admin_auth import router as admin_auth_router
from orchestrator.api.routes.admin_agent_runtimes import router as admin_agent_runtimes_router
from orchestrator.api.routes.admin_codex import router as admin_codex_router
from orchestrator.api.routes.admin_discord_commands import (
    router as admin_discord_commands_router,
)
from orchestrator.api.routes.admin_discord_allowlist import (
    router as admin_discord_allowlist_router,
)
from orchestrator.api.routes.admin_discord_install import (
    router as admin_discord_install_router,
)
from orchestrator.api.routes.admin_github import router as admin_github_router
from orchestrator.api.routes.admin_jira import router as admin_jira_router
from orchestrator.api.routes.admin_knowledge import router as admin_knowledge_router
from orchestrator.api.routes.admin_observability import router as admin_observability_router
from orchestrator.api.routes.admin_ready import router as admin_ready_router
from orchestrator.api.routes.admin_release import router as admin_release_router
from orchestrator.api.routes.admin_runs import router as admin_runs_router
from orchestrator.api.routes.admin_secrets import router as admin_secrets_router
from orchestrator.api.routes.admin_tenants import router as admin_tenants_router
from orchestrator.api.routes.admin_tokens import router as admin_tokens_router
from orchestrator.api.routes.app_auth import router as app_auth_router
from orchestrator.api.routes.discord import router as discord_router
from orchestrator.api.routes.runs import router as runs_router
from orchestrator.api.routes.webhook import router as webhook_router
from orchestrator.api.routes.webhook_discord import router as webhook_discord_router
from orchestrator.api.routes.webhook_discord_interactions import (
    router as webhook_discord_interactions_router,
)
from orchestrator.api.routes.webhook_github import router as webhook_github_router
from orchestrator.core.config import get_settings
from orchestrator.core.discord.commands_sync import sync_discord_guild_commands
from orchestrator.core.error_observability import emit_hard_error
from orchestrator.core.log_event_bus import initialize_run_streaming, shutdown_run_streaming
from orchestrator.core.logging import configure_logging
from orchestrator.core.platform_metrics import platform_metrics
from orchestrator.core.observability import reset_log_context, set_log_context
from orchestrator.core.sentry import initialize_sentry
from orchestrator.core.workflow.execution_snapshot_startup import (
    run_execution_snapshot_startup_bootstrap,
)
from orchestrator.storage.database_support import ensure_postgres_database_url
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.migrations import run_migrations

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    ensure_postgres_database_url(
        database_url=settings.database_url,
        context="API runtime",
        allow_sqlite_for_tests=bool(getattr(settings, "allow_sqlite_for_tests", False)),
    )
    configure_logging(
        settings.log_level,
        environment=settings.sentry_environment,
        platform_version=settings.sentry_release or "dev-local",
        default_agent_id="api",
    )
    initialize_sentry(settings=settings)
    cors_origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    admin_ui_origin = str(settings.admin_ui_base_url or "").strip().rstrip("/")
    if admin_ui_origin and admin_ui_origin not in cors_origins:
        cors_origins.append(admin_ui_origin)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.auto_migrate_on_startup:
            run_migrations()
        if bool(getattr(settings, "execution_snapshot_startup_bootstrap_enabled", True)):
            run_execution_snapshot_startup_bootstrap(
                session_factory=create_session_factory(),
                database_url=settings.database_url,
                actor="api",
            )
        # Best-effort: failures are logged by sync_discord_guild_commands and must not block API startup.
        sync_discord_guild_commands(settings=settings)
        initialize_run_streaming()
        try:
            yield
        finally:
            shutdown_run_streaming()

    app = FastAPI(title="master-builder orchestrator", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_origin_regex=str(getattr(settings, "cors_origin_regex", "") or "").strip() or None,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def log_requests(request: Request, call_next):
        start = time.perf_counter()
        correlation_id = request.headers.get("X-Request-Id") or uuid4().hex
        context_tokens = set_log_context(correlation_id=correlation_id)
        status_code = 500
        route_label = "__unmatched__"
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Request-Id"] = correlation_id
            return response
        except HTTPException as exc:
            status_code = exc.status_code
            raise
        finally:
            duration_seconds = max(0.0, time.perf_counter() - start)
            duration_ms = round(duration_seconds * 1000, 2)
            if route_label == "__unmatched__":
                route = request.scope.get("route")
                if route is not None and getattr(route, "path", None):
                    route_label = str(route.path)
            client_ip = request.client.host if request.client is not None else "-"
            message = (
                f"{client_ip} {request.method} {request.url.path} "
                f"Status: {status_code} Time: {duration_ms}ms"
            )
            logger.info(
                message,
                extra={
                    "event_type": "http_request",
                    "correlation_id": correlation_id,
                    "metadata": {
                        "client_ip": client_ip,
                        "http_method": request.method,
                        "http_path": request.url.path,
                        "status_code": status_code,
                        "duration_ms": duration_ms,
                    },
                },
            )
            platform_metrics.record_api_request(
                method=request.method,
                route=route_label,
                status_code=status_code,
                duration_seconds=duration_seconds,
            )
            reset_log_context(context_tokens)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        error_ref = uuid4().hex[:8]
        route = request.scope.get("route")
        route_label = str(route.path) if route is not None and getattr(route, "path", None) else "__unmatched__"
        platform_metrics.record_api_exception(
            method=request.method,
            route=route_label,
            error_type=type(exc).__name__,
        )
        logger.exception(
            "api_unhandled_exception method=%s path=%s error_ref=%s error=%s",
            request.method,
            request.url.path,
            error_ref,
            exc,
            extra={
                "event_type": "api_unhandled_exception",
                "metadata": {
                    "method": request.method,
                    "path": request.url.path,
                    "error_ref": error_ref,
                },
            },
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
            route = request.scope.get("route")
            route_label = str(route.path) if route is not None and getattr(route, "path", None) else "__unmatched__"
            platform_metrics.record_api_exception(
                method=request.method,
                route=route_label,
                error_type=f"http_{exc.status_code}",
            )
            logger.error(
                "api_http_exception_5xx method=%s path=%s status=%s detail=%s",
                request.method,
                request.url.path,
                exc.status_code,
                exc.detail,
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
            headers=exc.headers,
        )

    app.include_router(admin_knowledge_router)
    app.include_router(admin_observability_router)
    app.include_router(admin_runs_router)
    app.include_router(admin_tenants_router)
    app.include_router(admin_auth_router)
    app.include_router(admin_agent_runtimes_router)
    app.include_router(admin_codex_router)
    app.include_router(admin_discord_commands_router)
    app.include_router(admin_discord_allowlist_router)
    app.include_router(admin_discord_install_router)
    app.include_router(admin_github_router)
    app.include_router(admin_jira_router)
    app.include_router(admin_ready_router)
    app.include_router(admin_release_router)
    app.include_router(admin_secrets_router)
    app.include_router(admin_tokens_router)
    app.include_router(app_auth_router)
    app.include_router(discord_router)
    app.include_router(runs_router)
    app.include_router(webhook_router)
    app.include_router(webhook_discord_router)
    app.include_router(webhook_discord_interactions_router)
    app.include_router(webhook_github_router)
    register_discord_command_executor()

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            content=platform_metrics.render_prometheus(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


app = create_app()
