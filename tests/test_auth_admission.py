from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text

from orchestrator.core.config import get_settings
from orchestrator.storage.db import create_session_factory


def test_registration_disabled_before_allocation():
    from orchestrator.api.main import app

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/api/public/register",
            json={
                "email": "new@example.invalid",
                "password": "example-password-only",
                "full_name": "Example",
                "tenant_name": "Example",
            },
        )
    assert response.status_code == 403


def test_shared_budget_atomic_and_contains_no_raw_identifiers():
    from orchestrator.api.auth_admission import consume_budget

    barrier = Barrier(6)

    def attempt():
        barrier.wait()
        try:
            consume_budget(
                action="login",
                dimension="account",
                identifier="Example@Example.invalid",
                limit=3,
                window_seconds=60,
                now=120,
            )
            return True
        except HTTPException as exc:
            assert exc.status_code == 429
            assert exc.headers == {"Retry-After": "60"}
            return False

    with ThreadPoolExecutor(max_workers=6) as executor:
        assert sum(executor.map(lambda _: attempt(), range(6))) == 3
    with create_session_factory()() as session:
        rows = session.execute(
            text("SELECT bucket_key, attempts FROM auth_request_budgets")
        ).all()
    assert len(rows) == 1 and rows[0].attempts == 3
    assert len(rows[0].bucket_key) == 64
    assert "example" not in rows[0].bucket_key
    consume_budget(
        action="login",
        dimension="account",
        identifier="Example@Example.invalid",
        limit=3,
        window_seconds=60,
        now=180,
    )


def test_failed_login_persists_account_budget(monkeypatch):
    from orchestrator.api.main import app

    monkeypatch.setenv("ORCHESTRATOR_AUTH_LOGIN_ACCOUNT_LIMIT", "2")
    get_settings.cache_clear()
    with TestClient(app, raise_server_exceptions=False) as client:
        results = [
            client.post(
                "/api/app/auth/login",
                json={"email": "absent@example.invalid", "password": "invalid"},
            )
            for _ in range(3)
        ]
    assert [r.status_code for r in results] == [401, 401, 429]
    assert int(results[-1].headers["retry-after"]) > 0


def test_budget_database_failure_is_closed(monkeypatch):
    from orchestrator.api.auth_admission import consume_budget

    monkeypatch.setenv(
        "ORCHESTRATOR_DATABASE_URL", "sqlite:///file:unavailable?mode=ro&uri=true"
    )
    get_settings.cache_clear()
    with pytest.raises(HTTPException) as error:
        consume_budget(
            action="login",
            dimension="peer",
            identifier="127.0.0.1",
            limit=3,
            window_seconds=60,
            now=120,
        )
    assert error.value.status_code == 503


def test_auth_ingress_bounds_streamed_body_before_json_parsing():
    from orchestrator.api.main import app

    response = TestClient(app).post(
        "/api/app/auth/login",
        content=(b"x" * 8192 for _ in range(3)),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_reset_budget_does_not_disclose_account_existence(monkeypatch):
    from orchestrator.api.main import app

    monkeypatch.setenv("ORCHESTRATOR_AUTH_RESET_ACCOUNT_LIMIT", "2")
    get_settings.cache_clear()
    client = TestClient(app)
    responses = [
        client.post(
            "/api/public/password-reset/request",
            json={"email": "absent@example.invalid"},
        )
        for _ in range(3)
    ]
    assert [r.status_code for r in responses] == [202, 202, 429]


def test_admin_login_budget_is_enforced(monkeypatch):
    from orchestrator.api.main import app

    monkeypatch.setenv("ORCHESTRATOR_AUTH_LOGIN_ACCOUNT_LIMIT", "2")
    get_settings.cache_clear()
    client = TestClient(app)
    responses = [
        client.post(
            "/api/admin/auth/login", json={"username": "absent", "password": "invalid"}
        )
        for _ in range(3)
    ]
    assert [r.status_code for r in responses] == [401, 401, 429]


def test_registration_global_quota_is_shared_across_distinct_accounts(monkeypatch):
    from orchestrator.api.main import app

    monkeypatch.setenv("ORCHESTRATOR_PUBLIC_REGISTRATION_ENABLED", "true")
    monkeypatch.setenv("ORCHESTRATOR_AUTH_REGISTRATION_GLOBAL_LIMIT", "1")
    get_settings.cache_clear()
    client = TestClient(app)
    responses = [
        client.post(
            "/api/public/register",
            json=dict(
                email=f"{name}@example.invalid",
                password="example-password-only",
                full_name="Example",
                tenant_name=name,
            ),
        )
        for name in ("first", "second")
    ]
    assert [r.status_code for r in responses] == [201, 429]


def test_forwarded_host_and_peer_headers_do_not_bypass_default_ingress():
    from orchestrator.api.main import app

    response = TestClient(app).get(
        "/health",
        headers={
            "Host": "untrusted.example",
            "X-Forwarded-Host": "localhost",
            "X-Forwarded-For": "127.0.0.1",
        },
    )
    assert response.status_code == 400
