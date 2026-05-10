from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


class PasswordResetTokenError(RuntimeError):
    pass


@dataclass(frozen=True)
class PasswordResetTokenPayload:
    user_id: str
    email: str
    password_updated_at: str
    expires_at: datetime


def normalize_password_reset_timestamp(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def issue_password_reset_token(
    *,
    user_id: str,
    email: str,
    password_updated_at: datetime,
    secret: str,
    ttl_minutes: int = 30,
) -> str:
    expires_at = datetime.now(UTC) + timedelta(minutes=ttl_minutes)
    payload = {
        "user_id": user_id.strip(),
        "email": email.strip().lower(),
        "password_updated_at": normalize_password_reset_timestamp(password_updated_at),
        "expires_at": expires_at.isoformat(),
    }
    payload_token = _urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signature = _sign(secret=secret, payload_token=payload_token)
    return f"{payload_token}.{signature}"


def parse_password_reset_token(*, token: str, secret: str) -> PasswordResetTokenPayload:
    payload_token, separator, signature = token.partition(".")
    if not separator or not payload_token or not signature:
        raise PasswordResetTokenError("Invalid password reset token")
    expected_signature = _sign(secret=secret, payload_token=payload_token)
    if not hmac.compare_digest(signature, expected_signature):
        raise PasswordResetTokenError("Invalid password reset token")
    try:
        payload = json.loads(_urlsafe_b64decode(payload_token).decode("utf-8"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise PasswordResetTokenError("Invalid password reset token") from exc
    expires_at_raw = str(payload.get("expires_at") or "").strip()
    try:
        expires_at = datetime.fromisoformat(expires_at_raw)
    except ValueError as exc:
        raise PasswordResetTokenError("Invalid password reset token") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        raise PasswordResetTokenError("Password reset token expired")
    return PasswordResetTokenPayload(
        user_id=str(payload.get("user_id") or "").strip(),
        email=str(payload.get("email") or "").strip().lower(),
        password_updated_at=str(payload.get("password_updated_at") or "").strip(),
        expires_at=expires_at,
    )


def _sign(*, secret: str, payload_token: str) -> str:
    return hmac.new(secret.encode("utf-8"), payload_token.encode("ascii"), hashlib.sha256).hexdigest()


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)
