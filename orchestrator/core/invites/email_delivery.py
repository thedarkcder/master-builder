from __future__ import annotations

import logging

from orchestrator.core.platform.email_delivery import EmailMessagePayload, deliver_email

logger = logging.getLogger(__name__)


def send_tenant_invite_email(
    *, email: str, full_name: str | None, invite_url: str, tenant_name: str
) -> None:
    greeting_name = (full_name or email).strip()
    subject = f"You've been invited to join {tenant_name} on Master Builder"
    text_body = (
        f"Hi {greeting_name},\n\n"
        f"You've been invited to join {tenant_name} on Master Builder.\n"
        f"Accept your invitation here:\n{invite_url}\n\n"
        "This invite expires in 7 days.\n"
    )
    html_body = (
        "<p>"
        f"Hi {greeting_name},"
        "</p>"
        "<p>"
        f"You've been invited to join <strong>{tenant_name}</strong> on Master Builder."
        "</p>"
        f'<p><a href="{invite_url}">Accept your invitation</a></p>'
        "<p>This invite expires in 7 days.</p>"
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
        logger.exception(
            "tenant_invite_email_failed tenant=%s email=%s full_name=%s error=%s",
            tenant_name,
            email,
            full_name,
            exc,
        )
        raise
