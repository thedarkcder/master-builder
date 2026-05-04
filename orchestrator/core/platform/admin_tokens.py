from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from jwt import InvalidTokenError


def create_admin_access_token(*, username: str, secret: str, ttl_seconds: int) -> tuple[str, int]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    payload = {
        "sub": username,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": "master-builder-admin",
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return token, ttl_seconds


def parse_admin_access_token(*, token: str, secret: str) -> str:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], issuer="master-builder-admin")
    except InvalidTokenError as exc:
        raise ValueError("Invalid admin access token") from exc

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError("Invalid admin access token")
    return subject
