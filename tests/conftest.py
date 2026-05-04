from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(autouse=True)
def _isolate_test_environment() -> Iterator[None]:
    """Restore process env after every test to prevent cross-test leakage."""
    before = dict(os.environ)
    from orchestrator.core.config import get_settings

    get_settings.cache_clear()
    yield

    after_keys = set(os.environ)
    before_keys = set(before)
    for key in after_keys - before_keys:
        os.environ.pop(key, None)
    for key in before_keys:
        current = os.environ.get(key)
        expected = before[key]
        if current != expected:
            os.environ[key] = expected

    # Ensure settings reads in the next test reflect restored env values.
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _isolate_product_event_store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Unit tests use explicit fakes for ClickHouse-backed product events."""
    from orchestrator.core.observability.repository import configure_product_event_repository_for_tests, reset_product_event_repository_for_tests

    class _FakeProductEventRepository:
        def initialize(self) -> None:
            return None

        def insert_event(self, row) -> None:  # noqa: ANN001
            return None

        def list_events(self, **_kwargs):  # noqa: ANN003
            return []

        def list_events_after_sequence(self, **_kwargs):  # noqa: ANN003
            return []

    configure_product_event_repository_for_tests(_FakeProductEventRepository())
    monkeypatch.setattr("orchestrator.core.observability.writer.publish_product_event_notification", lambda **_kwargs: None)
    yield
    reset_product_event_repository_for_tests()
