from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class AtlassianOAuthState:
    exp: int
    return_to: str
    tenant_id: str | None


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(raw: str) -> bytes:
    padding = "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode((raw + padding).encode("ascii"))


def create_atlassian_oauth_state_token(
    *,
    exp: datetime,
    secret: str,
    return_to: str = "wizard",
    tenant_id: str | None = None,
) -> str:
    if not secret:
        raise ValueError("secret is required")
    if return_to not in {"wizard", "edit"}:
        raise ValueError("return_to must be 'wizard' or 'edit'")

    payload = {
        "exp": int(exp.astimezone(timezone.utc).timestamp()),
        "return_to": return_to,
        "tenant_id": tenant_id,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    payload_token = _b64url_encode(payload_bytes)
    signature = hmac.new(secret.encode("utf-8"), payload_token.encode("ascii"), hashlib.sha256).digest()
    signature_token = _b64url_encode(signature)
    return f"{payload_token}.{signature_token}"


def parse_atlassian_oauth_state_token(
    *,
    token: str,
    secret: str,
    now: datetime | None = None,
) -> AtlassianOAuthState:
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

    exp = payload.get("exp")
    return_to = payload.get("return_to")
    tenant_id = payload.get("tenant_id")
    if not isinstance(exp, int):
        raise ValueError("invalid state token exp")
    if return_to not in {"wizard", "edit"}:
        raise ValueError("invalid state token return_to")
    if tenant_id is not None and not isinstance(tenant_id, str):
        raise ValueError("invalid state token tenant_id")
    if return_to == "edit" and not tenant_id:
        raise ValueError("invalid state token tenant_id")

    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if exp <= int(now_utc.timestamp()):
        raise ValueError("state token expired")

    return AtlassianOAuthState(exp=exp, return_to=return_to, tenant_id=tenant_id)
