from __future__ import annotations

import hashlib
import hmac
import json
import secrets

from orchestrator.core.config import get_settings
from orchestrator.core.platform.secret_service import platform_secret_service
from orchestrator.core.platform.tenant_secret_service import tenant_secret_service
from orchestrator.storage.models import Tenant


def configure_tenant_webhook_sender(*, session_factory, tenant_id: str) -> str:
    """Configure an external test sender against the real encrypted secret store."""
    token = secrets.token_urlsafe(32)
    with session_factory() as session:
        tenant = session.get(Tenant, tenant_id)
        assert tenant is not None
        tenant.jira_config = dict(
            tenant.jira_config, webhook_secret_ref="test-webhook-token"
        )
        tenant.discord_config = dict(
            tenant.discord_config or {}, command_secret_ref="test-webhook-token"
        )
        tenant_secret_service.upsert_secret(
            session=session,
            secret_ref="test-webhook-token",
            plaintext_value=token,
            encryption_key=get_settings().secrets_encryption_key,
            tenant_id=tenant_id,
        )
        session.commit()
    return token


def configure_github_webhook_sender(*, session_factory) -> str:
    token = secrets.token_urlsafe(32)
    with session_factory() as session:
        platform_secret_service.upsert_secret(
            session=session,
            secret_ref="platform/GITHUB_WEBHOOK_SECRET",
            plaintext_value=token,
            encryption_key=get_settings().secrets_encryption_key,
        )
        session.commit()
    return token


def post_signed_github_webhook(
    *, client, token: str, url: str, json_payload: dict, headers: dict
):
    body = json.dumps(json_payload, ensure_ascii=False, separators=(",", ":")).encode()
    signature = "sha256=" + hmac.new(token.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        url,
        content=body,
        headers=dict(
            headers,
            **{"Content-Type": "application/json", "X-Hub-Signature-256": signature},
        ),
    )
