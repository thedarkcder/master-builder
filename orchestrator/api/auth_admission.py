"""Shared identity ingress budgets, committed independently of authentication."""

from hashlib import sha256
import hmac
import time

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.tenant_rls import set_identity_auth_rls_context


_UPSERT = text("""
INSERT INTO auth_request_budgets (bucket_key, window_start, expires_at, attempts)
VALUES (:key, :start, :expires, 1)
ON CONFLICT (bucket_key) DO UPDATE SET
 window_start = excluded.window_start,
 expires_at = excluded.expires_at,
 attempts = CASE WHEN auth_request_budgets.window_start < excluded.window_start
                 THEN 1 ELSE auth_request_budgets.attempts + 1 END
WHERE auth_request_budgets.window_start < excluded.window_start
   OR (auth_request_budgets.window_start = excluded.window_start
       AND auth_request_budgets.attempts < :limit)
RETURNING attempts
""")


def consume_budget(
    *,
    action: str,
    dimension: str,
    identifier: str,
    limit: int,
    window_seconds: int,
    now: int | None = None,
) -> None:
    if limit <= 0 or window_seconds <= 0:
        raise ValueError("Admission limits and windows must be positive")
    settings = get_settings()
    instant = int(time.time()) if now is None else now
    start = instant - instant % window_seconds
    key = hmac.new(
        settings.auth_token_secret.encode(),
        f"{action}\0{dimension}\0{identifier.strip().casefold()}".encode(),
        sha256,
    ).hexdigest()
    try:
        with create_session_factory()() as session, session.begin():
            set_identity_auth_rls_context(session)
            # Bound cleanup work and retained expired identifiers on each admission.
            session.execute(
                text("""DELETE FROM auth_request_budgets WHERE bucket_key IN
                (SELECT bucket_key FROM auth_request_budgets WHERE expires_at <= :now
                 ORDER BY expires_at LIMIT 100)"""),
                {"now": instant},
            )
            admitted = session.execute(
                _UPSERT,
                {
                    "key": key,
                    "start": start,
                    "expires": start + window_seconds,
                    "limit": limit,
                },
            ).scalar_one_or_none()
    except SQLAlchemyError:
        raise HTTPException(503, "Identity admission store unavailable") from None
    if admitted is None:
        raise HTTPException(
            429,
            "Too many authentication attempts",
            headers={"Retry-After": str(max(1, start + window_seconds - instant))},
        )


def enforce_auth_admission(
    request: Request, *, action: str, account: str | None = None
) -> None:
    settings = get_settings()
    if action == "register" and not settings.public_registration_enabled:
        raise HTTPException(
            403, "Public registration is disabled; contact an administrator"
        )
    if request.client is None:
        raise HTTPException(400, "Authentication requires a known ingress peer")
    if action in {"login", "admin-login"}:
        window, peer, account_limit = (
            settings.auth_login_window_seconds,
            settings.auth_login_peer_limit,
            settings.auth_login_account_limit,
        )
    elif action == "reset-request":
        window, peer, account_limit = (
            settings.auth_reset_window_seconds,
            settings.auth_reset_peer_limit,
            settings.auth_reset_account_limit,
        )
    elif action == "reset-confirm":
        window, peer, account_limit = (
            settings.auth_login_window_seconds,
            settings.auth_login_peer_limit,
            settings.auth_login_account_limit,
        )
    elif action == "register":
        window, peer, account_limit = (
            settings.auth_registration_window_seconds,
            settings.auth_registration_peer_limit,
            settings.auth_registration_peer_limit,
        )
    else:
        raise ValueError("Unknown identity admission action")
    consume_budget(
        action=action,
        dimension="peer",
        identifier=request.client.host,
        limit=peer,
        window_seconds=window,
    )
    if account is not None:
        consume_budget(
            action=action,
            dimension="account",
            identifier=account,
            limit=account_limit,
            window_seconds=window,
        )
    if action == "register":
        consume_budget(
            action=action,
            dimension="global",
            identifier="tenant-creation",
            limit=settings.auth_registration_global_limit,
            window_seconds=86400,
        )
