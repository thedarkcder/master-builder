from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cli_keeps_command_specific_runtimes_out_of_top_level_imports() -> None:
    tree = ast.parse((ROOT / "orchestrator" / "cli.py").read_text(encoding="utf-8"))
    forbidden_modules = {
        "orchestrator.core.discord.gateway_runtime",
        "orchestrator.core.discord.live_voice_gateway_runtime",
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
