from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from orchestrator.core.discord.live_voice_gateway_runtime import _healthcheck_loop


class LiveVoiceGatewayRuntimeTests(unittest.TestCase):
    def test_healthcheck_exits_silently_when_attempt_is_already_stopped(self) -> None:
        attempt_stop_event = threading.Event()
        runtime_stop_event = threading.Event()
        attempt_stop_event.set()

        with patch(
            "orchestrator.core.discord.live_voice_gateway_runtime.logger.exception"
        ) as logger_mock:
            _healthcheck_loop(
                conn=object(),
                attempt_stop_event=attempt_stop_event,
                runtime_stop_event=runtime_stop_event,
                poll_seconds=1,
            )

        logger_mock.assert_not_called()

    def test_healthcheck_marks_attempt_stopped_on_real_connection_loss(self) -> None:
        attempt_stop_event = threading.Event()
        runtime_stop_event = threading.Event()

        with (
            patch(
                "orchestrator.core.discord.live_voice_gateway_runtime._leader_lock_healthcheck",
                side_effect=RuntimeError("connection lost"),
            ),
            patch(
                "orchestrator.core.discord.live_voice_gateway_runtime.logger.exception"
            ) as logger_mock,
        ):
            _healthcheck_loop(
                conn=object(),
                attempt_stop_event=attempt_stop_event,
                runtime_stop_event=runtime_stop_event,
                poll_seconds=1,
            )

        self.assertTrue(attempt_stop_event.is_set())
        logger_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
