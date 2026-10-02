from __future__ import annotations

import secrets

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from orchestrator.api.url_helpers import resolve_public_base_url
from orchestrator.core.config import Settings


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("localhost", 60001),
            "path": "/api/public/password-reset/request",
            "headers": [
                (b"host", b"attacker.example"),
                (b"x-forwarded-host", b"attacker.example"),
                (b"x-forwarded-proto", b"https"),
            ],
        }
    )


@pytest.mark.parametrize("field", ["admin_token_secret", "auth_token_secret"])
def test_authentication_signing_secret_has_no_default(
    monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    monkeypatch.delenv(f"ORCHESTRATOR_{field.upper()}", raising=False)
    credentials = {
        "admin_password": "test-only-password",
        "admin_token_secret": secrets.token_urlsafe(32),
        "auth_token_secret": secrets.token_urlsafe(32),
    }
    credentials.pop(field)
    with pytest.raises(ValidationError, match=field):
        Settings(**credentials)


@pytest.mark.parametrize("field", ["admin_token_secret", "auth_token_secret"])
@pytest.mark.parametrize(
    "value", ["", "change-me", " " * 32, "local-dev-admin-token-secret"]
)
def test_authentication_signing_secret_rejects_weak_configuration(
    field: str, value: str
) -> None:
    credentials = {
        "admin_password": "test-only-password",
        "admin_token_secret": secrets.token_urlsafe(32),
        "auth_token_secret": secrets.token_urlsafe(32),
    }
    credentials[field] = value
    with pytest.raises(ValidationError, match=field):
        Settings(**credentials)


def test_authentication_signing_secrets_accept_explicit_random_configuration() -> None:
    settings = Settings(
        admin_password="test-only-password",
        admin_token_secret=secrets.token_urlsafe(32),
        auth_token_secret=secrets.token_urlsafe(32),
    )
    assert len(settings.admin_token_secret) >= 32
    assert len(settings.auth_token_secret) >= 32


@pytest.mark.parametrize(
    "configured", ["http://localhost:60002/", "https://public.example/"]
)
def test_security_links_ignore_attacker_controlled_host_headers(
    configured: str,
) -> None:
    assert resolve_public_base_url(
        request=_request(), configured_base_url=configured
    ) == configured.rstrip("/")


@pytest.mark.parametrize(
    "configured",
    [
        "",
        "ftp://public.example",
        "https://user:password@public.example",
        "https://public.example?redirect=other",
        "https://public.example#fragment",
        "http://",
        "//public.example",
    ],
)
def test_security_links_fail_without_valid_configured_origin(configured: str) -> None:
    with pytest.raises(ValueError, match="configured"):
        resolve_public_base_url(request=_request(), configured_base_url=configured)


@pytest.mark.parametrize(
    "field", ["public_api_base_url", "admin_ui_base_url", "cors_origins"]
)
def test_external_security_origins_require_https(field):
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(**{field: "http://public.example"})


@pytest.mark.parametrize("hosts", ["*", "localhost,*", "", "*.example.com"])
def test_trusted_ingress_hosts_require_explicit_names(hosts):
    with pytest.raises(ValidationError, match="trusted"):
        Settings(trusted_hosts=hosts)
