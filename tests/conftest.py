from __future__ import annotations

import os
from collections.abc import Iterator
from types import SimpleNamespace

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
    from orchestrator.core.product_events import reset_event_store_for_tests

    fake_event_store = SimpleNamespace(
        execute=lambda *_args, **_kwargs: "",
        query_events=lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr("orchestrator.core.product_events.event_store", lambda: fake_event_store)
    reset_event_store_for_tests()
    yield
    reset_event_store_for_tests()
