import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from urllib.error import HTTPError

from orchestrator.core.email_delivery import (
    EmailDeliveryError,
    EmailMessagePayload,
    ResendEmailDeliveryProvider,
    SmtpEmailDeliveryProvider,
    _format_resend_error_body,
    _parse_resend_success_id,
    build_email_delivery_provider,
    deliver_email,
)


class ResendEmailDeliveryTests(unittest.TestCase):
    def test_parse_resend_success_id(self) -> None:
        self.assertEqual(_parse_resend_success_id('{"id":"em_1"}'), "em_1")
        self.assertIsNone(_parse_resend_success_id(""))
        self.assertIsNone(_parse_resend_success_id("not-json"))

    def test_format_resend_error_body(self) -> None:
        self.assertIn("validation", _format_resend_error_body('{"message":"validation failed"}'))
        self.assertEqual(_format_resend_error_body(""), "(empty response body)")
        raw = '{"message": ["a", "b"]}'
        out = _format_resend_error_body(raw)
        self.assertIn("a", out)
        self.assertIn("b", out)

    def test_resend_send_success(self) -> None:
        settings = SimpleNamespace(
            resend_api_key="re_test_key",
            email_from_address="onboarding@resend.dev",
            email_from_name="Master Builder",
            email_reply_to="",
        )
        payload = EmailMessagePayload(
            to_email="user@example.com",
            subject="Hello",
            text_body="plain",
            html_body="<p>html</p>",
        )
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = b'{"id":"em_abc123"}'
        mock_cm.__exit__.return_value = None
        with patch("orchestrator.core.email_delivery.urlopen", return_value=mock_cm) as mock_urlopen:
            ResendEmailDeliveryProvider(settings=settings).send(payload)
        self.assertTrue(mock_urlopen.called)
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(getattr(req, "full_url", req.get_full_url()), "https://api.resend.com/emails")

    def test_resend_missing_api_key(self) -> None:
        settings = SimpleNamespace(
            resend_api_key="   ",
            email_from_address="onboarding@resend.dev",
            email_from_name="X",
            email_reply_to="",
        )
        with self.assertRaises(EmailDeliveryError) as ctx:
            ResendEmailDeliveryProvider(settings=settings).send(
                EmailMessagePayload(to_email="a@b.com", subject="s", text_body="t"),
            )
        self.assertIn("API key", str(ctx.exception))

    def test_resend_http_error_includes_message(self) -> None:
        settings = SimpleNamespace(
            resend_api_key="re_x",
            email_from_address="onboarding@resend.dev",
            email_from_name="X",
            email_reply_to="",
        )
        body = BytesIO(b'{"message":"from domain not verified"}')
        err = HTTPError("https://api.resend.com/emails", 403, "Forbidden", {}, body)
        with patch("orchestrator.core.email_delivery.urlopen", side_effect=err):
            with self.assertRaises(EmailDeliveryError) as ctx:
                ResendEmailDeliveryProvider(settings=settings).send(
                    EmailMessagePayload(to_email="a@b.com", subject="s", text_body="t"),
                )
        self.assertIn("from domain not verified", str(ctx.exception))

    def test_build_provider_selects_resend(self) -> None:
        settings = SimpleNamespace(
            email_delivery_provider="resend",
            resend_api_key="re_x",
            smtp_host="localhost",
            smtp_port=1025,
            smtp_username="",
            smtp_password="",
            smtp_use_tls=False,
            smtp_use_ssl=False,
            email_from_address="a@b.com",
            email_from_name="T",
            email_reply_to="",
        )
        provider = build_email_delivery_provider(settings=settings)
        self.assertIsInstance(provider, ResendEmailDeliveryProvider)

    def test_build_provider_defaults_to_smtp(self) -> None:
        settings = SimpleNamespace(
            email_delivery_provider="smtp",
            resend_api_key="",
            smtp_host="localhost",
            smtp_port=1025,
            smtp_username="",
            smtp_password="",
            smtp_use_tls=False,
            smtp_use_ssl=False,
            email_from_address="a@b.com",
            email_from_name="T",
            email_reply_to="",
        )
        provider = build_email_delivery_provider(settings=settings)
        self.assertIsInstance(provider, SmtpEmailDeliveryProvider)


class DeliverEmailIntegrationTests(unittest.TestCase):
    def test_deliver_email_uses_resend_when_configured(self) -> None:
        settings = SimpleNamespace(
            email_delivery_provider="resend",
            resend_api_key="re_x",
            smtp_host="",
            smtp_port=1025,
            smtp_username="",
            smtp_password="",
            smtp_use_tls=False,
            smtp_use_ssl=False,
            email_from_address="onboarding@resend.dev",
            email_from_name="T",
            email_reply_to="",
        )
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value.read.return_value = b'{"id":"em_z"}'
        mock_cm.__exit__.return_value = None
        with patch("orchestrator.core.email_delivery.urlopen", return_value=mock_cm):
            deliver_email(
                EmailMessagePayload(to_email="a@b.com", subject="s", text_body="t"),
                settings=settings,
            )
