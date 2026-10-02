import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, Request
from starlette.requests import ClientDisconnect

from orchestrator.api.webhooks import contracts, jira_payload_contracts, payload_utils


def _build_request(
    *, body: bytes = b"{}", headers: dict[str, str] | None = None
) -> Request:
    encoded_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": encoded_headers,
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


class PayloadUtilsTests(unittest.TestCase):
    def test_max_webhook_body_bytes_invalid_env_fails_configuration(self) -> None:
        with patch.dict(
            os.environ, {"ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES": "invalid"}, clear=False
        ):
            with self.assertRaisesRegex(
                ValueError, "ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES"
            ):
                payload_utils.max_webhook_body_bytes()

    def test_read_json_payload_rejects_too_large_payload(self) -> None:
        with patch.dict(
            os.environ, {"ORCHESTRATOR_WEBHOOK_MAX_BODY_BYTES": "4"}, clear=False
        ):
            request = _build_request(body=b'{"abc":1}')
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(
                    payload_utils.read_json_payload(
                        request, request_id="req-1", source="jira"
                    )
                )
        self.assertEqual(ctx.exception.status_code, payload_utils.HTTP_413_TOO_LARGE)

    def test_read_json_payload_rejects_invalid_json(self) -> None:
        request = _build_request(body=b"{bad")
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                payload_utils.read_json_payload(
                    request, request_id="req-1", source="jira"
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_read_json_payload_rejects_non_object(self) -> None:
        request = _build_request(body=b'["x"]')
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                payload_utils.read_json_payload(
                    request, request_id="req-1", source="jira"
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_read_json_payload_handles_client_disconnect(self) -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
        }

        async def receive() -> dict:
            raise ClientDisconnect()

        request = Request(scope, receive)
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(
                payload_utils.read_json_payload(
                    request, request_id="req-1", source="jira"
                )
            )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Client disconnected", str(ctx.exception.detail))

    def test_extract_webhook_token_prefers_custom_header_then_bearer(self) -> None:
        direct = _build_request(headers={"X-Webhook-Token": "abc"})
        bearer = _build_request(headers={"Authorization": "Bearer xyz"})
        self.assertEqual(payload_utils.extract_webhook_token(direct), "abc")
        self.assertEqual(payload_utils.extract_webhook_token(bearer), "xyz")


class JiraPayloadContractsTests(unittest.TestCase):
    def test_normalize_jira_webhook_event_handles_prefix(self) -> None:
        self.assertEqual(
            jira_payload_contracts.normalize_jira_webhook_event(
                " Jira:Comment_Created "
            ),
            "comment_created",
        )
        self.assertIsNone(jira_payload_contracts.normalize_jira_webhook_event(123))

    def test_extract_issue_payload_rejects_missing_issue(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            jira_payload_contracts.extract_issue_payload({})
        self.assertEqual(ctx.exception.status_code, 400)

    def test_parse_jira_comment_command_covers_invalid_and_valid_paths(self) -> None:
        invalid_payload = {"comment": {"body": " /mb run now "}}
        command, arg, error = jira_payload_contracts.parse_jira_comment_command(
            invalid_payload
        )
        self.assertIsNone(command)
        self.assertIsNone(arg)
        self.assertEqual(error, "invalid_comment_command")

        ask_payload = {"comment": {"body": "/mb ask why failing?\ncheck logs"}}
        command, arg, error = jira_payload_contracts.parse_jira_comment_command(
            ask_payload
        )
        self.assertEqual(command, "ask")
        self.assertIn("why failing?", str(arg))
        self.assertIsNone(error)

        clarify_payload = {
            "comment": {
                "body": "/mb clarify Which customer-facing fallback should win?"
            }
        }
        command, arg, error = jira_payload_contracts.parse_jira_comment_command(
            clarify_payload
        )
        self.assertEqual(command, "clarify")
        self.assertIn("fallback", str(arg))
        self.assertIsNone(error)

    def test_extract_status_transition_reads_status_items(self) -> None:
        payload = {
            "changelog": {
                "items": [
                    {"field": "summary", "fromString": "a", "toString": "b"},
                    {
                        "field": "status",
                        "fromString": "To Do",
                        "toString": "In Progress",
                    },
                ]
            }
        }
        self.assertEqual(
            jira_payload_contracts.extract_status_transition(payload),
            ("To Do", "In Progress"),
        )

    def test_extract_changed_fields_normalizes_and_dedupes(self) -> None:
        payload = {
            "changelog": {
                "items": [
                    {"field": "Summary"},
                    {"field": "description"},
                    {"field": "Summary"},
                ]
            }
        }
        self.assertEqual(
            jira_payload_contracts.extract_changed_fields(payload),
            ["summary", "description"],
        )


class WebhookContractsTests(unittest.TestCase):
    def test_extract_delivery_id_checks_headers_in_priority_order(self) -> None:
        request = _build_request(headers={"X-GitHub-Delivery": "gh-1"})
        self.assertEqual(contracts.extract_delivery_id(request), "gh-1")

    def test_validate_github_webhook_signature_rejects_missing_header(self) -> None:
        request = _build_request(headers={})
        with self.assertRaises(HTTPException) as ctx:
            contracts.validate_github_webhook_signature(
                request=request,
                payload_bytes=b"{}",
                shared_secret="s",
                request_id="req-1",
                tenant_id="tenant-a",
            )
        self.assertEqual(ctx.exception.status_code, 401)

    def test_validate_github_webhook_signature_accepts_matching_digest(self) -> None:
        payload = b'{"ok":true}'
        import hashlib
        import hmac

        digest = hmac.new(b"secret", payload, hashlib.sha256).hexdigest()
        request = _build_request(headers={"X-Hub-Signature-256": f"sha256={digest}"})
        contracts.validate_github_webhook_signature(
            request=request,
            payload_bytes=payload,
            shared_secret="secret",
            request_id="req-1",
            tenant_id="tenant-a",
        )

    def test_resolve_platform_github_webhook_secret_requires_value(self) -> None:
        settings = SimpleNamespace(secrets_encryption_key="key")
        with patch(
            "orchestrator.api.webhooks.contracts.resolve_platform_secret_ref",
            return_value=None,
        ):
            with self.assertRaises(HTTPException) as ctx:
                contracts.resolve_global_github_webhook_secret(
                    request_id="req-1", session=MagicMock(), settings=settings
                )
        self.assertEqual(ctx.exception.status_code, 500)

    def test_validate_webhook_auth_rejects_missing_secret_reference(self) -> None:
        tenant = SimpleNamespace(tenant_id="tenant-a", jira_config={})
        with self.assertRaises(HTTPException) as ctx:
            contracts.validate_webhook_auth(
                tenant=tenant,
                request=_build_request(),
                request_id="req-1",
                session=MagicMock(),
                settings=SimpleNamespace(secrets_encryption_key="key"),
            )
        self.assertEqual(ctx.exception.status_code, 500)
