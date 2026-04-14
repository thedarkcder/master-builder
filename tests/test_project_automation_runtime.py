from __future__ import annotations

from types import SimpleNamespace
import unittest

from orchestrator.core.project_automation_runtime import ProjectAutomationRuntime


class ProjectAutomationRuntimeTests(unittest.TestCase):
    def test_run_forever_requires_postgres(self) -> None:
        runtime = ProjectAutomationRuntime(
            settings=SimpleNamespace(
                database_url="sqlite:///tmp/test.db",
                project_automation_lock_key=1,
                project_automation_poll_seconds=5,
                project_automation_interval_seconds=30,
            )
        )

        with self.assertRaisesRegex(RuntimeError, "requires PostgreSQL"):
            runtime.run_forever()


if __name__ == "__main__":
    unittest.main()
