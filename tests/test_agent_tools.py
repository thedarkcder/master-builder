from __future__ import annotations

from unittest.mock import patch

import pytest

from orchestrator.core.agent_tools import allowed_tools_for_stage, execute_agent_tool


def test_allowed_tools_for_stage_dev_contains_github_and_jira() -> None:
    tools = allowed_tools_for_stage("dev")
    assert "github.create_branch" in tools
    assert "github.open_pr" in tools
    assert "jira.comment" in tools


def test_execute_agent_tool_rejects_disallowed_stage_tool() -> None:
    class _FakeContext:
        stage = "pm"

    with patch("orchestrator.core.agent_tools._resolve_context", return_value=_FakeContext()):
        with pytest.raises(PermissionError):
            execute_agent_tool(
                session=None,  # type: ignore[arg-type]
                settings=None,
                tenant_id="route25",
                project_id="route25-default",
                run_id="run-1",
                issue_key="MAB-1",
                stage="pm",
                tool_name="github.open_pr",
                tool_args={},
            )
