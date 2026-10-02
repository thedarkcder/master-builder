from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from urllib.parse import quote, urlencode

START_WORK_ACTION = "start_ready_development"
DEFAULT_START_WORK_ACTION_TTL_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class StartWorkActionTokenClaims:
    tenant_id: str
    project_id: str
    execution_id: str
    workflow_id: str
    issue_key: str


def create_start_work_action_token(
    *,
    claims: StartWorkActionTokenClaims,
    secret: str,
    ttl_seconds: int = DEFAULT_START_WORK_ACTION_TTL_SECONDS,
) -> str:
    normalized_secret = _required_secret(secret)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=max(60, int(ttl_seconds))
    )
    payload = {
        "action": START_WORK_ACTION,
        "tenant_id": _required("tenant_id", claims.tenant_id),
        "project_id": _required("project_id", claims.project_id),
        "execution_id": _required("execution_id", claims.execution_id),
        "workflow_id": _required("workflow_id", claims.workflow_id),
        "issue_key": _required("issue_key", claims.issue_key).upper(),
        "exp": int(expires_at.timestamp()),
    }
    encoded_payload = _urlsafe_b64encode(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    signature = _signature(encoded_payload=encoded_payload, secret=normalized_secret)
    return f"{encoded_payload}.{signature}"


def verify_start_work_action_token(
    *,
    token: str,
    secret: str,
    expected_execution_id: str,
) -> StartWorkActionTokenClaims:
    normalized_secret = _required_secret(secret)
    encoded_payload, signature = _split_token(token)
    expected_signature = _signature(
        encoded_payload=encoded_payload, secret=normalized_secret
    )
    if not hmac.compare_digest(signature, expected_signature):
        raise ValueError("Start work action token signature is invalid")
    payload = json.loads(_urlsafe_b64decode(encoded_payload).decode("utf-8"))
    if payload.get("action") != START_WORK_ACTION:
        raise ValueError("Start work action token action is invalid")
    expires_at = int(payload.get("exp") or 0)
    if expires_at < int(datetime.now(timezone.utc).timestamp()):
        raise ValueError("Start work action token has expired")
    execution_id = _required("execution_id", payload.get("execution_id"))
    if execution_id != _required("expected_execution_id", expected_execution_id):
        raise ValueError("Start work action token does not match this execution")
    return StartWorkActionTokenClaims(
        tenant_id=_required("tenant_id", payload.get("tenant_id")),
        project_id=_required("project_id", payload.get("project_id")),
        execution_id=execution_id,
        workflow_id=_required("workflow_id", payload.get("workflow_id")),
        issue_key=_required("issue_key", payload.get("issue_key")).upper(),
    )


def build_start_work_action_url(
    *,
    admin_ui_base_url: str,
    claims: StartWorkActionTokenClaims,
    secret: str,
    ttl_seconds: int = DEFAULT_START_WORK_ACTION_TTL_SECONDS,
) -> str:
    base_url = str(admin_ui_base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError(
            "Admin UI base URL is required to build a start work action link"
        )
    token = create_start_work_action_token(
        claims=claims, secret=secret, ttl_seconds=ttl_seconds
    )
    path = f"/{quote(claims.tenant_id, safe='')}/start/{quote(claims.execution_id, safe='')}"
    return f"{base_url}{path}?{urlencode({'startDevelopmentToken': token})}"


def _split_token(token: str) -> tuple[str, str]:
    normalized = str(token or "").strip()
    if not normalized or "." not in normalized:
        raise ValueError("Start work action token is malformed")
    encoded_payload, signature = normalized.rsplit(".", 1)
    if not encoded_payload or not signature:
        raise ValueError("Start work action token is malformed")
    return encoded_payload, signature


def _signature(*, encoded_payload: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
    ).digest()
    return _urlsafe_b64encode(digest)


def _required(field_name: str, value: object) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"Start work action token requires {field_name}")
    return normalized


def _required_secret(secret: str) -> str:
    normalized = str(secret or "").strip()
    if not normalized:
        raise ValueError("Start work action token secret is required")
    return normalized


def _urlsafe_b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _urlsafe_b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))
