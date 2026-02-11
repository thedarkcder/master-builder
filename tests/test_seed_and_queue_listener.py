from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from orchestrator.api.discord.seed import matching, normalization
from orchestrator.core.worker import queue_listener


class SeedMatchingTests(unittest.TestCase):
    def test_normalized_summary_and_similarity(self) -> None:
        self.assertEqual(matching.normalized_summary_key("Fix login bug!!!"), "fix login bug")
        self.assertGreater(matching.summary_similarity("fix login bug", "login bug fix"), 0.5)
        self.assertEqual(matching.summary_similarity("", "x"), 0.0)

    def test_select_seed_match(self) -> None:
        issues = [
            SimpleNamespace(key="MAB-1", summary="Fix login bug"),
            SimpleNamespace(key="MAB-2", summary="Add onboarding wizard"),
            SimpleNamespace(key="MAB-3", summary="Improve queue listener reliability"),
        ]

        requested = matching.select_seed_match(
            existing_issues=issues,
            summary="anything",
            requested_issue_key="MAB-2",
            matched_issue_keys=set(),
        )
        self.assertEqual(requested.key, "MAB-2")

        normalized = matching.select_seed_match(
            existing_issues=issues,
            summary="fix login bug",
            requested_issue_key=None,
            matched_issue_keys=set(),
        )
        self.assertEqual(normalized.key, "MAB-1")

        fuzzy = matching.select_seed_match(
            existing_issues=issues,
            summary="improve queue listener reliability",
            requested_issue_key=None,
            matched_issue_keys={"MAB-1", "MAB-2"},
        )
        self.assertEqual(fuzzy.key, "MAB-3")

        none_match = matching.select_seed_match(
            existing_issues=issues,
            summary="totally unrelated",
            requested_issue_key=None,
            matched_issue_keys={"MAB-1", "MAB-2", "MAB-3"},
        )
        self.assertIsNone(none_match)


class SeedNormalizationTests(unittest.TestCase):
    def test_normalization_helpers(self) -> None:
        self.assertEqual(normalization.normalize_seed_issue_labels(["Bug", "bug", "  "]), ["discord-seeded", "bug"])
        self.assertEqual(normalization.normalize_seed_issue_labels(None), ["discord-seeded"])

        self.assertEqual(normalization.normalize_seed_issue_tags(["UI", "ui", "backend"]), ["ui", "backend"])
        self.assertEqual(normalization.normalize_seed_issue_tags(None), [])

        self.assertEqual(normalization.parse_seed_issue_type("bug"), "Bug")
        self.assertEqual(normalization.parse_seed_issue_type("story"), "Story")
        self.assertEqual(normalization.parse_seed_issue_type("other"), "Task")

        self.assertEqual(normalization.normalize_seed_issue_scope(["A", "", " B "]), ["A", "B"])
        self.assertEqual(normalization.normalize_seed_issue_scope(None), [])

    def test_issue_key_and_question_helpers(self) -> None:
        pattern = __import__("re").compile(r"^[A-Z]+-\d+$")
        self.assertEqual(normalization.normalize_seed_issue_key("mab-1", issue_key_pattern=pattern), "MAB-1")
        self.assertIsNone(normalization.normalize_seed_issue_key("bad", issue_key_pattern=pattern))

        self.assertTrue(normalization.seed_text_is_missing("TBD"))
        self.assertTrue(normalization.seed_text_is_missing("to be determined later"))
        self.assertFalse(normalization.seed_text_is_missing("implement oauth"))

        questions = normalization.collect_seed_issue_questions(
            issue_summary="Improve reliability",
            objective="TBD",
            scope_in=[],
            scope_out=[],
            acceptance=[],
        )
        self.assertEqual(len(questions), 4)


class QueueListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_for_wake_or_stop(self) -> None:
        wake_event = asyncio.Event()
        stop_event = asyncio.Event()

        wake_event.set()
        await queue_listener.wait_for_wake_or_stop(wake_event=wake_event, stop_event=stop_event)

        wake_event.clear()
        waiter = asyncio.create_task(queue_listener.wait_for_wake_or_stop(wake_event=wake_event, stop_event=stop_event))
        stop_event.set()
        await waiter

    def test_run_queue_notification_bridge_run_paths(self) -> None:
        wake_event = SimpleNamespace(set=MagicMock())
        loop = SimpleNamespace(call_soon_threadsafe=MagicMock())
        logger = MagicMock()

        bridge = queue_listener.RunQueueNotificationBridge(
            postgres_dsn="dsn",
            wake_event=wake_event,
            loop=loop,
            logger=logger,
            notify_channel="run_queue",
            psycopg_module=None,
        )
        bridge._run()
        logger.error.assert_called_once()

        class FakeConn:
            def __init__(self):
                self.closed = False

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, _sql):
                return None

            def notifies(self):
                yield object()
                bridge._stop_event.set()
                yield object()

            def close(self):
                self.closed = True

        psycopg = SimpleNamespace(connect=lambda *_args, **_kwargs: FakeConn())
        bridge = queue_listener.RunQueueNotificationBridge(
            postgres_dsn="dsn",
            wake_event=wake_event,
            loop=loop,
            logger=logger,
            notify_channel="run_queue",
            psycopg_module=psycopg,
        )
        bridge._run()
        self.assertGreaterEqual(loop.call_soon_threadsafe.call_count, 1)

        class RaisingPsycopg:
            @staticmethod
            def connect(*_args, **_kwargs):
                raise RuntimeError("boom")

        bridge = queue_listener.RunQueueNotificationBridge(
            postgres_dsn="dsn",
            wake_event=wake_event,
            loop=loop,
            logger=logger,
            notify_channel="run_queue",
            psycopg_module=RaisingPsycopg,
        )
        bridge._run()
        logger.exception.assert_called()

    def test_start_stop(self) -> None:
        wake_event = SimpleNamespace(set=MagicMock())
        loop = SimpleNamespace(call_soon_threadsafe=MagicMock())
        logger = MagicMock()

        class FakeThread:
            def __init__(self, **kwargs):
                self._alive = False
                self.kwargs = kwargs

            def start(self):
                self._alive = True

            def is_alive(self):
                return self._alive

            def join(self, timeout=None):
                self._alive = False

        created: list[FakeThread] = []

        def fake_thread_factory(**kwargs):
            t = FakeThread(**kwargs)
            created.append(t)
            return t

        with unittest.mock.patch("orchestrator.core.worker.queue_listener.threading.Thread", side_effect=fake_thread_factory):
            bridge = queue_listener.RunQueueNotificationBridge(
                postgres_dsn="dsn",
                wake_event=wake_event,
                loop=loop,
                logger=logger,
                notify_channel="run_queue",
                psycopg_module=None,
            )
            bridge.start()
            self.assertEqual(len(created), 1)
            bridge.start()
            self.assertEqual(len(created), 1)

            bridge._conn = SimpleNamespace(close=MagicMock())
            bridge.stop()
            bridge._conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
