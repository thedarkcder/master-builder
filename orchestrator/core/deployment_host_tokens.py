from __future__ import annotations

import hashlib
import secrets


def generate_deployment_host_token() -> str:
    return secrets.token_urlsafe(32)


def hash_deployment_host_token(raw_token: str) -> str:
    normalized = str(raw_token or "").strip()
    if not normalized:
        raise ValueError("deployment host token is required")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
