from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from orchestrator.core.discord.gateway_listener import DiscordGatewayListener


class _FakeWebSocket:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = [json.dumps(item) for item in messages]
        self.sent_payloads: list[dict] = []

    async def recv(self) -> str:
        if not self._messages:
            raise RuntimeError("no more messages")
        return self._messages.pop(0)

    async def send(self, payload: str) -> None:
        self.sent_payloads.append(json.loads(payload))


class _FakeWebSocketContext:
    def __init__(self, websocket: _FakeWebSocket) -> None:
        self._websocket = websocket

    async def __aenter__(self) -> _FakeWebSocket:
        return self._websocket

    async def __aexit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False


class _NoopTask:
    def cancel(self) -> None:
        return

    def __await__(self):  # noqa: ANN204
        async def _done() -> None:
            return

        return _done().__await__()


class DiscordGatewayListenerRuntimeTests(unittest.TestCase):
    def _listener(self) -> tuple[DiscordGatewayListener, MagicMock]:
        settings = SimpleNamespace(
            discord_bot_token_secret_ref="platform/DISCORD_BOT_TOKEN",
            secrets_encryption_key="enc",
        )
        session = MagicMock()
        with patch("orchestrator.core.discord.gateway_listener.create_session_factory", return_value=lambda: self._ctx(session)):
            listener = DiscordGatewayListener(settings=settings)
        return listener, session

    @staticmethod
    def _ctx(session):  # noqa: ANN001, ANN205
        class _Ctx:
            def __enter__(self):  # noqa: ANN204
                return session

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001, ANN204
                return False

        return _Ctx()

    @staticmethod
    def _fake_create_task(coro) -> _NoopTask:  # noqa: ANN001
        coro.close()
        return _NoopTask()

    def test_run_loop_skips_without_websockets_or_token_ref(self) -> None:
        listener, _session = self._listener()
        listener._settings.discord_bot_token_secret_ref = ""

        asyncio.run(listener._run_loop())

        listener, _session = self._listener()
        with patch("orchestrator.core.discord.gateway_listener.websockets", None):
            asyncio.run(listener._run_loop())

    def test_start_and_stop_handle_thread_lifecycle(self) -> None:
        listener, _session = self._listener()
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True
        listener._thread = fake_thread
        listener.start()
        fake_thread.start.assert_not_called()

        listener.stop()
        fake_thread.join.assert_called_once()

    def test_run_thread_catches_asyncio_run_failure(self) -> None:
        listener, _session = self._listener()
        with patch("orchestrator.core.discord.gateway_listener.asyncio.run", side_effect=RuntimeError("boom")):
            listener._run_thread()

    def test_run_loop_waits_for_token_then_runs_connection(self) -> None:
        listener, _session = self._listener()
        calls = {"sleep": 0, "connect": 0}

        async def _fake_sleep(_seconds: float) -> None:
            calls["sleep"] += 1
            if calls["sleep"] >= 2:
                listener._stop_event.set()

        async def _fake_run_single_connection(*, bot_token: str) -> None:
            self.assertEqual(bot_token, "bot-token")
            calls["connect"] += 1
            listener._stop_event.set()

        with (
            patch("orchestrator.core.discord.gateway_listener.websockets", object()),
            patch.object(listener, "_resolve_bot_token", side_effect=["", "bot-token"]),
            patch.object(listener, "_run_single_connection", side_effect=_fake_run_single_connection),
            patch("orchestrator.core.discord.gateway_listener.asyncio.sleep", side_effect=_fake_sleep),
        ):
            asyncio.run(listener._run_loop())

        self.assertGreaterEqual(calls["sleep"], 1)
        self.assertEqual(calls["connect"], 1)

    def test_resolve_bot_token(self) -> None:
        listener, session = self._listener()
        with patch("orchestrator.core.discord.gateway_listener.resolve_scoped_secret_ref", return_value=" token "):
            self.assertEqual(listener._resolve_bot_token(token_ref="platform/DISCORD_BOT_TOKEN"), "token")
        with patch("orchestrator.core.discord.gateway_listener.resolve_scoped_secret_ref", return_value=None):
            self.assertEqual(listener._resolve_bot_token(token_ref="platform/DISCORD_BOT_TOKEN"), "")
        self.assertIsNotNone(session)

    def test_run_single_connection_identify_and_resume_paths(self) -> None:
        listener, _session = self._listener()

        ws = _FakeWebSocket(
            [
                {"d": {"heartbeat_interval": 100}},
                {"op": 0, "t": "READY", "d": {"session_id": "sess-1", "resume_gateway_url": "wss://resume.test"}, "s": 1},
                {"op": 7, "t": "", "d": {}, "s": 2},
            ]
        )

        async def _fake_to_thread(fn, *args):  # noqa: ANN001
            fn(*args)

        with (
            patch("orchestrator.core.discord.gateway_listener.websockets", SimpleNamespace(connect=lambda *a, **k: _FakeWebSocketContext(ws))),
            patch("orchestrator.core.discord.gateway_listener.asyncio.create_task", side_effect=self._fake_create_task),
            patch("orchestrator.core.discord.gateway_listener.asyncio.to_thread", side_effect=_fake_to_thread),
        ):
            asyncio.run(listener._run_single_connection(bot_token="bot-token"))

        self.assertEqual(ws.sent_payloads[0]["op"], 2)
        self.assertEqual(listener._session_id, "sess-1")
        self.assertTrue(listener._resume_gateway_url.startswith("wss://resume.test"))

        listener._session_id = "sess-1"
        listener._sequence = 3
        ws_resume = _FakeWebSocket(
            [
                {"d": {"heartbeat_interval": 100}},
                {"op": 7, "t": "", "d": {}, "s": 4},
            ]
        )
        with (
            patch("orchestrator.core.discord.gateway_listener.websockets", SimpleNamespace(connect=lambda *a, **k: _FakeWebSocketContext(ws_resume))),
            patch("orchestrator.core.discord.gateway_listener.asyncio.create_task", side_effect=self._fake_create_task),
        ):
            asyncio.run(listener._run_single_connection(bot_token="bot-token"))

        self.assertEqual(ws_resume.sent_payloads[0]["op"], 6)

    def test_run_single_connection_handles_invalid_session_opcode(self) -> None:
        listener, _session = self._listener()
        listener._session_id = "sess-1"
        listener._sequence = 3
        ws = _FakeWebSocket(
            [
                {"d": {"heartbeat_interval": 100}},
                {"op": 9, "t": "", "d": {}, "s": 4},
            ]
        )

        async def _fake_sleep(_seconds: float) -> None:
            return

        with (
            patch("orchestrator.core.discord.gateway_listener.websockets", SimpleNamespace(connect=lambda *a, **k: _FakeWebSocketContext(ws))),
            patch("orchestrator.core.discord.gateway_listener.asyncio.create_task", side_effect=self._fake_create_task),
            patch("orchestrator.core.discord.gateway_listener.asyncio.sleep", side_effect=_fake_sleep),
        ):
            asyncio.run(listener._run_single_connection(bot_token="bot-token"))

        self.assertIsNone(listener._session_id)
        self.assertIsNone(listener._sequence)

    def test_heartbeat_loop_sends_sequence(self) -> None:
        listener, _session = self._listener()
        listener._sequence = 42
        websocket = SimpleNamespace(send=AsyncMock())
        calls = {"count": 0}

        async def _fake_sleep(_seconds: float) -> None:
            calls["count"] += 1
            listener._stop_event.set()

        with patch("orchestrator.core.discord.gateway_listener.asyncio.sleep", side_effect=_fake_sleep):
            asyncio.run(listener._heartbeat_loop(websocket, 0.01))

        websocket.send.assert_awaited_once()
        sent_payload = json.loads(websocket.send.await_args.args[0])
        self.assertEqual(sent_payload["op"], 1)
        self.assertEqual(sent_payload["d"], 42)

    def test_run_single_connection_dispatches_message_create(self) -> None:
        listener, _session = self._listener()
        ws = _FakeWebSocket(
            [
                {"d": {"heartbeat_interval": 100}},
                {"op": 0, "t": "MESSAGE_CREATE", "d": {"channel_id": "c1"}, "s": 1},
                {"op": 7, "t": "", "d": {}, "s": 2},
            ]
        )

        called = {"count": 0}

        def _handle(payload: dict, bot_token: str) -> None:
            called["count"] += 1
            self.assertEqual(payload["channel_id"], "c1")
            self.assertEqual(bot_token, "bot-token")

        async def _fake_to_thread(fn, *args):  # noqa: ANN001
            fn(*args)

        with (
            patch("orchestrator.core.discord.gateway_listener.websockets", SimpleNamespace(connect=lambda *a, **k: _FakeWebSocketContext(ws))),
            patch("orchestrator.core.discord.gateway_listener.asyncio.create_task", side_effect=self._fake_create_task),
            patch.object(listener, "_handle_message_create", side_effect=_handle),
            patch("orchestrator.core.discord.gateway_listener.asyncio.to_thread", side_effect=_fake_to_thread),
        ):
            asyncio.run(listener._run_single_connection(bot_token="bot-token"))

        self.assertEqual(called["count"], 1)


if __name__ == "__main__":
    unittest.main()
