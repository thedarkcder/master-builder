from __future__ import annotations

from pathlib import Path
import re
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]


class TeamRunGuardrailTests(unittest.TestCase):
    def test_agent_runtimes_route_does_not_fall_back_to_hardcoded_known_agent_catalogs(self) -> None:
        source = (REPO_ROOT / "orchestrator/api/routes/admin_agent_runtimes.py").read_text(encoding="utf-8")
        self.assertNotIn("list_known_agent_roles", source)
        self.assertNotIn("list_known_agent_names", source)
        self.assertNotIn("list_known_execution_selectors", source)

    def test_team_run_primary_backend_files_do_not_hardcode_legacy_stage_literals(self) -> None:
        quoted_stage_pattern = re.compile(r"""['"](pm|dev|test|review)['"]""")
        files = [
            REPO_ROOT / "orchestrator/core/team_run_service.py",
            REPO_ROOT / "orchestrator/core/runs.py",
            REPO_ROOT / "orchestrator/core/worker/queue_selector.py",
            REPO_ROOT / "orchestrator/api/routes/admin_platform_catalog.py",
            REPO_ROOT / "orchestrator/temporal/team_run_orchestration.py",
            REPO_ROOT / "orchestrator/temporal/activities/team_run.py",
            REPO_ROOT / "orchestrator/temporal/workflows/team_run.py",
        ]
        for path in files:
            source = path.read_text(encoding="utf-8")
            self.assertIsNone(
                quoted_stage_pattern.search(source),
                f"Found a hardcoded legacy stage literal in {path}",
            )
