from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import select
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError

from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import WorkerRuntimeState
from orchestrator.core.worker.queue_selector import QueueClaimabilityProbe, QueueClaimabilityReason
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

        async def _run() -> tuple[bool, bool, bool]:
            wake = asyncio.Event()
            stop = asyncio.Event()
            wake.set()
            timed_out_first = await wait_for_wake_or_stop(wake_event=wake, stop_event=stop)
            first = wake.is_set()
            wake.clear()
            stop.set()
            timed_out_second = await wait_for_wake_or_stop(wake_event=wake, stop_event=stop)
            return first, stop.is_set(), timed_out_first or timed_out_second

        result = asyncio.run(_run())
        self.assertEqual(result, (True, True, False))

    def test_wait_for_wake_or_stop_timeout(self) -> None:
        from orchestrator.core.worker.queue_listener import wait_for_wake_or_stop

        async def _run() -> bool:
            wake = asyncio.Event()
            stop = asyncio.Event()
            return await wait_for_wake_or_stop(
                wake_event=wake,
                stop_event=stop,
                timeout_seconds=0.01,
            )

        self.assertTrue(asyncio.run(_run()))


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
    def test_process_next_run_once_requires_claim_metadata(self) -> None:
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

        with self.assertRaisesRegex(RuntimeError, "without claimed_run_id"):
            worker_module._process_next_run_once(
                session_factory=_session_factory,
                claimed_run_id="",
                claim_id="claim-123",
            )

    def test_process_next_run_once_builds_runner_lazily(self) -> None:
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

        runner = MagicMock()

        def _run_next(*, session, runner, run_id, claim_id, send_discord_message_fn):  # noqa: ANN001, ANN202
            _ = session
            _ = run_id
            _ = claim_id
            _ = send_discord_message_fn
            runner.run("request")
            return object()

        with (
            patch.object(worker_module, "build_workflow_runner_for_session", return_value=runner) as runner_mock,
            patch.object(worker_module, "_process_claimed_run_with_dependencies", side_effect=_run_next) as run_mock,
        ):
            worker_module._process_next_run_once(
                session_factory=_session_factory,
                claimed_run_id="run-123",
                claim_id="claim-123",
            )

        runner_mock.assert_called_once_with(session=run_session)
        run_mock.assert_called_once()
        runner.run.assert_called_once_with("request")

    def test_process_next_run_once_uses_preclaimed_run_id(self) -> None:
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

        with patch.object(worker_module, "_process_claimed_run_with_dependencies", return_value=MagicMock()) as claimed_mock:
            worker_module._process_next_run_once(
                session_factory=_session_factory,
                claimed_run_id="run-123",
                claim_id="claim-123",
            )

        claimed_mock.assert_called_once()
        self.assertEqual(claimed_mock.call_args.kwargs["run_id"], "run-123")
        self.assertEqual(claimed_mock.call_args.kwargs["claim_id"], "claim-123")

    def test_reconcile_claimed_run_after_child_exit_terminalizes_dispatching_run(self) -> None:
        import orchestrator.worker as worker_module

        dispatching_run = SimpleNamespace(run_id="run-123", status="dispatching")
        failed_run = SimpleNamespace(run_id="run-123", status="failed")
        session = MagicMock()
        session.get.return_value = dispatching_run

        class _SessionCtx:
            def __enter__(self):  # noqa: ANN204
                return session

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        def _session_factory():  # noqa: ANN202
            return _SessionCtx()

        with patch.object(worker_module, "mark_run_terminal", return_value=failed_run) as mark_terminal_mock:
            status = worker_module._reconcile_claimed_run_after_child_exit(
                session_factory=_session_factory,
                run_id="run-123",
                claim_id="claim-123",
            )

        self.assertEqual(status, "failed")
        mark_terminal_mock.assert_called_once_with(
            session,
            run_id="run-123",
            terminal_status="failed",
            last_error=worker_module._CHILD_DISPATCH_STUCK_ERROR,
            expected_claim_id="claim-123",
        )

    def test_reconcile_claimed_run_after_child_exit_returns_ownership_lost_on_claim_mismatch(self) -> None:
        import orchestrator.worker as worker_module

        dispatching_run = SimpleNamespace(run_id="run-123", status="dispatching", claim_id="claim-2")
        session = MagicMock()
        session.get.return_value = dispatching_run

        class _SessionCtx:
            def __enter__(self):  # noqa: ANN204
                return session

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        def _session_factory():  # noqa: ANN202
            return _SessionCtx()

        with patch.object(worker_module, "mark_run_terminal") as mark_terminal_mock:
            status = worker_module._reconcile_claimed_run_after_child_exit(
                session_factory=_session_factory,
                run_id="run-123",
                claim_id="claim-123",
            )

        self.assertEqual(status, "ownership_lost")
        mark_terminal_mock.assert_not_called()

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

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = timeout_seconds
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return False
            stop_event.set()
            return False

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = wake_event
            _ = child_timeout_seconds
            self.assertIsNone(claimed_run_id)
            self.assertIsNone(claim_id)
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
            patch.object(worker_module, "prewarm_knowledge_dependencies") as prewarm_mock,
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=True,
                    reason=QueueClaimabilityReason.CLAIMABLE,
                ),
            ),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once") as recovery_mock,
            patch.object(worker_module, "_run_stale_recovery_loop") as stale_loop_mock,
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            asyncio.run(worker_module.run_worker(mode="webhooks"))

        self.assertIn("webhooks", spawned_modes)
        prewarm_mock.assert_not_called()
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

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = timeout_seconds
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return False
            stop_event.set()
            return False

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = wake_event
            _ = child_timeout_seconds
            self.assertEqual(claimed_run_id, "run-1")
            self.assertEqual(claim_id, "claim-1")
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

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = mode
            _ = wake_event
            _ = child_timeout_seconds
            _ = claimed_run_id
            _ = claim_id
            child_result = worker_module.WorkerChildProcessResult(
                return_code=worker_module.WORKER_CHILD_EXIT_IDLE,
                processed=False,
                dependency_failure=False,
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
            patch.object(worker_module, "prewarm_knowledge_dependencies") as prewarm_mock,
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(
                worker_module,
                "_claim_next_run_once",
                side_effect=[
                    worker_module.ClaimedRunDispatch(
                        run_id="run-1",
                        claim_id="claim-1",
                        tenant_id="tenant-1",
                        issue_key="GP-1",
                    ),
                    None,
                ],
            ),
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=False,
                    reason=QueueClaimabilityReason.NO_QUEUED_RUNS,
                ),
            ),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once") as recovery_mock,
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "_purge_archived_tenants_once", new=purge_mock),
            patch.object(worker_module, "_run_archived_tenant_purge_loop", new=_archived_tenant_purge_loop),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            asyncio.run(worker_module.run_worker(mode="runs"))

        self.assertEqual(spawned_modes, [])
        prewarm_mock.assert_called_once_with(settings=fake_settings)
        recovery_mock.assert_called_once()
        purge_mock.assert_called_once()
        listener.start.assert_called_once()
        listener.stop.assert_called_once()

    def test_run_worker_runs_does_not_claim_when_startup_auth_is_blocked(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
        )
        listener = MagicMock()
        wait_calls = {"count": 0}

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = wake_event
            _ = timeout_seconds
            wait_calls["count"] += 1
            stop_event.set()
            return False

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _archived_tenant_purge_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = mode
            _ = wake_event
            _ = child_timeout_seconds
            _ = claimed_run_id
            _ = claim_id
            child_result = worker_module.WorkerChildProcessResult(
                return_code=worker_module.WORKER_CHILD_EXIT_IDLE,
                processed=False,
                dependency_failure=False,
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
            patch.object(worker_module, "prewarm_knowledge_dependencies"),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "_run_archived_tenant_purge_loop", new=_archived_tenant_purge_loop),
            patch.object(worker_module, "_recover_worker_run_health_once"),
            patch.object(worker_module, "_purge_archived_tenants_once"),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_claim_next_run_once") as claim_mock,
            patch.object(worker_module, "_probe_claimable_run_once") as probe_mock,
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
            patch.object(
                worker_module,
                "_sync_run_worker_runtime_dependencies_once",
                return_value=worker_module.WorkerRuntimeDependencySnapshot(
                    dependencies={
                        "codex_cli": SimpleNamespace(state="degraded"),
                        "openai": SimpleNamespace(state="ready"),
                    }
                ),
            ),
        ):
            asyncio.run(worker_module.run_worker(mode="runs"))

        claim_mock.assert_called_once()
        self.assertEqual(claim_mock.call_args.kwargs["ready_runtime_kinds"], {"openai"})
        probe_mock.assert_not_called()
        listener.start.assert_called_once()
        listener.stop.assert_called_once()

    def test_run_worker_runs_claims_after_startup_auth_recovers(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
        )
        listener = MagicMock()
        wait_calls = {"count": 0}
        spawned_run_ids: list[str | None] = []

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = wake_event
            _ = timeout_seconds
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                return True
            stop_event.set()
            return False

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = mode
            _ = wake_event
            _ = child_timeout_seconds
            spawned_run_ids.append(claimed_run_id)
            child_result = worker_module.WorkerChildProcessResult(
                return_code=worker_module.WORKER_CHILD_EXIT_IDLE,
                processed=False,
                dependency_failure=False,
            )
            process = SimpleNamespace(pid=777, returncode=child_result.return_code, terminate=lambda: None)
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
            patch.object(worker_module, "prewarm_knowledge_dependencies"),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "_run_archived_tenant_purge_loop", new=_archived_tenant_purge_loop),
            patch.object(worker_module, "_recover_worker_run_health_once"),
            patch.object(worker_module, "_purge_archived_tenants_once"),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(
                worker_module,
                "_claim_next_run_once",
                side_effect=[
                    worker_module.ClaimedRunDispatch(
                        run_id="run-claimed",
                        claim_id="claim-claimed",
                        tenant_id="tenant-1",
                        issue_key="GP-186",
                    ),
                    None,
                ],
            ) as claim_mock,
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=False,
                    reason=QueueClaimabilityReason.NO_QUEUED_RUNS,
                ),
            ),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
            patch.object(
                worker_module,
                "_sync_run_worker_runtime_dependencies_once",
                side_effect=[
                    worker_module.WorkerRuntimeDependencySnapshot(
                        dependencies={
                            "codex_cli": SimpleNamespace(state="degraded"),
                            "openai": SimpleNamespace(state="ready"),
                        }
                    ),
                    worker_module.WorkerRuntimeDependencySnapshot(
                        dependencies={
                            "codex_cli": SimpleNamespace(state="ready"),
                            "openai": SimpleNamespace(state="ready"),
                        }
                    ),
                ],
            ),
        ):
            asyncio.run(worker_module.run_worker(mode="runs"))

        self.assertEqual(spawned_run_ids, ["run-claimed"])
        self.assertGreaterEqual(claim_mock.call_count, 1)

    def test_run_worker_retries_startup_db_recovery_then_claims_run(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
            worker_poll_interval_seconds=5,
        )
        listener = MagicMock()
        retry_timeouts: list[float] = []
        spawned_run_ids: list[str | None] = []

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = wake_event
            if timeout_seconds == worker_module.WORKER_STARTUP_DB_RETRY_BASE_DELAY_SECONDS:
                retry_timeouts.append(float(timeout_seconds))
                return True
            stop_event.set()
            return False

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = mode
            _ = wake_event
            _ = child_timeout_seconds
            _ = claim_id
            spawned_run_ids.append(claimed_run_id)
            child_result = worker_module.WorkerChildProcessResult(
                return_code=worker_module.WORKER_CHILD_EXIT_IDLE,
                processed=False,
                dependency_failure=False,
            )
            process = SimpleNamespace(pid=777, returncode=child_result.return_code, terminate=lambda: None)
            return worker_module.WorkerChildProcessHandle(
                process=process,
                wait_task=asyncio.create_task(asyncio.sleep(0, result=child_result)),
            )

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _archived_tenant_purge_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        recovery_error = SQLAlchemyOperationalError(
            "SELECT 1",
            {},
            RuntimeError("connection failed: the database system is in recovery mode"),
        )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "prewarm_knowledge_dependencies"),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "_run_archived_tenant_purge_loop", new=_archived_tenant_purge_loop),
            patch.object(worker_module, "_recover_worker_run_health_once"),
            patch.object(worker_module, "_purge_archived_tenants_once"),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(
                worker_module,
                "_claim_next_run_once",
                side_effect=[
                    recovery_error,
                    worker_module.ClaimedRunDispatch(
                        run_id="run-claimed",
                        claim_id="claim-claimed",
                        tenant_id="tenant-1",
                        issue_key="GP-186",
                    ),
                    None,
                ],
            ) as claim_mock,
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=False,
                    reason=QueueClaimabilityReason.NO_QUEUED_RUNS,
                ),
            ) as probe_mock,
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
            patch.object(
                worker_module,
                "_sync_run_worker_runtime_dependencies_once",
                return_value=worker_module.WorkerRuntimeDependencySnapshot(
                    dependencies={"openai": SimpleNamespace(state="ready")}
                ),
            ),
        ):
            asyncio.run(worker_module.run_worker(mode="runs"))

        self.assertEqual(retry_timeouts, [worker_module.WORKER_STARTUP_DB_RETRY_BASE_DELAY_SECONDS])
        self.assertEqual(spawned_run_ids, ["run-claimed"])
        self.assertEqual(claim_mock.call_count, 2)
        probe_mock.assert_not_called()

    def test_run_worker_raises_when_startup_db_retry_budget_is_exhausted(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
            worker_poll_interval_seconds=5,
        )
        listener = MagicMock()
        retry_timeouts: list[float] = []

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = wake_event
            _ = stop_event
            retry_timeouts.append(float(timeout_seconds or 0))
            return True

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _archived_tenant_purge_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        recovery_error = SQLAlchemyOperationalError(
            "SELECT 1",
            {},
            RuntimeError("connection failed: the database system is in recovery mode"),
        )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "prewarm_knowledge_dependencies"),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "_run_archived_tenant_purge_loop", new=_archived_tenant_purge_loop),
            patch.object(worker_module, "_recover_worker_run_health_once"),
            patch.object(worker_module, "_purge_archived_tenants_once"),
            patch.object(worker_module, "_claim_next_run_once", side_effect=recovery_error) as claim_mock,
            patch.object(worker_module, "_probe_claimable_run_once") as probe_mock,
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
            patch.object(
                worker_module,
                "_sync_run_worker_runtime_dependencies_once",
                return_value=worker_module.WorkerRuntimeDependencySnapshot(
                    dependencies={"openai": SimpleNamespace(state="ready")}
                ),
            ),
            patch.object(worker_module, "WORKER_STARTUP_DB_RETRY_MAX_ATTEMPTS", 3),
        ):
            with self.assertRaises(SQLAlchemyOperationalError):
                asyncio.run(worker_module.run_worker(mode="runs"))

        self.assertEqual(claim_mock.call_count, 3)
        self.assertEqual(
            retry_timeouts,
            [
                worker_module.WORKER_STARTUP_DB_RETRY_BASE_DELAY_SECONDS,
                worker_module.WORKER_STARTUP_DB_RETRY_BASE_DELAY_SECONDS * 2,
            ],
        )
        probe_mock.assert_not_called()

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
            database_url="postgresql://user:pass@localhost/test",
        )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "ensure_execution_snapshot_startup_bootstrap"),
            patch.object(worker_module, "_process_next_run_once", return_value=object()),
            patch.dict(
                os.environ,
                {
                    "ORCHESTRATOR_WORKER_CLAIMED_RUN_ID": "run-abc",
                    "ORCHESTRATOR_WORKER_CLAIM_ID": "claim-abc",
                },
                clear=False,
            ),
        ):
            self.assertEqual(
                worker_module.run_worker_child_once(mode="runs"),
                worker_module.WORKER_CHILD_EXIT_PROCESSED,
            )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "ensure_execution_snapshot_startup_bootstrap"),
            patch.object(worker_module, "_resolve_webhook_owner_id", return_value="worker:webhooks:child:test"),
            patch.object(worker_module, "_process_next_webhook_job_once", return_value=None) as process_webhook_once,
        ):
            self.assertEqual(
                worker_module.run_worker_child_once(mode="webhooks"),
                worker_module.WORKER_CHILD_EXIT_IDLE,
            )
        process_webhook_once.assert_called_once()
        self.assertEqual(process_webhook_once.call_args.kwargs["owner_id"], "worker:webhooks:child:test")

    def test_run_worker_child_once_uses_claimed_run_env(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
            database_url="postgresql://user:pass@localhost/test",
        )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "ensure_execution_snapshot_startup_bootstrap"),
            patch.object(worker_module, "_process_next_run_once", return_value=object()) as process_run_once,
            patch.dict(
                os.environ,
                {
                    "ORCHESTRATOR_WORKER_CLAIMED_RUN_ID": "run-abc",
                    "ORCHESTRATOR_WORKER_CLAIM_ID": "claim-abc",
                },
                clear=False,
            ),
        ):
            self.assertEqual(
                worker_module.run_worker_child_once(mode="runs"),
                worker_module.WORKER_CHILD_EXIT_PROCESSED,
            )

        self.assertEqual(process_run_once.call_args.kwargs["claimed_run_id"], "run-abc")
        self.assertEqual(process_run_once.call_args.kwargs["claim_id"], "claim-abc")

    def test_run_worker_child_once_requires_claimed_run_env(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
            database_url="postgresql://user:pass@localhost/test",
        )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "ensure_execution_snapshot_startup_bootstrap"),
            patch.dict(
                os.environ,
                {
                    "ORCHESTRATOR_WORKER_CLAIMED_RUN_ID": "",
                    "ORCHESTRATOR_WORKER_CLAIM_ID": "",
                },
                clear=False,
            ),
        ):
            self.assertEqual(
                worker_module.run_worker_child_once(mode="runs"),
                worker_module.WORKER_CHILD_EXIT_RUNTIME_FAILURE,
            )

    def test_run_worker_runs_claims_before_spawning_child(self) -> None:
        import orchestrator.worker as worker_module

        fake_settings = SimpleNamespace(
            database_url="postgresql://localhost/db",
            log_level="INFO",
            sentry_environment="test",
            sentry_release=None,
            agent_id="worker-test",
        )
        listener = MagicMock()
        spawned_run_ids: list[str | None] = []
        wait_calls = {"count": 0}

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = wake_event
            _ = timeout_seconds
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                return False
            stop_event.set()
            return False

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = wake_event
            _ = child_timeout_seconds
            self.assertEqual(mode, "runs")
            spawned_run_ids.append(claimed_run_id)
            self.assertEqual(claim_id, "claim-claimed")
            child_result = worker_module.WorkerChildProcessResult(
                return_code=worker_module.WORKER_CHILD_EXIT_IDLE,
                processed=False,
                dependency_failure=False,
            )
            process = SimpleNamespace(pid=654, returncode=child_result.return_code, terminate=lambda: None)
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
            patch.object(worker_module, "prewarm_knowledge_dependencies"),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(
                worker_module,
                "_claim_next_run_once",
                side_effect=[
                    worker_module.ClaimedRunDispatch(
                        run_id="run-claimed",
                        claim_id="claim-claimed",
                        tenant_id="tenant-1",
                        issue_key="GP-186",
                    ),
                    None,
                ],
            ) as claim_mock,
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=False,
                    reason=QueueClaimabilityReason.NO_QUEUED_RUNS,
                ),
            ) as probe_mock,
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once"),
            patch.object(worker_module, "_purge_archived_tenants_once"),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "_run_archived_tenant_purge_loop", new=_archived_tenant_purge_loop),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            asyncio.run(worker_module.run_worker(mode="runs"))

        self.assertEqual(spawned_run_ids, ["run-claimed"])
        self.assertGreaterEqual(claim_mock.call_count, 1)
        probe_mock.assert_not_called()

    def test_spawn_worker_child_process_times_out_and_terminates_child(self) -> None:
        import orchestrator.worker as worker_module

        class _HungProcess:
            def __init__(self) -> None:
                self.pid = 4321
                self.returncode = None
                self.terminate_called = False
                self.kill_called = False

            async def wait(self) -> int:
                if self.terminate_called:
                    self.returncode = 143
                    return self.returncode
                await asyncio.Future()

            def terminate(self) -> None:
                self.terminate_called = True

            def kill(self) -> None:
                self.kill_called = True
                self.returncode = -9

        process = _HungProcess()

        async def _run() -> None:
            with patch("orchestrator.worker.asyncio.create_subprocess_exec", return_value=process):
                handle = await worker_module._spawn_worker_child_process(
                    mode="runs",
                    wake_event=asyncio.Event(),
                    child_timeout_seconds=1,
                )
                result = await handle.wait_task
            self.assertEqual(result.return_code, worker_module.WORKER_CHILD_EXIT_RUNTIME_FAILURE)
            self.assertFalse(result.processed)
            self.assertFalse(result.dependency_failure)
            self.assertTrue(result.timed_out)
            self.assertTrue(process.terminate_called)
            self.assertFalse(process.kill_called)

        asyncio.run(_run())

    def test_spawn_worker_child_process_uses_package_entrypoint(self) -> None:
        import orchestrator.worker as worker_module

        class _Process:
            def __init__(self) -> None:
                self.pid = 9876
                self.returncode = 0

            async def wait(self) -> int:
                return self.returncode

        process = _Process()

        async def _run() -> None:
            with patch("orchestrator.worker.asyncio.create_subprocess_exec", return_value=process) as spawn_mock:
                handle = await worker_module._spawn_worker_child_process(
                    mode="runs",
                    wake_event=asyncio.Event(),
                    child_timeout_seconds=30,
                    claimed_run_id="run-123",
                    claim_id="claim-456",
                )
                result = await handle.wait_task

            self.assertEqual(result.return_code, 0)
            self.assertTrue(result.processed)
            spawn_args = spawn_mock.call_args.args
            self.assertEqual(spawn_args[:4], (sys.executable, "-m", "orchestrator", "worker-child-runs"))
            child_env = spawn_mock.call_args.kwargs["env"]
            self.assertEqual(child_env["ORCHESTRATOR_WORKER_CLAIMED_RUN_ID"], "run-123")
            self.assertEqual(child_env["ORCHESTRATOR_WORKER_CLAIM_ID"], "claim-456")

        asyncio.run(_run())

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

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = timeout_seconds
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return False
            stop_event.set()
            return False

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = mode
            _ = wake_event
            _ = child_timeout_seconds
            self.assertEqual(claimed_run_id, "run-1")
            self.assertEqual(claim_id, "claim-1")
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
            patch.object(
                worker_module,
                "_claim_next_run_once",
                side_effect=[
                    worker_module.ClaimedRunDispatch(
                        run_id="run-1",
                        claim_id="claim-1",
                        tenant_id="tenant-1",
                        issue_key="GP-1",
                    ),
                    None,
                ],
            ),
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=False,
                    reason=QueueClaimabilityReason.NO_QUEUED_RUNS,
                ),
            ),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once", new=recovery_mock),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            asyncio.run(worker_module.run_worker())

        self.assertEqual(process_mock.call_count, 1)
        recovery_mock.assert_called_once()
        listener.start.assert_called_once()
        listener.stop.assert_called_once()

    def test_run_worker_reconciles_claimed_run_after_processed_child(self) -> None:
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

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = timeout_seconds
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return False
            stop_event.set()
            return False

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = mode
            _ = wake_event
            _ = child_timeout_seconds
            child_result = worker_module.WorkerChildProcessResult(
                return_code=worker_module.WORKER_CHILD_EXIT_PROCESSED,
                processed=True,
                dependency_failure=False,
            )
            process = SimpleNamespace(pid=777, returncode=child_result.return_code, terminate=lambda: None)
            return worker_module.WorkerChildProcessHandle(
                process=process,
                wait_task=asyncio.create_task(asyncio.sleep(0, result=child_result)),
                claimed_run_id=claimed_run_id,
                claim_id=claim_id,
            )

        with (
            patch.object(worker_module, "get_settings", return_value=fake_settings),
            patch.object(worker_module, "configure_logging"),
            patch.object(worker_module, "create_session_factory", return_value=MagicMock()),
            patch.object(worker_module, "prewarm_knowledge_dependencies"),
            patch.object(worker_module, "is_postgres_database_url", return_value=True),
            patch.object(worker_module, "postgres_dsn_from_database_url", return_value="postgres://dsn"),
            patch.object(worker_module, "RunQueueNotificationBridge", return_value=listener),
            patch.object(worker_module, "wait_for_wake_or_stop", new=_wait_for_wake_or_stop),
            patch.object(worker_module, "_resolve_worker_child_capacity", return_value=1),
            patch.object(
                worker_module,
                "_claim_next_run_once",
                side_effect=[
                    worker_module.ClaimedRunDispatch(
                        run_id="run-1",
                        claim_id="claim-1",
                        tenant_id="tenant-1",
                        issue_key="GP-1",
                    ),
                    None,
                ],
            ),
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=False,
                    reason=QueueClaimabilityReason.NO_QUEUED_RUNS,
                ),
            ),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once"),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
            patch.object(worker_module, "_reconcile_claimed_run_after_child_exit", return_value="failed") as reconcile_mock,
        ):
            asyncio.run(worker_module.run_worker(mode="runs"))

        reconcile_mock.assert_called_once_with(
            session_factory=unittest.mock.ANY,
            run_id="run-1",
            claim_id="claim-1",
        )

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
        spawned_run_ids: list[str | None] = []

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = timeout_seconds
            wait_calls["count"] += 1
            if wait_calls["count"] == 1:
                wake_event.set()
                return False
            stop_event.set()
            return False

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = mode
            _ = wake_event
            _ = child_timeout_seconds
            spawned_run_ids.append(claimed_run_id)
            self.assertEqual(claim_id, f"claim-{len(spawned_run_ids)}")
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
            patch.object(
                worker_module,
                "_claim_next_run_once",
                side_effect=[
                    worker_module.ClaimedRunDispatch(
                        run_id="run-1",
                        claim_id="claim-1",
                        tenant_id="tenant-1",
                        issue_key="GP-1",
                    ),
                    worker_module.ClaimedRunDispatch(
                        run_id="run-2",
                        claim_id="claim-2",
                        tenant_id="tenant-1",
                        issue_key="GP-2",
                    ),
                    None,
                ],
            ),
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=False,
                    reason=QueueClaimabilityReason.NO_QUEUED_RUNS,
                ),
            ),
            patch.object(worker_module, "_spawn_worker_child_process", new=_spawn_worker_child_process),
            patch.object(worker_module, "_recover_worker_run_health_once", new=recovery_mock),
            patch.object(worker_module, "_run_stale_recovery_loop", new=_stale_recovery_loop),
            patch.object(worker_module, "worker_service_instance_id_for_mode", return_value="node-a:1234"),
        ):
            asyncio.run(worker_module.run_worker())

        self.assertTrue(slots_mock.called)
        self.assertGreaterEqual(process_mock.call_count, 2)
        self.assertEqual(spawned_run_ids, ["run-1", "run-2"])
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

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = timeout_seconds
            if child_spawns["count"] >= 2:
                stop_event.set()
                return False
            wake_event.set()
            return False

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = mode
            _ = wake_event
            _ = child_timeout_seconds
            self.assertEqual(claimed_run_id, f"run-{child_spawns['count'] + 1}")
            self.assertEqual(claim_id, f"claim-{child_spawns['count'] + 1}")
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
            patch.object(
                worker_module,
                "_claim_next_run_once",
                side_effect=[
                    worker_module.ClaimedRunDispatch(
                        run_id="run-1",
                        claim_id="claim-1",
                        tenant_id="tenant-1",
                        issue_key="GP-1",
                    ),
                    worker_module.ClaimedRunDispatch(
                        run_id="run-2",
                        claim_id="claim-2",
                        tenant_id="tenant-1",
                        issue_key="GP-2",
                    ),
                    None,
                ],
            ),
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=False,
                    reason=QueueClaimabilityReason.NO_QUEUED_RUNS,
                ),
            ),
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

        async def _wait_for_wake_or_stop(
            *,
            wake_event: asyncio.Event,
            stop_event: asyncio.Event,
            timeout_seconds: float | None = None,
        ) -> bool:
            _ = timeout_seconds
            _ = stop_event
            wake_event.set()
            return False

        async def _stale_recovery_loop(*, stop_event: asyncio.Event, **_kwargs: object) -> None:
            await stop_event.wait()

        async def _spawn_worker_child_process(
            *,
            mode: str,
            wake_event: asyncio.Event,
            child_timeout_seconds: int,
            claimed_run_id: str | None = None,
            claim_id: str | None = None,
        ):
            _ = mode
            _ = wake_event
            _ = child_timeout_seconds
            self.assertEqual(claimed_run_id, "run-1")
            self.assertEqual(claim_id, "claim-1")
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
            patch.object(
                worker_module,
                "_claim_next_run_once",
                return_value=worker_module.ClaimedRunDispatch(
                    run_id="run-1",
                    claim_id="claim-1",
                    tenant_id="tenant-1",
                    issue_key="GP-1",
                ),
            ),
            patch.object(
                worker_module,
                "_probe_claimable_run_once",
                return_value=QueueClaimabilityProbe(
                    claimable=False,
                    reason=QueueClaimabilityReason.NO_QUEUED_RUNS,
                ),
            ),
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
        from orchestrator.core.worker.runtime_dependencies import stop_all_live_runtime_auth_sessions

        stop_all_live_runtime_auth_sessions()
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

    def test_sync_run_worker_runtime_dependencies_marks_runtime_degraded_when_codex_auth_missing(self) -> None:
        import orchestrator.worker as worker_module

        session_factory = create_session_factory(self.database_url)
        settings = SimpleNamespace(
            worker_capabilities="linux",
            worker_runtime_kinds="codex_cli",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
        )
        service_instance_id = "worker-linux-local:runs"

        worker_module._register_worker_runtime_once(
            session_factory=session_factory,
            settings=settings,
            agent_id="worker-linux-local",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )

        fake_request = SimpleNamespace(
            request_id="request-1",
            status="active",
            remediation_text="Open this link",
            expires_at=datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc),
        )
        with patch(
            "orchestrator.core.worker.runtime_dependencies.codex_login_status",
            return_value=(False, "Not logged in"),
        ), patch(
            "orchestrator.core.worker.runtime_dependencies._start_codex_cli_login_session",
            return_value=fake_request,
        ), patch(
            "orchestrator.core.worker.runtime_dependencies._refresh_live_runtime_auth_request",
            return_value=fake_request,
        ):
            with session_factory() as session:
                from orchestrator.storage.models import WorkerRuntimeAuthRequest

                session.add(
                    WorkerRuntimeAuthRequest(
                        request_id="request-1",
                        service_instance_id=service_instance_id,
                        runtime_kind="codex_cli",
                        status="pending",
                        remediation_text=None,
                        requested_at=datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc),
                        started_at=None,
                        completed_at=None,
                        expires_at=None,
                        last_error=None,
                    )
                )
                session.commit()
            snapshot = worker_module._sync_run_worker_runtime_dependencies_once(
                session_factory=session_factory,
                settings=settings,
                agent_id="worker-linux-local",
                service_instance_id=service_instance_id,
            )

        self.assertIn("codex_cli", snapshot.blocked_runtime_kinds)
        with session_factory() as session:
            row = session.get(WorkerRuntimeState, service_instance_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.state, "degraded")
            self.assertEqual(row.runtime_kinds_json, ["codex_cli"])
            self.assertEqual(
                row.runtime_dependencies_json["codex_cli"]["remediation_text"],
                "Open this link",
            )

    def test_sync_run_worker_runtime_dependencies_starts_live_login_session_for_pending_request(self) -> None:
        import orchestrator.worker as worker_module
        from orchestrator.storage.models import WorkerRuntimeAuthRequest

        session_factory = create_session_factory(self.database_url)
        settings = SimpleNamespace(
            worker_capabilities="linux",
            worker_runtime_kinds="codex_cli",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
        )
        service_instance_id = "worker-linux-local:runs"

        worker_module._register_worker_runtime_once(
            session_factory=session_factory,
            settings=settings,
            agent_id="worker-linux-local",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )

        requested_at = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
        with session_factory() as session:
            session.add(
                WorkerRuntimeAuthRequest(
                    request_id="request-1",
                    service_instance_id=service_instance_id,
                    runtime_kind="codex_cli",
                    status="pending",
                    remediation_text=None,
                    requested_at=requested_at,
                    started_at=None,
                    completed_at=None,
                    expires_at=None,
                    last_error=None,
                )
            )
            session.commit()

        class _FakePipe:
            def __init__(self, lines: list[str]) -> None:
                self._lines = list(lines)
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeProcess:
            def __init__(self) -> None:
                self.stdout = _FakePipe(
                    [
                        "Open this link in your browser and sign in to your account\n",
                        "https://auth.openai.com/codex/device\n",
                    ]
                )
                self.pid = 4321
                self._returncode: int | None = None

            def poll(self) -> int | None:
                return self._returncode

            def terminate(self) -> None:
                self._returncode = 0

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return self._returncode or 0

            def kill(self) -> None:
                self._returncode = -9

        class _ImmediateThread:
            def __init__(self, target=None, args=(), daemon=None) -> None:  # noqa: ANN001, ARG002
                self._target = target
                self._args = args

            def start(self) -> None:
                if self._target is not None:
                    self._target(*self._args)

        with patch(
            "orchestrator.core.worker.runtime_dependencies.codex_login_status",
            return_value=(False, "Not logged in"),
        ), patch(
            "orchestrator.core.worker.runtime_dependencies.subprocess.Popen",
            return_value=_FakeProcess(),
        ), patch(
            "orchestrator.core.worker.runtime_dependencies.threading.Thread",
            _ImmediateThread,
        ):
            snapshot = worker_module._sync_run_worker_runtime_dependencies_once(
                session_factory=session_factory,
                settings=settings,
                agent_id="worker-linux-local",
                service_instance_id=service_instance_id,
            )

        self.assertIn("codex_cli", snapshot.blocked_runtime_kinds)
        with session_factory() as session:
            row = session.get(WorkerRuntimeState, service_instance_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.state, "degraded")
            self.assertIn(
                "https://auth.openai.com/codex/device",
                row.runtime_dependencies_json["codex_cli"]["remediation_text"],
            )
            request_row = session.get(WorkerRuntimeAuthRequest, "request-1")
            self.assertIsNotNone(request_row)
            assert request_row is not None
            self.assertEqual(request_row.status, "active")
            self.assertIn("https://auth.openai.com/codex/device", str(request_row.remediation_text or ""))

    def test_sync_worker_runtime_auth_requests_starts_pending_session_without_readiness_sync(self) -> None:
        from orchestrator.core.worker.runtime_dependencies import sync_worker_runtime_auth_requests
        from orchestrator.storage.models import WorkerRuntimeAuthRequest

        session_factory = create_session_factory(self.database_url)
        settings = SimpleNamespace(
            worker_runtime_kinds="codex_cli",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
            worker_runtime_auth_remediation_ttl_seconds=900,
        )
        service_instance_id = "worker-linux-local:runs"

        import orchestrator.worker as worker_module

        worker_module._register_worker_runtime_once(
            session_factory=session_factory,
            settings=SimpleNamespace(worker_capabilities="linux", worker_runtime_kinds="codex_cli"),
            agent_id="worker-linux-local",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )

        requested_at = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
        with session_factory() as session:
            session.add(
                WorkerRuntimeAuthRequest(
                    request_id="request-sync",
                    service_instance_id=service_instance_id,
                    runtime_kind="codex_cli",
                    status="pending",
                    remediation_text=None,
                    requested_at=requested_at,
                    started_at=None,
                    completed_at=None,
                    expires_at=None,
                    last_error=None,
                )
            )
            session.commit()

        class _FakePipe:
            def __init__(self, lines: list[str]) -> None:
                self._lines = list(lines)
                self._index = 0

            def readline(self) -> str:
                if self._index >= len(self._lines):
                    return ""
                line = self._lines[self._index]
                self._index += 1
                return line

            def close(self) -> None:
                return None

        class _FakeProcess:
            def __init__(self) -> None:
                self.stdout = _FakePipe(
                    [
                        "Open this link in your browser and sign in to your account\n",
                        "https://auth.openai.com/codex/device\n",
                    ]
                )
                self._returncode: int | None = None

            def poll(self) -> int | None:
                return self._returncode

            def terminate(self) -> None:
                self._returncode = 0

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return self._returncode or 0

            def kill(self) -> None:
                self._returncode = -9

        class _ImmediateThread:
            def __init__(self, target=None, args=(), daemon=None) -> None:  # noqa: ANN001, ARG002
                self._target = target
                self._args = args

            def start(self) -> None:
                if self._target is not None:
                    self._target(*self._args)

        with patch(
            "orchestrator.core.worker.runtime_dependencies.subprocess.Popen",
            return_value=_FakeProcess(),
        ), patch(
            "orchestrator.core.worker.runtime_dependencies.threading.Thread",
            _ImmediateThread,
        ):
            sync_worker_runtime_auth_requests(
                session_factory=session_factory,
                settings=settings,
                service_instance_id=service_instance_id,
            )

        with session_factory() as session:
            request_row = session.get(WorkerRuntimeAuthRequest, "request-sync")
            self.assertIsNotNone(request_row)
            assert request_row is not None
            self.assertEqual(request_row.status, "active")
            self.assertIn("https://auth.openai.com/codex/device", str(request_row.remediation_text or ""))

    def test_sync_worker_runtime_auth_requests_completes_successful_session(self) -> None:
        from orchestrator.core.worker.runtime_dependencies import sync_worker_runtime_auth_requests
        from orchestrator.storage.models import WorkerRuntimeAuthRequest

        session_factory = create_session_factory(self.database_url)
        settings = SimpleNamespace(
            worker_runtime_kinds="codex_cli",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
            worker_runtime_auth_remediation_ttl_seconds=900,
        )
        service_instance_id = "worker-linux-local:runs"

        import orchestrator.worker as worker_module

        worker_module._register_worker_runtime_once(
            session_factory=session_factory,
            settings=SimpleNamespace(worker_capabilities="linux", worker_runtime_kinds="codex_cli"),
            agent_id="worker-linux-local",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )

        requested_at = datetime(2026, 4, 13, 0, 0, tzinfo=timezone.utc)
        with session_factory() as session:
            session.add(
                WorkerRuntimeAuthRequest(
                    request_id="request-complete",
                    service_instance_id=service_instance_id,
                    runtime_kind="codex_cli",
                    status="active",
                    remediation_text="Open this link\nhttps://auth.openai.com/codex/device",
                    requested_at=requested_at,
                    started_at=requested_at,
                    completed_at=None,
                    expires_at=requested_at + timedelta(minutes=15),
                    last_error=None,
                )
            )
            session.commit()

        class _FakeProcess:
            def __init__(self) -> None:
                self._returncode: int | None = 0

            def poll(self) -> int | None:
                return self._returncode

            def terminate(self) -> None:
                return None

            def wait(self, timeout: float | None = None) -> int:  # noqa: ARG002
                return 0

            def kill(self) -> None:
                return None

        from orchestrator.core.worker import runtime_dependencies as runtime_dependencies_module

        runtime_dependencies_module._LIVE_RUNTIME_AUTH_SESSIONS[(service_instance_id, "codex_cli")] = (
            runtime_dependencies_module._LiveRuntimeAuthSession(
                request_id="request-complete",
                process=_FakeProcess(),
                started_at=requested_at,
                expires_at=requested_at + timedelta(minutes=15),
            )
        )
        runtime_dependencies_module._LIVE_RUNTIME_AUTH_SESSIONS[(service_instance_id, "codex_cli")].append(
            "Open this link\nhttps://auth.openai.com/codex/device\n"
        )

        with patch(
            "orchestrator.core.worker.runtime_dependencies.codex_login_status",
            return_value=(True, "Logged in"),
        ):
            sync_worker_runtime_auth_requests(
                session_factory=session_factory,
                settings=settings,
                service_instance_id=service_instance_id,
            )

        with session_factory() as session:
            request_row = session.get(WorkerRuntimeAuthRequest, "request-complete")
            self.assertIsNotNone(request_row)
            assert request_row is not None
            self.assertEqual(request_row.status, "completed")

    def test_sync_run_worker_runtime_dependencies_does_not_create_login_request_when_missing(self) -> None:
        import orchestrator.worker as worker_module
        from orchestrator.storage.models import WorkerRuntimeAuthRequest

        session_factory = create_session_factory(self.database_url)
        settings = SimpleNamespace(
            worker_capabilities="linux",
            worker_runtime_kinds="codex_cli",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
        )
        service_instance_id = "worker-linux-local:runs"

        worker_module._register_worker_runtime_once(
            session_factory=session_factory,
            settings=settings,
            agent_id="worker-linux-local",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )

        with patch(
            "orchestrator.core.worker.runtime_dependencies.codex_login_status",
            return_value=(False, "Not logged in"),
        ):
            snapshot = worker_module._sync_run_worker_runtime_dependencies_once(
                session_factory=session_factory,
                settings=settings,
                agent_id="worker-linux-local",
                service_instance_id=service_instance_id,
            )

        self.assertIn("codex_cli", snapshot.blocked_runtime_kinds)
        with session_factory() as session:
            rows = session.execute(select(WorkerRuntimeAuthRequest)).scalars().all()
            self.assertEqual(len(rows), 0)
            row = session.get(WorkerRuntimeState, service_instance_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.state, "degraded")
            self.assertEqual(row.runtime_dependencies_json["codex_cli"]["summary"], "Codex CLI is not authenticated on this worker.")
            self.assertNotIn("remediation_text", row.runtime_dependencies_json["codex_cli"])

    def test_sync_run_worker_runtime_dependencies_clears_stale_remediation_without_open_request(self) -> None:
        import orchestrator.worker as worker_module

        session_factory = create_session_factory(self.database_url)
        settings = SimpleNamespace(
            worker_capabilities="linux",
            worker_runtime_kinds="codex_cli",
            codex_cli_command="codex",
            runtime_home="/tmp/master-builder-test-runtime-home",
        )
        service_instance_id = "worker-linux-local:runs"

        worker_module._register_worker_runtime_once(
            session_factory=session_factory,
            settings=settings,
            agent_id="worker-linux-local",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )

        with session_factory() as session:
            row = session.get(WorkerRuntimeState, service_instance_id)
            assert row is not None
            row.runtime_dependencies_json = {
                "codex_cli": {
                    "state": "degraded",
                    "summary": "Codex CLI is not authenticated on this worker.",
                    "remediation_text": "stale instructions",
                    "remediation_expires_at": datetime(2026, 4, 13, 12, 0, tzinfo=timezone.utc).isoformat(),
                }
            }
            session.commit()

        with patch(
            "orchestrator.core.worker.runtime_dependencies.codex_login_status",
            return_value=(False, "Not logged in"),
        ):
            snapshot = worker_module._sync_run_worker_runtime_dependencies_once(
                session_factory=session_factory,
                settings=settings,
                agent_id="worker-linux-local",
                service_instance_id=service_instance_id,
            )

        self.assertIn("codex_cli", snapshot.blocked_runtime_kinds)
        with session_factory() as session:
            row = session.get(WorkerRuntimeState, service_instance_id)
            assert row is not None
            self.assertNotIn("remediation_text", row.runtime_dependencies_json["codex_cli"])
            self.assertNotIn("remediation_expires_at", row.runtime_dependencies_json["codex_cli"])

    def test_sync_run_worker_runtime_dependencies_marks_runtime_idle_when_codex_auth_ready(self) -> None:
        import orchestrator.worker as worker_module

        session_factory = create_session_factory(self.database_url)
        settings = SimpleNamespace(
            worker_capabilities="linux",
            worker_runtime_kinds="codex_cli",
            codex_cli_command="codex",
        )
        service_instance_id = "worker-linux-local:runs"

        worker_module._register_worker_runtime_once(
            session_factory=session_factory,
            settings=settings,
            agent_id="worker-linux-local",
            service_instance_id=service_instance_id,
            worker_mode=worker_module.WORKER_MODE_RUNS,
        )
        with patch(
            "orchestrator.core.worker.runtime_dependencies.codex_login_status",
            return_value=(True, "Logged in"),
        ):
            snapshot = worker_module._sync_run_worker_runtime_dependencies_once(
                session_factory=session_factory,
                settings=settings,
                agent_id="worker-linux-local",
                service_instance_id=service_instance_id,
            )

        self.assertFalse(snapshot.degraded)
        with session_factory() as session:
            row = session.get(WorkerRuntimeState, service_instance_id)
            self.assertIsNotNone(row)
            assert row is not None
            self.assertEqual(row.state, "idle")
            self.assertEqual(row.runtime_kinds_json, ["codex_cli"])
            self.assertEqual(row.runtime_dependencies_json["codex_cli"]["state"], "ready")


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
