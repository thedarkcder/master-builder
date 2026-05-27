from __future__ import annotations

import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from orchestrator.core.platform.secret_service import resolve_platform_secret_ref

DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF = "DISCORD_INTERACTIONS_PUBLIC_KEY"
ASK_CONFIRM_CUSTOM_ID_PATTERN = re.compile(r"^ask\.(approve|reject)\.([0-9a-f]{32})$")
INSTALL_REQUEST_DECISION_CUSTOM_ID_PATTERN = re.compile(r"^install_request\.(approve|reject)\.([0-9a-f]{32})$")
ASK_REPLY_MODAL_CUSTOM_ID_PATTERN = re.compile(r"^ask\.reply\.([0-9]{15,25})$")
ASK_REPLY_OPEN_CUSTOM_ID = "ask.reply.open"


def _resolve_discord_interactions_public_key(
    *,
    session: Session,
    settings,
) -> bytes:  # noqa: ANN001
    raw_public_key = (
        resolve_platform_secret_ref(
            session,
            secret_ref=DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF,
            encryption_key=settings.secrets_encryption_key,
        )
        or ""
    ).strip()
    if not raw_public_key:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Discord interactions public key is missing. "
                f"Set managed secret '{DISCORD_INTERACTIONS_PUBLIC_KEY_SECRET_REF}'."
            ),
        )

    try:
        public_key_bytes = bytes.fromhex(raw_public_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Discord interactions public key must be a hex-encoded Ed25519 public key",
        ) from exc

    if len(public_key_bytes) != 32:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Discord interactions public key must be 32 bytes (64 hex chars)",
        )
    return public_key_bytes


def _validate_discord_interaction_signature(
    *,
    request: Request,
    payload_bytes: bytes,
    public_key: bytes,
) -> None:
    signature_hex = (request.headers.get("X-Signature-Ed25519") or "").strip()
    timestamp = (request.headers.get("X-Signature-Timestamp") or "").strip()
    if not signature_hex or not timestamp:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Discord interaction signature headers",
        )

    try:
        signature = bytes.fromhex(signature_hex)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Discord interaction signature",
        ) from exc

    message = timestamp.encode("utf-8") + payload_bytes
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except InvalidSignature as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Discord interaction signature",
        ) from exc


def _discord_interaction_response(*, content: str, ephemeral: bool = True) -> JSONResponse:
    response_data = {"content": content}
    if ephemeral:
        response_data["flags"] = 64
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"type": 4, "data": response_data},
    )


def _discord_interaction_deferred_response(*, ephemeral: bool = True) -> JSONResponse:
    response_data: dict[str, object] = {}
    if ephemeral:
        response_data["flags"] = 64
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"type": 5, "data": response_data},
    )


def _discord_autocomplete_response(*, choices: list[dict]) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"type": 8, "data": {"choices": choices[:25]}},
    )


def _parse_ask_confirmation_custom_id(custom_id: str) -> tuple[str, str] | None:
    match = ASK_CONFIRM_CUSTOM_ID_PATTERN.match(custom_id.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def _parse_install_request_decision_custom_id(custom_id: str) -> tuple[str, str] | None:
    match = INSTALL_REQUEST_DECISION_CUSTOM_ID_PATTERN.match(custom_id.strip())
    if match is None:
        return None
    return match.group(1), match.group(2)


def _parse_ask_reply_modal_custom_id(custom_id: str) -> str | None:
    match = ASK_REPLY_MODAL_CUSTOM_ID_PATTERN.match(custom_id.strip())
    if match is None:
        return None
    return match.group(1)


def _discord_interaction_modal_response(
    *,
    custom_id: str,
    title: str,
    text_input_custom_id: str,
    text_input_label: str,
    placeholder: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={
            "type": 9,
            "data": {
                "custom_id": custom_id,
                "title": title[:45],
                "components": [
                    {
                        "type": 1,
                        "components": [
                            {
                                "type": 4,
                                "custom_id": text_input_custom_id,
                                "label": text_input_label[:45],
                                "style": 2,
                                "min_length": 1,
                                "max_length": 1800,
                                "required": True,
                                "placeholder": placeholder[:100],
                            }
                        ],
                    }
                ],
            },
        },
    )


def _discord_modal_text_value(payload: dict, *, custom_id: str) -> str | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    rows = data.get("components")
    if not isinstance(rows, list):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        components = row.get("components")
        if not isinstance(components, list):
            continue
        for component in components:
            if not isinstance(component, dict):
                continue
            if component.get("type") != 4:
                continue
            component_custom_id = str(component.get("custom_id") or "").strip()
            if component_custom_id != custom_id:
                continue
            value = component.get("value")
            if value is None:
                return None
            normalized = str(value).strip()
            return normalized or None
    return None
