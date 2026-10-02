from __future__ import annotations

import unittest

from orchestrator.core.communications.command_pipeline import (
    CommandScope,
    CommandExecutionContext,
    IngressSource,
    parse_ingress_source,
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

    def test_context_uses_explicit_scope_contract(self) -> None:
        context = CommandExecutionContext(
            command_name="status",
            arguments=(),
            tenant_id="tenant-a",
            tenant=object(),
            session=object(),
            payload=object(),
            normalized_user_id="u-1",
            normalized_channel_id="c-1",
            flags={},
            scope=CommandScope(
                project_id="project-a", project_keys=("TP",), channel_id="c-1"
            ),
        )
        self.assertEqual(context.scope.project_id, "project-a")
        self.assertEqual(context.scope.project_keys, ("TP",))

    def test_parse_ingress_source_contract(self) -> None:
        self.assertEqual(parse_ingress_source("discord"), IngressSource.DISCORD)
        self.assertEqual(
            parse_ingress_source("jira_comment"), IngressSource.JIRA_COMMENT
        )
        with self.assertRaises(ValueError):
            parse_ingress_source("slack")


if __name__ == "__main__":
    unittest.main()
