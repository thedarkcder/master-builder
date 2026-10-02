from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from orchestrator.api.webhooks.payload_utils import (
    max_webhook_body_bytes,
    read_json_payload,
)
from orchestrator.core.observability.log_redaction import redact_log_text


def test_webhook_body_limit_stops_consuming_oversized_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES", "4")
    calls = []

    async def receive():
        calls.append(True)
        return {
            "type": "http.request",
            "body": b"12345" if len(calls) == 1 else b"6",
            "more_body": len(calls) == 1,
        }

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/jira/webhook/example",
            "headers": [],
        },
        receive,
    )
    with pytest.raises(HTTPException) as failure:
        asyncio.run(read_json_payload(request, request_id="test", source="jira"))
    assert failure.value.status_code == 413
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["invalid", "0", "-1"])
def test_invalid_webhook_body_limit_fails_configuration(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES", value)
    with pytest.raises(ValueError, match="ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES"):
        max_webhook_body_bytes()


@pytest.mark.parametrize(
    "message",
    [
        "POST /deployments/coolify/webhook/workspace/project/TEST_ONLY_SHARED_TOKEN Status:202",
        "HTTP request https://public.example/deployments/coolify/webhook/workspace/project/TEST_ONLY_SHARED_TOKEN?mode=deploy",
    ],
)
def test_coolify_webhook_shared_url_token_is_redacted(message: str) -> None:
    redacted = redact_log_text(message)
    assert "TEST_ONLY_SHARED_TOKEN" not in redacted
    assert "/deployments/coolify/webhook/workspace/project/[REDACTED]" in redacted
