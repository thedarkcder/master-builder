from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator.api.main import create_app
from orchestrator.core.sentry import (
    _reset_sentry_initialization_for_tests,
    initialize_sentry,
)


class SentryInitializationTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_sentry_initialization_for_tests()

    def tearDown(self) -> None:
        _reset_sentry_initialization_for_tests()

    def test_initialize_sentry_returns_false_without_dsn(self) -> None:
        settings = SimpleNamespace(
            sentry_dsn="",
            sentry_environment="dev",
            sentry_release="",
            sentry_traces_sample_rate=0.0,
        )
        self.assertFalse(initialize_sentry(settings=settings, init_fn=lambda **_: None))

    def test_initialize_sentry_calls_init_when_dsn_present(self) -> None:
        settings = SimpleNamespace(
            sentry_dsn="https://examplePublicKey@o0.ingest.sentry.io/0",
            sentry_environment="staging",
            sentry_release="v0.1.0",
            sentry_traces_sample_rate=0.25,
        )

        captured: dict[str, object] = {}

        def _fake_init(**kwargs):  # type: ignore[no-untyped-def]
            captured.update(kwargs)

        initialized = initialize_sentry(settings=settings, init_fn=_fake_init)
        self.assertTrue(initialized)
        self.assertEqual(captured["dsn"], settings.sentry_dsn)
        self.assertEqual(captured["environment"], "staging")
        self.assertEqual(captured["release"], "v0.1.0")
        self.assertEqual(captured["traces_sample_rate"], 0.25)

    def test_initialize_sentry_is_idempotent_for_same_process_settings(self) -> None:
        settings = SimpleNamespace(
            sentry_dsn="https://examplePublicKey@o0.ingest.sentry.io/0",
            sentry_environment="staging",
            sentry_release="v0.1.0",
            sentry_traces_sample_rate=0.25,
        )
        calls = 0

        def _fake_init(**_kwargs):  # type: ignore[no-untyped-def]
            nonlocal calls
            calls += 1

        self.assertTrue(initialize_sentry(settings=settings, init_fn=_fake_init))
        self.assertTrue(initialize_sentry(settings=settings, init_fn=_fake_init))
        self.assertEqual(calls, 1)

    def test_initialize_sentry_rejects_changed_settings_after_initialization(
        self,
    ) -> None:
        settings = SimpleNamespace(
            sentry_dsn="https://examplePublicKey@o0.ingest.sentry.io/0",
            sentry_environment="staging",
            sentry_release="v0.1.0",
            sentry_traces_sample_rate=0.25,
        )
        changed_settings = SimpleNamespace(
            sentry_dsn="https://examplePublicKey@o0.ingest.sentry.io/0",
            sentry_environment="production",
            sentry_release="v0.1.0",
            sentry_traces_sample_rate=0.25,
        )

        self.assertTrue(initialize_sentry(settings=settings, init_fn=lambda **_: None))
        with self.assertRaisesRegex(
            RuntimeError, "already been initialized with different settings"
        ):
            initialize_sentry(settings=changed_settings, init_fn=lambda **_: None)

    def test_create_app_initializes_sentry(self) -> None:
        with patch("orchestrator.api.main.initialize_sentry") as sentry_init:
            create_app()
        sentry_init.assert_called_once()
