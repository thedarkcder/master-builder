from pathlib import Path
import unittest


class RunHybridWorkersScriptTests(unittest.TestCase):
    def test_script_starts_and_configures_redis_for_local_worker(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("redis", script)
        self.assertIn(
            'export ORCHESTRATOR_REDIS_URL="${ORCHESTRATOR_REDIS_URL:-redis://127.0.0.1:46379/0}"',
            script,
        )


if __name__ == "__main__":
    unittest.main()
