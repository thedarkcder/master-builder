from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.core.discord.gateway_runtime import (
    DiscordGatewayDependencyFailure,
    _leader_lock_healthcheck,
    _run_gateway_leader_loop,
    _try_acquire_leader_lock,
)


class _CursorCtx:
    def __init__(self, fetchone_value):
        self._fetchone_value = fetchone_value
        self.execute_calls: list[tuple[str, tuple]] = []

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False

    def execute(self, query: str, params: tuple = ()) -> None:
        self.execute_calls.append((query, params))

    def fetchone(self):  # noqa: ANN201
        return self._fetchone_value


class _Connection:
    def __init__(self, cursor_ctx: _CursorCtx):
        self._cursor_ctx = cursor_ctx

    def cursor(self):  # noqa: ANN201
        return self._cursor_ctx


class _ConnectionContext:
    def __init__(self, conn: _Connection):
        self._conn = conn

    def __enter__(self):  # noqa: ANN204
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False


class _FakeEvent:
    def __init__(self) -> None:
        self._set = False
        self.wait_calls = 0

    def set(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set

    def wait(self, timeout: float | None = None) -> bool:
        _ = timeout
        self.wait_calls += 1
        if self.wait_calls >= 2:
            self._set = True
        return self._set


class DiscordGatewayRuntimeTests(unittest.TestCase):
    def test_run_discord_gateway_registers_command_executor_before_loop(self) -> None:
        settings = SimpleNamespace(
            log_level="INFO",
            sentry_environment="test",
            sentry_release="dev-local",
        )
        with (
            patch(
                "orchestrator.core.discord.gateway_runtime.get_settings",
                return_value=settings,
            ),
            patch("orchestrator.core.discord.gateway_runtime.configure_logging"),
            patch(
                "orchestrator.core.discord.gateway_runtime.register_discord_command_executor"
            ) as register_mock,
            patch(
                "orchestrator.core.discord.gateway_runtime._run_gateway_leader_loop"
            ) as run_loop_mock,
        ):
            from orchestrator.core.discord.gateway_runtime import run_discord_gateway

            run_discord_gateway()

        register_mock.assert_called_once_with()
        run_loop_mock.assert_called_once_with(settings=settings)

    def test_try_acquire_leader_lock_true_when_pg_returns_true(self) -> None:
        cursor = _CursorCtx((True,))
        conn = _Connection(cursor)
        self.assertTrue(_try_acquire_leader_lock(conn=conn, lock_key=1234))
        self.assertEqual(cursor.execute_calls[0][0], "SELECT pg_try_advisory_lock(%s)")
        self.assertEqual(cursor.execute_calls[0][1], (1234,))

    def test_leader_lock_healthcheck_runs_select_one(self) -> None:
        cursor = _CursorCtx((1,))
        conn = _Connection(cursor)
        self.assertTrue(_leader_lock_healthcheck(conn=conn))
        self.assertEqual(cursor.execute_calls[0][0], "SELECT 1")

    def test_leader_loop_requires_postgres(self) -> None:
        settings = SimpleNamespace(
            database_url="sqlite:///tmp/test.db",
            discord_gateway_lock_key=1,
            discord_gateway_poll_seconds=1,
            secrets_encryption_key="enc",
        )
        with self.assertRaises(DiscordGatewayDependencyFailure):
            _run_gateway_leader_loop(settings=settings)

    def test_leader_loop_starts_listener_only_when_lock_acquired(self) -> None:
        settings = SimpleNamespace(
            database_url="postgresql+psycopg://user:pass@localhost:5432/db",
            discord_gateway_lock_key=99,
            discord_gateway_poll_seconds=1,
            secrets_encryption_key="enc",
        )
        listener = MagicMock()
        fake_conn = _Connection(object())  # type: ignore[arg-type]
        fake_event = _FakeEvent()

        with (
            patch(
                "orchestrator.core.discord.gateway_runtime.is_postgres_database_url",
                return_value=True,
            ),
            patch(
                "orchestrator.core.discord.gateway_runtime.postgres_dsn_from_database_url",
                return_value="dsn",
            ),
            patch(
                "orchestrator.core.discord.gateway_runtime.psycopg",
                SimpleNamespace(connect=lambda *a, **k: _ConnectionContext(fake_conn)),
            ),
            patch(
                "orchestrator.core.discord.gateway_runtime.DiscordGatewayListener",
                return_value=listener,
            ),
            patch(
                "orchestrator.core.discord.gateway_runtime._try_acquire_leader_lock",
                side_effect=[False, True],
            ),
            patch(
                "orchestrator.core.discord.gateway_runtime._leader_lock_healthcheck",
                side_effect=[True, RuntimeError("lost")],
            ),
            patch(
                "orchestrator.core.discord.gateway_runtime.threading.Event",
                return_value=fake_event,
            ),
            patch("orchestrator.core.discord.gateway_runtime.signal.signal"),
        ):
            _run_gateway_leader_loop(settings=settings)

        listener.start.assert_called_once_with()
        self.assertGreaterEqual(listener.stop.call_count, 1)


if __name__ == "__main__":
    unittest.main()
