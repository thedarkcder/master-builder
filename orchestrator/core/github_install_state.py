from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class GitHubInstallState:
    tenant_id: str
    exp: int


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def create_install_state_token(*, tenant_id: str, exp: datetime, secret: str) -> str:
    if not tenant_id.strip():
        raise ValueError("tenant_id is required")
    if not secret:
        raise ValueError("secret is required")

    exp_utc = exp.astimezone(timezone.utc)
    payload = {
        "tenant_id": tenant_id.strip(),
        "exp": int(exp_utc.timestamp()),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_token = _b64url_encode(payload_bytes)
    signature = hmac.new(secret.encode("utf-8"), payload_token.encode("ascii"), hashlib.sha256).digest()
    signature_token = _b64url_encode(signature)
    return f"{payload_token}.{signature_token}"


def parse_install_state_token(*, token: str, secret: str, now: datetime | None = None) -> GitHubInstallState:
    if not token:
        raise ValueError("state token is required")
    if not secret:
        raise ValueError("secret is required")

    try:
        payload_token, signature_token = token.split(".", 1)
    except ValueError as exc:
        raise ValueError("invalid state token format") from exc

    expected_signature = hmac.new(
        secret.encode("utf-8"), payload_token.encode("ascii"), hashlib.sha256
    ).digest()
    expected_signature_token = _b64url_encode(expected_signature)

    if not hmac.compare_digest(expected_signature_token, signature_token):
        raise ValueError("invalid state token signature")

    try:
        payload_raw = _b64url_decode(payload_token)
        payload = json.loads(payload_raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid state token payload") from exc

    tenant_id = payload.get("tenant_id")
    exp = payload.get("exp")

    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("invalid state token tenant_id")
    if not isinstance(exp, int):
        raise ValueError("invalid state token exp")

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if exp <= int(now_utc.timestamp()):
        raise ValueError("state token expired")

    return GitHubInstallState(tenant_id=tenant_id.strip(), exp=exp)
