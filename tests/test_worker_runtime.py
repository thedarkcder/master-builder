from __future__ import annotations

import asyncio
import logging
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


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

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
        return False


class _FakePsycopg:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def connect(self, _dsn: str, autocommit: bool = True) -> _FakeConn:  # noqa: ARG002
        return self._conn


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
        bridge._run()
        loop.run_until_complete(asyncio.sleep(0))
        self.assertTrue(wake_event.is_set())
        logger.error.assert_called_once()
        loop.close()

    def test_bridge_run_listen_and_notifications_set_wake_event(self) -> None:
        from orchestrator.core.worker.queue_listener import RunQueueNotificationBridge

        loop = asyncio.new_event_loop()
        wake_event = asyncio.Event()
        conn = _FakeConn(notifications=[object(), object()])
        bridge = RunQueueNotificationBridge(
            postgres_dsn="postgres://x",
            wake_event=wake_event,
            loop=loop,
            logger=logging.getLogger("test"),
            notify_channel="run_queue",
            psycopg_module=_FakePsycopg(conn),
        )
        bridge._run()
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
    def test_process_next_queued_run_passes_send_discord_fn(self) -> None:
        import orchestrator.worker as worker_module

        process_mock = MagicMock(return_value=None)
        with patch.object(worker_module, "_process_next_queued_run_with_dependencies", new=process_mock):
            result = worker_module.process_next_queued_run(MagicMock(), MagicMock())
        self.assertIsNone(result)
        self.assertIn("send_discord_message_fn", process_mock.call_args.kwargs)

    def test_run_worker_requires_postgres(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = MagicMock()
        fake_settings.database_url = "sqlite:///test.db"
        fake_settings.log_level = "INFO"
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
        run_worker_mock.assert_called_once_with()

    def test_run_worker_processes_and_stops_cleanly(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(database_url="postgresql://localhost/db", log_level="INFO")
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
        wait_calls = {"count": 0}

        async def _wait_for_wake_or_stop(*, wake_event, stop_event):  # noqa: ANN001
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return
            stop_event.set()

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=_session_factory),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "build_workflow_runner_for_session", return_value=MagicMock()),
            patch.object(worker_module, "process_next_queued_run", new=process_mock),
        ):
            asyncio.run(worker_module.run_worker())

        self.assertEqual(process_mock.call_count, 2)
        listener.start.assert_called_once()
        listener.stop.assert_called_once()

    def test_run_worker_raises_runtime_unavailable_when_runner_build_fails(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(database_url="postgresql://localhost/db", log_level="INFO")
        session = MagicMock()

        class _SessionCtx:
            def __enter__(self):  # noqa: ANN204
                return session

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        def _session_factory():  # noqa: ANN202
            return _SessionCtx()

        listener = MagicMock()
        build_runner_mock = MagicMock(side_effect=worker_module.CodexRuntimeError("missing runtime"))
        get_settings_mock = MagicMock(return_value=fake_settings)
        configure_logging_mock = MagicMock()
        create_session_factory_mock = MagicMock(return_value=_session_factory)
        is_postgres_mock = MagicMock(return_value=True)
        postgres_dsn_mock = MagicMock(return_value="postgres://dsn")
        queue_bridge_mock = MagicMock(return_value=listener)

        async def _wait_for_wake_or_stop(*, wake_event, stop_event):  # noqa: ANN001
            wake_event.set()

        with (
            patch.object(worker_module, "get_settings", new=get_settings_mock),
            patch.object(worker_module, "configure_logging", new=configure_logging_mock),
            patch.object(worker_module, "create_session_factory", new=create_session_factory_mock),
            patch.object(worker_module, "is_postgres_database_url", new=is_postgres_mock),
            patch.object(worker_module, "postgres_dsn_from_database_url", new=postgres_dsn_mock),
            patch.object(worker_module, "RunQueueNotificationBridge", new=queue_bridge_mock),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "build_workflow_runner_for_session", new=build_runner_mock),
        ):
            with self.assertRaisesRegex(RuntimeError, "Worker runtime unavailable"):
                asyncio.run(worker_module.run_worker())

        listener.stop.assert_called_once()


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
