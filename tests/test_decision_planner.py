from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from orchestrator.core.decision_planner import plan_decision_questions


class DecisionPlannerTests(unittest.TestCase):
    def test_planner_prompt_uses_structured_tool_contract_without_agent_tool_command(self) -> None:
        tenant = SimpleNamespace(tenant_id="tenant-1")
        project = SimpleNamespace(project_id="project-1")
        cycle = SimpleNamespace(cycle_id="cycle-1", question_set_json=[], metadata_json={})
        case = SimpleNamespace(state="blocked")
        captured: dict[str, object] = {}

        def _render_prompt(template_name: str, **kwargs):  # noqa: ANN001
            if template_name == "policy/decision_planner_user.j2":
                captured.update(kwargs)
            return template_name

        with (
            patch("orchestrator.core.decision_planner.build_codex_runtime", return_value=SimpleNamespace()),
            patch(
                "orchestrator.core.runtime_stage_session.invoke_runtime_json_with_tools",
                return_value={"gate_status": "clear", "reason": "", "questions": [], "question_states": []},
            ),
            patch("orchestrator.core.decision_planner.render_prompt", side_effect=_render_prompt),
        ):
            result = plan_decision_questions(
                session=SimpleNamespace(),
                settings=SimpleNamespace(),
                tenant=tenant,
                project=project,
                issue_key="MAB-174",
                source="discord",
                classification="decision_gate",
                block_reason="clarification required",
                case=case,
                cycle=cycle,
            )

        self.assertIsNotNone(result)
        self.assertEqual(captured["project_id"], "project-1")
        self.assertIn("decision.read_state", str(captured["allowed_tools_json"]))
        self.assertNotIn("agent_tool_command", captured)


if __name__ == "__main__":
    unittest.main()
