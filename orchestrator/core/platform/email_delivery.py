"""
Email delivery abstraction: SMTP (dev/Mailpit) or Resend (HTTPS API).

Tenant invites and password resets call :func:`deliver_email` with a normalized
:class:`EmailMessagePayload`. Switch providers via ``ORCHESTRATOR_EMAIL_DELIVERY_PROVIDER``
(``smtp`` | ``resend``). Resend requires ``ORCHESTRATOR_RESEND_API_KEY`` and a
``from`` address on a domain verified in the Resend dashboard.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from orchestrator.core.config import Settings, get_settings


logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"


class EmailDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class EmailMessagePayload:
    to_email: str
    subject: str
    text_body: str
    html_body: str | None = None


class EmailDeliveryProvider:
    def send(self, payload: EmailMessagePayload) -> None:
        raise NotImplementedError


class SmtpEmailDeliveryProvider(EmailDeliveryProvider):
    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings

    def send(self, payload: EmailMessagePayload) -> None:
        message = EmailMessage()
        message["Subject"] = payload.subject
        message["From"] = _format_from_header(settings=self._settings)
        message["To"] = payload.to_email
        if self._settings.email_reply_to:
            message["Reply-To"] = self._settings.email_reply_to
        message.set_content(payload.text_body)
        if payload.html_body:
            message.add_alternative(payload.html_body, subtype="html")

        try:
            if self._settings.smtp_use_ssl:
                with smtplib.SMTP_SSL(
                    host=self._settings.smtp_host,
                    port=self._settings.smtp_port,
                    context=ssl.create_default_context(),
                    timeout=30,
                ) as client:
                    _smtp_login_if_needed(client=client, settings=self._settings)
                    client.send_message(message)
                return

            with smtplib.SMTP(host=self._settings.smtp_host, port=self._settings.smtp_port, timeout=30) as client:
                client.ehlo()
                if self._settings.smtp_use_tls:
                    client.starttls(context=ssl.create_default_context())
                    client.ehlo()
                _smtp_login_if_needed(client=client, settings=self._settings)
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise EmailDeliveryError(f"SMTP delivery failed: {exc}") from exc


class ResendEmailDeliveryProvider(EmailDeliveryProvider):
    """Send mail through `Resend <https://resend.com/docs/api-reference/emails/send-email>`_."""

    def __init__(self, *, settings: Settings) -> None:
        self._settings = settings

    def send(self, payload: EmailMessagePayload) -> None:
        if not self._settings.resend_api_key.strip():
            raise EmailDeliveryError("Resend API key is missing")
        body: dict[str, object] = {
            "from": _format_from_header(settings=self._settings),
            "to": [payload.to_email],
            "subject": payload.subject,
            "text": payload.text_body,
        }
        if payload.html_body:
            body["html"] = payload.html_body
        reply_to = self._settings.email_reply_to.strip()
        if reply_to:
            body["reply_to"] = reply_to
        request = Request(
            url=_RESEND_API_URL,
            method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._settings.resend_api_key.strip()}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise EmailDeliveryError(
                f"Resend delivery failed ({exc.code}): {_format_resend_error_body(error_body)}",
            ) from exc
        except URLError as exc:
            raise EmailDeliveryError(f"Resend delivery failed: {exc}") from exc

        resend_id = _parse_resend_success_id(raw)
        if resend_id:
            logger.info("resend_email_accepted id=%s to=%s", resend_id, payload.to_email)
        else:
            logger.info("resend_email_sent to=%s", payload.to_email)


def _format_resend_error_body(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return "(empty response body)"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    message = data.get("message")
    if isinstance(message, str) and message:
        return message
    if isinstance(message, list):
        parts = [str(item) for item in message if item]
        if parts:
            return "; ".join(parts)
    return raw


def _parse_resend_success_id(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    rid = data.get("id")
    return str(rid) if rid else None


def build_email_delivery_provider(*, settings: Settings | None = None) -> EmailDeliveryProvider:
    resolved_settings = settings or get_settings()
    provider = str(resolved_settings.email_delivery_provider or "smtp").strip().lower()
    if provider == "resend":
        return ResendEmailDeliveryProvider(settings=resolved_settings)
    return SmtpEmailDeliveryProvider(settings=resolved_settings)


def deliver_email(payload: EmailMessagePayload, *, settings: Settings | None = None) -> None:
    try:
        build_email_delivery_provider(settings=settings).send(payload)
    except EmailDeliveryError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise EmailDeliveryError(f"Email delivery failed: {exc}") from exc


def _format_from_header(*, settings: Settings) -> str:
    address = settings.email_from_address.strip()
    if not address:
        raise EmailDeliveryError("Email from address is missing")
    name = settings.email_from_name.strip()
    return f"{name} <{address}>" if name else address


def _smtp_login_if_needed(*, client: smtplib.SMTP, settings: Settings) -> None:
    username = settings.smtp_username.strip()
    if not username:
        return
    client.login(username, settings.smtp_password)
