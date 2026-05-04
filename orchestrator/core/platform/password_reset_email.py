from __future__ import annotations

import logging

from orchestrator.core.platform.email_delivery import EmailMessagePayload, deliver_email

logger = logging.getLogger(__name__)


def send_password_reset_email(*, email: str, full_name: str | None, reset_url: str) -> None:
    greeting_name = (full_name or email).strip()
    subject = "Reset your Master Builder password"
    text_body = (
        f"Hi {greeting_name},\n\n"
        "We received a request to reset your Master Builder password.\n"
        f"Reset it here:\n{reset_url}\n\n"
        "This link expires in 30 minutes.\n"
        "If you did not request this reset, you can ignore this email.\n"
    )
    html_body = (
        "<p>"
        f"Hi {greeting_name},"
        "</p>"
        "<p>We received a request to reset your Master Builder password.</p>"
        f'<p><a href="{reset_url}">Reset your password</a></p>'
        "<p>This link expires in 30 minutes.</p>"
        "<p>If you did not request this reset, you can ignore this email.</p>"
    )
    try:
        deliver_email(
            EmailMessagePayload(
                to_email=email,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("password_reset_email_failed email=%s full_name=%s error=%s", email, full_name, exc)
        raise
