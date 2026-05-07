from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
from jwt import InvalidTokenError


def create_auth_access_token(*, user_id: str, secret: str, ttl_seconds: int) -> tuple[str, int]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=ttl_seconds)
    payload = {
        "sub": user_id,
        "iat": int(now.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": "master-builder-auth",
    }
    token = jwt.encode(payload, secret, algorithm="HS256")
    return token, ttl_seconds


def parse_auth_access_token(*, token: str, secret: str) -> str:
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"], issuer="master-builder-auth")
    except InvalidTokenError as exc:
        raise ValueError("Invalid auth access token") from exc

    subject = payload.get("sub")
    if not isinstance(subject, str) or not subject:
        raise ValueError("Invalid auth access token")
    return subject
