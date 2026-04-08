from __future__ import annotations

import asyncio
import logging
import threading
import unittest
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import WorkerRuntimeState
from tests.workflow_test_support import add_workflow_attempt


class _FakeConn:
    def __init__(self, notifications: list[object] | None = None) -> None:
        self._notifications = notifications or []
        self.executed: list[str] = []
        self.closed = False

    def execute(self, query: str) -> None:
        self.executed.append(query)

    def notifies(self):  # noqa: ANN204
        for item in self._notifications:
            yield item

    def close(self) -> None:
        self.closed = True

    def __enter__(self):  # noqa: ANN204
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakePsycopg:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self, _dsn: str, autocommit: bool = True) -> _FakeConn:  # noqa: ARG002
        return self._conn


class _FlakyPsycopg:
    def __init__(self, *results: object) -> None:
        self._results = list(results)
        self.connect_calls = 0

    def connect(self, _dsn: str, autocommit: bool = True):  # noqa: ANN202, ARG002
        self.connect_calls += 1
        if not self._results:
            raise RuntimeError("no connection result configured")
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _StopAfterFirstNotifyConn(_FakeConn):
    def __init__(self, *, stop_event: threading.Event) -> None:
        super().__init__()
        self._stop_event = stop_event

    def notifies(self):  # noqa: ANN204
        yield object()
        self._stop_event.set()


class QueueListenerTests(unittest.TestCase):
    def test_bridge_run_with_missing_psycopg_sets_wake_event(self) -> None:
        from orchestrator.core.worker.queue_listener import RunQueueNotificationBridge

        loop = asyncio.new_event_loop()
        wake_event = asyncio.Event()
        logger = MagicMock()
        bridge = RunQueueNotificationBridge(
            postgres_dsn="postgres://x",
            wake_event=wake_event,
            loop=loop,
            logger=logger,
            notify_channel="run_queue",
            psycopg_module=None,
        )
        bridge._run_once()
        loop.run_until_complete(asyncio.sleep(0))
        self.assertTrue(wake_event.is_set())
        logger.error.assert_called_once()
        loop.close()

    def test_bridge_run_listen_and_notifications_set_wake_event(self) -> None:
        from orchestrator.core.worker.queue_listener import RunQueueNotificationBridge

        loop = asyncio.new_event_loop()
        wake_event = asyncio.Event()
        bridge = RunQueueNotificationBridge(
            postgres_dsn="postgres://x",
            wake_event=wake_event,
            loop=loop,
            logger=logging.getLogger("test"),
            notify_channel="run_queue",
            psycopg_module=None,
        )
        conn = _StopAfterFirstNotifyConn(stop_event=bridge._stop_event)
        bridge._psycopg = _FakePsycopg(conn)
        bridge._run_once()
        loop.run_until_complete(asyncio.sleep(0))
        self.assertIn('LISTEN "run_queue"', conn.executed[0])
        self.assertTrue(wake_event.is_set())
        loop.close()

    def test_bridge_stop_closes_connection(self) -> None:
        from orchestrator.core.worker.queue_listener import RunQueueNotificationBridge

        loop = asyncio.new_event_loop()
        wake_event = asyncio.Event()
        conn = _FakeConn()
        bridge = RunQueueNotificationBridge(
            postgres_dsn="postgres://x",
            wake_event=wake_event,
            loop=loop,
            logger=logging.getLogger("test"),
            notify_channel="run_queue",
            psycopg_module=_FakePsycopg(conn),
        )
        bridge._conn = conn
        bridge._thread = threading.Thread(target=lambda: None)
        bridge.stop()
        self.assertTrue(conn.closed)
        loop.close()

    def test_bridge_reconnects_after_listener_failure(self) -> None:
        from orchestrator.core.worker import queue_listener as queue_listener_module

        loop = asyncio.new_event_loop()
        wake_event = asyncio.Event()
        logger = MagicMock()
        bridge = queue_listener_module.RunQueueNotificationBridge(
            postgres_dsn="postgres://x",
            wake_event=wake_event,
            loop=loop,
            logger=logger,
            notify_channel="run_queue",
            psycopg_module=None,
        )
        conn = _StopAfterFirstNotifyConn(stop_event=bridge._stop_event)
        bridge._psycopg = _FlakyPsycopg(RuntimeError("db restarted"), conn)

        with patch.object(queue_listener_module, "RECONNECT_DELAY_SECONDS", 0):
            bridge._run()

        loop.run_until_complete(asyncio.sleep(0))
        self.assertTrue(wake_event.is_set())
        self.assertEqual(bridge._psycopg.connect_calls, 2)
        logger.exception.assert_called_once()
        loop.close()

    def test_wait_for_wake_or_stop(self) -> None:
        from orchestrator.core.worker.queue_listener import wait_for_wake_or_stop

        async def _run() -> tuple[bool, bool]:
            wake = asyncio.Event()
            stop = asyncio.Event()
            wake.set()
            await wait_for_wake_or_stop(wake_event=wake, stop_event=stop)
            first = wake.is_set()
            wake.clear()
            stop.set()
            await wait_for_wake_or_stop(wake_event=wake, stop_event=stop)
            return first, stop.is_set()

        result = asyncio.run(_run())
        self.assertEqual(result, (True, True))


class RuntimeFactoryTests(unittest.TestCase):
    def test_build_workflow_runner_for_session(self) -> None:
        from orchestrator.core.worker.runtime_factory import build_workflow_runner_for_session

        session = MagicMock()
        settings = MagicMock()
        settings.database_url = "sqlite:///test.db"
        with (
            patch("orchestrator.core.worker.runtime_factory.get_settings", return_value=settings) as settings_mock,
            patch("orchestrator.core.worker.runtime_factory.build_codex_runtime", return_value=MagicMock()) as runtime_mock,
            patch(
                "orchestrator.core.worker.runtime_factory.OrchestratedRunWorkflowExecutor",
                return_value=MagicMock(),
            ) as agents_mock,
            patch("orchestrator.core.worker.runtime_factory.WorkflowRunner", return_value=MagicMock()) as runner_mock,
        ):
            runner = build_workflow_runner_for_session(session=session)
        settings_mock.assert_called_once()
        runtime_mock.assert_called_once()
        agents_mock.assert_called_once()
        runner_mock.assert_called_once()
        self.assertIsNotNone(runner)


class WorkerTests(unittest.TestCase):
    def test_process_next_run_once_does_not_touch_webhook_queue(self) -> None:
        import orchestrator.worker as worker_module

        run_session = MagicMock(name="run-session")

        class _SessionCtx:
            def __init__(self, current_session) -> None:  # noqa: ANN001
                self._session = current_session

            def __enter__(self):  # noqa: ANN204
                return self._session

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        def _session_factory():  # noqa: ANN202
            return _SessionCtx(run_session)

        with (
            patch.object(worker_module, "_process_next_webhook_job_with_dependencies") as webhook_mock,
            patch.object(worker_module, "build_workflow_runner_for_session", return_value=MagicMock()) as runner_mock,
            patch.object(worker_module, "process_next_queued_run", return_value=None) as run_mock,
        ):
            worker_module._process_next_run_once(session_factory=_session_factory)

        webhook_mock.assert_not_called()
        runner_mock.assert_called_once_with(session=run_session)
        run_mock.assert_called_once_with(run_session, runner_mock.return_value)

    def test_process_next_queued_run_passes_send_discord_fn(self) -> None:
        import orchestrator.worker as worker_module

        process_mock = MagicMock(return_value=None)
        with patch.object(worker_module, "_process_next_queued_run_with_dependencies", new=process_mock):
            result = worker_module.process_next_queued_run(MagicMock(), MagicMock())
        self.assertIsNone(result)
        self.assertIn("send_discord_message_fn", process_mock.call_args.kwargs)

    def test_run_worker_webhooks_skips_stale_recovery(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
        )
        listener = MagicMock()
        wait_calls = {"count": 0}
        spawned_modes: list[str] = []

        async def _wait_for_wake_or_stop(*, wake_event: asyncio.Event, stop_event: asyncio.Event) -> None:
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return
            stop_event.set()

        async def _spawn_worker_child_process(*, mode: str, wake_event: asyncio.Event):
            _ = wake_event
            spawned_modes.append(mode)
            child_result = worker_module.WorkerChildProcessResult(
                return_code=worker_module.WORKER_CHILD_EXIT_IDLE,
                processed=False,
                dependency_failure=False,
            )
            process = SimpleNamespace(pid=321, returncode=child_result.return_code, terminate=lambda: None)
            return worker_module.WorkerChildProcessHandle(
                process=process,
                wait_task=asyncio.create_task(asyncio.sleep(0, result=child_result)),
            )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once") as recovery_mock,
            patch.object(worker_module, "_run_stale_recovery_loop") as stale_loop_mock,
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            asyncio.run(worker_module.run_worker(mode="webhooks"))

        self.assertIn("webhooks", spawned_modes)
        recovery_mock.assert_not_called()
        stale_loop_mock.assert_not_called()
        listener.start.assert_called_once()
        listener.stop.assert_called_once()

    def test_run_worker_runs_uses_run_processor(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
        )
        listener = MagicMock()
        purge_mock = MagicMock()
        spawned_modes: list[str] = []
        wait_calls = {"count": 0}

        async def _wait_for_wake_or_stop(*, wake_event: asyncio.Event, stop_event: asyncio.Event) -> None:
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return
            stop_event.set()

        async def _spawn_worker_child_process(*, mode: str, wake_event: asyncio.Event):
            _ = wake_event
            spawned_modes.append(mode)
            child_result = worker_module.WorkerChildProcessResult(
                return_code=worker_module.WORKER_CHILD_EXIT_IDLE,
                processed=False,
                dependency_failure=False,
            )
            process = SimpleNamespace(pid=456, returncode=child_result.return_code, terminate=lambda: None)
            return worker_module.WorkerChildProcessHandle(
                process=process,
                wait_task=asyncio.create_task(asyncio.sleep(0, result=child_result)),
            )

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _archived_tenant_purge_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once") as recovery_mock,
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "_purge_archived_tenants_once", new=purge_mock),
            patch.object(worker_module, "_run_archived_tenant_purge_loop", new=_archived_tenant_purge_loop),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            asyncio.run(worker_module.run_worker(mode="runs"))

        self.assertIn("runs", spawned_modes)
        recovery_mock.assert_called_once()
        purge_mock.assert_called_once()
        listener.start.assert_called_once()
        listener.stop.assert_called_once()

    def test_run_worker_requires_postgres(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = MagicMock()
        fake_settings.database_url = "sqlite:///test.db"
        fake_settings.log_level = "INFO"
        fake_settings.sentry_environment = "test"
        fake_settings.sentry_release = None
        fake_settings.agent_id = "worker-test"
        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory"),
            patch.object(worker_module, "is_postgres_database_url", return_value=False),
        ):
            with self.assertRaisesRegex(RuntimeError, "requires PostgreSQL"):
                asyncio.run(worker_module.run_worker())

    def test_main_runs_asyncio_worker(self) -> None:
        import orchestrator.worker as worker_module

        run_worker_mock = MagicMock(return_value="coro-token")
        with (
            patch.object(worker_module, "run_worker", new=run_worker_mock),
            patch("orchestrator.worker.asyncio.run") as run_mock,
        ):
            worker_module.main()
        run_mock.assert_called_once()
        run_worker_mock.assert_called_once_with(mode="runs")

    def test_run_worker_child_once_returns_expected_exit_codes(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
        )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "_resolve_worker_processor", return_value=MagicMock(return_value=object())),
        ):
            self.assertEqual(
                worker_module.run_worker_child_once(mode="runs"),
                worker_module.WORKER_CHILD_EXIT_PROCESSED,
            )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "_resolve_worker_processor", return_value=MagicMock(return_value=None)),
        ):
            self.assertEqual(
                worker_module.run_worker_child_once(mode="webhooks"),
                worker_module.WORKER_CHILD_EXIT_IDLE,
            )

    def test_resolve_worker_child_capacity_applies_policy_and_runtime_cap(self) -> None:
        import orchestrator.worker as worker_module

        settings = SimpleNamespace(worker_max_child_processes=2)
        with patch.object(worker_module, "_resolve_parallel_slots_from_policy", return_value=5):
            self.assertEqual(
                worker_module._resolve_worker_child_capacity(
                    settings=settings,
                    session_factory=MagicMock(),
                ),
                2,
            )

    def test_run_worker_processes_and_stops_cleanly(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
        )
        session = MagicMock()

        class _SessionCtx:
            def __enter__(self):  # noqa: ANN204
                return session

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        def _session_factory():  # noqa: ANN202
            return _SessionCtx()

        listener = MagicMock()
        process_mock = MagicMock(side_effect=[object(), None])
        recovery_mock = MagicMock()
        wait_calls = {"count": 0}

        async def _wait_for_wake_or_stop(*, wake_event: asyncio.Event, stop_event: asyncio.Event) -> None:
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return
            stop_event.set()

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(*, mode: str, wake_event: asyncio.Event):
            _ = mode
            _ = wake_event
            token = process_mock()
            child_result = worker_module.WorkerChildProcessResult(
                return_code=(
                    worker_module.WORKER_CHILD_EXIT_PROCESSED
                    if token is not None
                    else worker_module.WORKER_CHILD_EXIT_IDLE
                ),
                processed=token is not None,
                dependency_failure=False,
            )
            process = SimpleNamespace(
                pid=1234,
                returncode=child_result.return_code,
                terminate=lambda: None,
            )
            return worker_module.WorkerChildProcessHandle(
                process=process,
                wait_task=asyncio.create_task(asyncio.sleep(0, result=child_result)),
            )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=_session_factory),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once", new=recovery_mock),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            asyncio.run(worker_module.run_worker())

        self.assertEqual(process_mock.call_count, 2)
        recovery_mock.assert_called_once()
        listener.start.assert_called_once()
        listener.stop.assert_called_once()

    def test_run_worker_uses_policy_parallel_slots(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
        )
        session = MagicMock()

        class _SessionCtx:
            def __enter__(self):  # noqa: ANN204
                return session

            def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
                return False

        def _session_factory():  # noqa: ANN202
            return _SessionCtx()

        listener = MagicMock()
        process_mock = MagicMock(return_value=None)
        recovery_mock = MagicMock()
        wait_calls = {"count": 0}

        async def _wait_for_wake_or_stop(*, wake_event: asyncio.Event, stop_event: asyncio.Event) -> None:
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return
            stop_event.set()

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(*, mode: str, wake_event: asyncio.Event):
            _ = mode
            _ = wake_event
            token = process_mock()
            child_result = worker_module.WorkerChildProcessResult(
                return_code=(
                    worker_module.WORKER_CHILD_EXIT_PROCESSED
                    if token is not None
                    else worker_module.WORKER_CHILD_EXIT_IDLE
                ),
                processed=token is not None,
                dependency_failure=False,
            )
            process = SimpleNamespace(pid=901, returncode=child_result.return_code, terminate=lambda: None)
            return worker_module.WorkerChildProcessHandle(
                process=process,
                wait_task=asyncio.create_task(asyncio.sleep(0, result=child_result)),
            )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=_session_factory),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=2) as slots_mock,
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once", new=recovery_mock),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            asyncio.run(worker_module.run_worker())

        self.assertTrue(slots_mock.called)
        self.assertGreaterEqual(process_mock.call_count, 2)
        listener.start.assert_called_once()
        listener.stop.assert_called_once()

    def test_run_worker_recovers_after_worker_child_failure(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
        )

        listener = MagicMock()
        recovery_mock = MagicMock()
        child_spawns = {"count": 0}

        async def _wait_for_wake_or_stop(*, wake_event: asyncio.Event, stop_event: asyncio.Event) -> None:
            if child_spawns["count"] >= 2:
                stop_event.set()
                return
            wake_event.set()

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(*, mode: str, wake_event: asyncio.Event):
            _ = mode
            _ = wake_event
            child_spawns["count"] += 1
            return_code = (
                worker_module.WORKER_CHILD_EXIT_RUNTIME_FAILURE
                if child_spawns["count"] == 1
                else worker_module.WORKER_CHILD_EXIT_IDLE
            )
            child_result = worker_module.WorkerChildProcessResult(
                return_code=return_code,
                processed=False,
                dependency_failure=False,
            )
            process = SimpleNamespace(pid=654, returncode=child_result.return_code, terminate=lambda: None)
            return worker_module.WorkerChildProcessHandle(
                process=process,
                wait_task=asyncio.create_task(asyncio.sleep(0, result=child_result)),
            )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(worker_module, "_recover_worker_run_health_once", new=recovery_mock),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
            patch.object(worker_module.platform_metrics, "record_worker_failure") as failure_metric,
        ):
            asyncio.run(worker_module.run_worker())

        self.assertEqual(child_spawns["count"], 2)
        failure_metric.assert_any_call(kind="child_crash")
        listener.start.assert_called_once()
        listener.stop.assert_called_once()

    def test_run_worker_raises_runtime_unavailable_when_child_reports_dependency_failure(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
        )

        listener = MagicMock()
        recovery_mock = MagicMock()

        async def _wait_for_wake_or_stop(*, wake_event: asyncio.Event, stop_event: asyncio.Event) -> None:
            _ = stop_event
            wake_event.set()

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(*, mode: str, wake_event: asyncio.Event):
            _ = mode
            _ = wake_event
            child_result = worker_module.WorkerChildProcessResult(
                return_code=worker_module.WORKER_CHILD_EXIT_DEPENDENCY_FAILURE,
                processed=False,
                dependency_failure=True,
            )
            process = SimpleNamespace(pid=777, returncode=child_result.return_code, terminate=lambda: None)
            return worker_module.WorkerChildProcessHandle(
                process=process,
                wait_task=asyncio.create_task(asyncio.sleep(0, result=child_result)),
            )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once", new=recovery_mock),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Worker runtime unavailable"):
                asyncio.run(worker_module.run_worker())

        listener.stop.assert_called_once()


class WorkerRuntimeRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/worker_runtime.db"
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        reset_db_engine_cache()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()
        reset_db_engine_cache()

    def test_worker_service_instance_id_for_mode_is_sticky_by_agent_and_mode(self) -> None:
        from orchestrator.core.worker.run_health import worker_service_instance_id_for_mode

        settings = SimpleNamespace(agent_id="worker-linux-local")

        self.assertEqual(
            worker_service_instance_id_for_mode(settings=settings, mode="runs"),
            "worker-linux-local:runs",
        )
        self.assertEqual(
            worker_service_instance_id_for_mode(settings=settings, mode="webhooks"),
            "worker-linux-local:webhooks",
        )

    def test_worker_runtime_registration_refresh_and_stop(self) -> None:
        import orchestrator.worker as worker_module

        session_factory = create_session_factory(self.database_url)
        settings = SimpleNamespace(worker_capabilities="linux,macos")
        service_instance_id = "node-a:1234"

        worker_module._register_worker_runtime_once(
            session_factory=session_factory,
            settings=settings,
            agent_id="worker-a",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )

        with session_factory() as session:
            row = session.get(WorkerRuntimeState, service_instance_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.state, "starting")
            self.assertEqual(row.capabilities_json, ["linux", "macos"])

        now = datetime.now(timezone.utc)
        with session_factory() as session:
            add_workflow_attempt(
                session,
                run_id="run-1",
                tenant_id="tenant-1",
                project_id=None,
                issue_key="GP-1",
                issue_summary="Issue",
                issue_description=None,
                repo_url=None,
                created_at=now,
                run_status="running",
                workflow_status="running",
                started_at=now,
                last_heartbeat_at=now,
                worker_service_instance_id=service_instance_id,
            )
            session.commit()

        worker_module._refresh_worker_runtime_once(
            session_factory=session_factory,
            settings=settings,
            agent_id="worker-a",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )

        with session_factory() as session:
            row = session.get(WorkerRuntimeState, service_instance_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.state, "busy")
            self.assertIsNotNone(row.last_heartbeat_at)

        worker_module._stop_worker_runtime_once(
            session_factory=session_factory,
            settings=settings,
            agent_id="worker-a",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )

        with session_factory() as session:
            row = session.get(WorkerRuntimeState, service_instance_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.state, "stopped")

    def test_worker_runtime_registration_rejects_invalid_capability_settings(self) -> None:
        import orchestrator.worker as worker_module

        session_factory = create_session_factory(self.database_url)
        settings = SimpleNamespace(worker_capabilities="linux,darwin")

        with self.assertRaisesRegex(ValueError, "Invalid worker capability token\\(s\\)"):
            worker_module._register_worker_runtime_once(
                session_factory=session_factory,
                settings=settings,
                agent_id="worker-a",
                service_instance_id="node-a:1234",
                worker_mode=worker_module.WORKER_MODE_RUNS,
            )


class MainEntryTests(unittest.TestCase):
    def test_module_main_invokes_cli_main(self) -> None:
        import orchestrator.__main__ as main_module

        main_mock = MagicMock(return_value=0)
        with patch.object(main_module, "main", new=main_mock):
            result = main_module.run(["arg1"])
        self.assertEqual(result, 0)
        main_mock.assert_called_once_with(["arg1"])


if __name__ == "__main__":
    unittest.main()
