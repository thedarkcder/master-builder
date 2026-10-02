from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.api.transport_runtime import build_http_transport_action_executors


class TransportRuntimeTests(unittest.TestCase):
    def test_build_http_transport_action_executors_injects_discord_bot_token(
        self,
    ) -> None:
        settings = SimpleNamespace(secrets_encryption_key="enc")
        session = object()

        with patch(
            "orchestrator.api.transport_runtime.build_discord_transport_executor"
        ) as build_executor_mock:
            build_executor_mock.return_value = object()
            executors = build_http_transport_action_executors(
                session=session,
                settings=settings,
                resolve_platform_secret_ref_fn=lambda _session, **_kwargs: (
                    "bot-token-123"
                ),
            )

        self.assertEqual(len(executors), 1)
        self.assertEqual(
            build_executor_mock.call_args.kwargs["bot_token"], "bot-token-123"
        )


if __name__ == "__main__":
    unittest.main()
