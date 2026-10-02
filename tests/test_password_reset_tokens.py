from __future__ import annotations

from datetime import UTC, datetime

from orchestrator.core.platform.password_reset_tokens import (
    issue_password_reset_token,
    parse_password_reset_token,
)


def test_issue_password_reset_token_treats_naive_password_updated_at_as_utc() -> None:
    token = issue_password_reset_token(
        user_id="user-1",
        email="owner@example.com",
        password_updated_at=datetime(2026, 4, 7, 12, 0, 0),
        secret="secret",
    )

    payload = parse_password_reset_token(token=token, secret="secret")

    assert (
        payload.password_updated_at
        == datetime(2026, 4, 7, 12, 0, 0, tzinfo=UTC).isoformat()
    )
