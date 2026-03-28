from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request


_LOCAL_HOSTS = {"", "localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal"}


def resolve_public_base_url(*, request: Request, configured_base_url: str) -> str:
    configured = configured_base_url.strip().rstrip("/")
    if configured:
        parsed = urlparse(configured)
        if (parsed.hostname or "").lower() not in _LOCAL_HOSTS:
            return configured

    forwarded_host = request.headers.get("x-forwarded-host", "").strip()
    host = forwarded_host or request.headers.get("host", "").strip()
    if host:
        proto = request.headers.get("x-forwarded-proto", "").strip() or request.url.scheme
        return f"{proto}://{host}".rstrip("/")

    return configured or str(request.base_url).rstrip("/")
