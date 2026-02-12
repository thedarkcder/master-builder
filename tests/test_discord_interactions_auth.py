from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from cryptography.exceptions import InvalidSignature
from fastapi import HTTPException

from orchestrator.api.discord.interactions.auth import (
    _discord_autocomplete_response,
    _discord_interaction_deferred_response,
    _discord_interaction_modal_response,
    _discord_interaction_response,
    _discord_modal_text_value,
    _parse_ask_confirmation_custom_id,
    _parse_ask_reply_modal_custom_id,
    _resolve_discord_interactions_public_key,
    _validate_discord_interaction_signature,
)


class DiscordInteractionsAuthTests(unittest.TestCase):
    def test_resolve_discord_interactions_public_key(self) -> None:
        settings = SimpleNamespace(secrets_encryption_key="enc")
        session = MagicMock()

        with patch("orchestrator.api.discord.interactions.auth.resolve_scoped_secret_ref", return_value=""):
            with self.assertRaises(HTTPException) as exc_ctx:
                _resolve_discord_interactions_public_key(session=session, settings=settings)
        self.assertEqual(exc_ctx.exception.status_code, 500)

        with patch("orchestrator.api.discord.interactions.auth.resolve_scoped_secret_ref", return_value="not-hex"):
            with self.assertRaises(HTTPException) as exc_ctx:
                _resolve_discord_interactions_public_key(session=session, settings=settings)
        self.assertIn("hex-encoded", exc_ctx.exception.detail)

        with patch("orchestrator.api.discord.interactions.auth.resolve_scoped_secret_ref", return_value="ab" * 31):
            with self.assertRaises(HTTPException) as exc_ctx:
                _resolve_discord_interactions_public_key(session=session, settings=settings)
        self.assertIn("32 bytes", exc_ctx.exception.detail)

        with patch("orchestrator.api.discord.interactions.auth.resolve_scoped_secret_ref", return_value="ab" * 32):
            key = _resolve_discord_interactions_public_key(session=session, settings=settings)
        self.assertEqual(len(key), 32)

    def test_validate_discord_interaction_signature(self) -> None:
        request = SimpleNamespace(headers={})
        with self.assertRaises(HTTPException) as exc_ctx:
            _validate_discord_interaction_signature(
                request=request,
                payload_bytes=b"{}",
                public_key=b"a" * 32,
            )
        self.assertEqual(exc_ctx.exception.status_code, 401)

        request = SimpleNamespace(headers={"X-Signature-Ed25519": "not-hex", "X-Signature-Timestamp": "1"})
        with self.assertRaises(HTTPException) as exc_ctx:
            _validate_discord_interaction_signature(
                request=request,
                payload_bytes=b"{}",
                public_key=b"a" * 32,
            )
        self.assertEqual(exc_ctx.exception.status_code, 401)

        request = SimpleNamespace(headers={"X-Signature-Ed25519": "ab" * 64, "X-Signature-Timestamp": "1"})
        verifier = MagicMock()
        verifier.verify.side_effect = InvalidSignature()
        with patch("orchestrator.api.discord.interactions.auth.Ed25519PublicKey.from_public_bytes", return_value=verifier):
            with self.assertRaises(HTTPException) as exc_ctx:
                _validate_discord_interaction_signature(
                    request=request,
                    payload_bytes=b"{}",
                    public_key=b"a" * 32,
                )
        self.assertEqual(exc_ctx.exception.status_code, 401)

        verifier = MagicMock()
        with patch("orchestrator.api.discord.interactions.auth.Ed25519PublicKey.from_public_bytes", return_value=verifier):
            _validate_discord_interaction_signature(
                request=request,
                payload_bytes=b"{}",
                public_key=b"a" * 32,
            )
        verifier.verify.assert_called_once()

    def test_interaction_response_helpers(self) -> None:
        response = _discord_interaction_response(content="ok", ephemeral=True)
        payload = json.loads(response.body)
        self.assertEqual(payload["type"], 4)
        self.assertEqual(payload["data"]["flags"], 64)

        response = _discord_interaction_response(content="ok", ephemeral=False)
        payload = json.loads(response.body)
        self.assertNotIn("flags", payload["data"])

        deferred = _discord_interaction_deferred_response(ephemeral=True)
        deferred_payload = json.loads(deferred.body)
        self.assertEqual(deferred_payload["type"], 5)
        self.assertEqual(deferred_payload["data"]["flags"], 64)

        choices = [{"name": f"x-{i}", "value": f"x-{i}"} for i in range(30)]
        autocomplete = _discord_autocomplete_response(choices=choices)
        autocomplete_payload = json.loads(autocomplete.body)
        self.assertEqual(len(autocomplete_payload["data"]["choices"]), 25)

    def test_custom_id_parsers_and_modal_response(self) -> None:
        parsed = _parse_ask_confirmation_custom_id("ask.approve.0123456789abcdef0123456789abcdef")
        self.assertEqual(parsed, ("approve", "0123456789abcdef0123456789abcdef"))
        self.assertIsNone(_parse_ask_confirmation_custom_id("ask.approve.bad"))

        self.assertEqual(_parse_ask_reply_modal_custom_id("ask.reply.123456789012345"), "123456789012345")
        self.assertIsNone(_parse_ask_reply_modal_custom_id("ask.reply.bad"))

        modal = _discord_interaction_modal_response(
            custom_id="cid",
            title="T" * 80,
            text_input_custom_id="icid",
            text_input_label="L" * 80,
            placeholder="P" * 120,
        )
        payload = json.loads(modal.body)
        self.assertEqual(payload["type"], 9)
        self.assertEqual(len(payload["data"]["title"]), 45)
        component = payload["data"]["components"][0]["components"][0]
        self.assertEqual(len(component["label"]), 45)
        self.assertEqual(len(component["placeholder"]), 100)

    def test_discord_modal_text_value(self) -> None:
        payload = {
            "data": {
                "components": [
                    {
                        "components": [
                            {"type": 4, "custom_id": "reply", "value": "   hello world   "},
                        ]
                    }
                ]
            }
        }
        self.assertEqual(_discord_modal_text_value(payload, custom_id="reply"), "hello world")
        self.assertIsNone(_discord_modal_text_value({"data": {}}, custom_id="reply"))
        self.assertIsNone(_discord_modal_text_value(payload, custom_id="other"))


if __name__ == "__main__":
    unittest.main()
