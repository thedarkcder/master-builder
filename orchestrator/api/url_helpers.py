from __future__ import annotations

from urllib.parse import urlparse

from fastapi import Request


def resolve_public_base_url(*, request: Request, configured_base_url: str) -> str:
    """Build security links from administrator configuration, never request headers."""
    configured = configured_base_url.strip().rstrip("/")
    parsed = urlparse(configured)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "A configured HTTP(S) public base URL without credentials, query, or fragment is required"
        )
    return configured
