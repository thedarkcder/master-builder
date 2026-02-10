from __future__ import annotations

import json
import logging
import os

from fastapi import HTTPException, Request, status

DEFAULT_WEBHOOK_MAX_BODY_BYTES = 1_048_576
HTTP_413_TOO_LARGE = getattr(
    status,
    "HTTP_413_CONTENT_TOO_LARGE",
    status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
)

logger = logging.getLogger(__name__)


def max_webhook_body_bytes() -> int:
    raw_value = os.environ.get("ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES", str(DEFAULT_WEBHOOK_MAX_BODY_BYTES))
    try:
        parsed = int(raw_value)
        if parsed <= 0:
            raise ValueError
    except ValueError:
        logger.warning(
            "invalid_webhook_max_body_bytes value=%s default=%s",
            raw_value,
            DEFAULT_WEBHOOK_MAX_BODY_BYTES,
        )
        return DEFAULT_WEBHOOK_MAX_BODY_BYTES
    return parsed


async def read_json_payload(
    request: Request,
    *,
    request_id: str,
    source: str,
) -> tuple[dict, bytes]:
    body = await request.body()
    max_bytes = max_webhook_body_bytes()
    if len(body) > max_bytes:
        logger.warning(
            "%s_webhook_payload_too_large request_id=%s body_bytes=%s max_bytes=%s",
            source,
            request_id,
            len(body),
            max_bytes,
        )
        raise HTTPException(
            status_code=HTTP_413_TOO_LARGE,
            detail="Payload too large",
        )

    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid payload")
    return payload, body


def extract_webhook_token(request: Request) -> str | None:
    webhook_token = request.headers.get("X-Webhook-Token")
    if webhook_token:
        return webhook_token.strip()

    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()

    return None
