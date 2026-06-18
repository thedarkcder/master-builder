from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import patch

from orchestrator.cli import _build_parser, main


ROOT = Path(__file__).resolve().parents[1]


def test_cli_keeps_command_specific_runtimes_out_of_top_level_imports() -> None:
    tree = ast.parse((ROOT / "orchestrator" / "cli.py").read_text(encoding="utf-8"))
    forbidden_modules = {
        "orchestrator.core.discord.gateway_runtime",
        "orchestrator.core.discord.live_voice_gateway_runtime",
        "orchestrator.core.deployment_runtime",
        "orchestrator.core.jira_project_reconciliation.scheduler",
        "orchestrator.core.knowledge.jira_sync_runtime",
        "orchestrator.core.knowledge.prewarm",
        "orchestrator.core.projects.automation_runtime",
        "orchestrator.core.runtime.tools",
        "orchestrator.core.voice.prewarm",
        "orchestrator.storage.migrations",
        "orchestrator.temporal.worker",
        "orchestrator.worker",
    }

    violations = [
        node.module
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module in forbidden_modules
    ]

    assert violations == []


def test_cli_exposes_deployment_reconciler_command() -> None:
    parser = _build_parser()
    choices = parser._subparsers._group_actions[0].choices

    assert "deployment-reconciler" in choices


def test_cli_dispatches_deployment_reconciler_command() -> None:
    with patch("orchestrator.core.deployment_runtime.run_deployment_reconciler") as run_reconciler:
        assert main(["deployment-reconciler"]) == 0

    run_reconciler.assert_called_once_with()
