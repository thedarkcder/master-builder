from __future__ import annotations

import unittest

from orchestrator.core.communications.command_pipeline import (
    CommandExecutionContext,
    dispatch_registered_command,
)


class CommandPipelineTests(unittest.TestCase):
    def _context(self, command_name: str = "help") -> CommandExecutionContext:
        return CommandExecutionContext(
            command_name=command_name,
            arguments=(),
            tenant_id="tenant-a",
            tenant=object(),
            session=object(),
            payload=object(),
            normalized_user_id="u-1",
            normalized_channel_id="c-1",
            flags={},
        )

    def test_dispatch_uses_registered_handler_for_command(self) -> None:
        called: list[str] = []

        def _handler(context: CommandExecutionContext) -> dict:
            called.append(context.command_name)
            return {"ok": True, "command": context.command_name}

        result = dispatch_registered_command(
            context=self._context("ask"),
            registry={"ask": (_handler,)},
        )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["command"], "ask")
        self.assertEqual(called, ["ask"])

    def test_dispatch_raises_for_unsupported_command(self) -> None:
        with self.assertRaises(ValueError):
            dispatch_registered_command(
                context=self._context("unknown"),
                registry={"help": (lambda _ctx: {"ok": True},)},
            )


if __name__ == "__main__":
    unittest.main()
