from pathlib import Path
import unittest


class RunHybridWorkersScriptTests(unittest.TestCase):
    def test_script_starts_and_configures_clickhouse_for_local_worker(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("clickhouse", script)
        self.assertIn(
            'export ORCHESTRATOR_CLICKHOUSE_HTTP_URL="${ORCHESTRATOR_CLICKHOUSE_HTTP_URL:-http://127.0.0.1:8123}"',
            script,
        )
        self.assertNotIn("ORCHESTRATOR_REDIS_URL", script)

    def test_script_restarts_existing_local_worker_before_launch(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("local_worker_candidate_pids()", script)
        self.assertIn("restart_existing_local_worker_if_owned()", script)
        self.assertIn('awk \'/[[:space:]]-m orchestrator worker-runs([[:space:]]|$)/ { print $1 }\'', script)
        self.assertIn('if [[ "$pid_cwd" == "$ROOT_DIR" ]]', script)
        self.assertIn('echo "Stopping existing local run worker..."', script)
        self.assertLess(
            script.index("restart_existing_local_worker_if_owned"),
            script.index('"${VENV_DIR}/bin/python" -m orchestrator worker-runs'),
        )

    def test_script_includes_deployment_host_services_and_one_shot_bootstrap_readiness(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("deployment-host-bootstrap", script)
        self.assertIn("deployment-host-agent", script)
        self.assertIn('if [[ "$service_name" == "deployment-host-bootstrap" && "$state_status" == "exited" ]]', script)
        self.assertIn('exit_code="$(docker inspect --format \'{{.State.ExitCode}}\' "$container_id" 2>/dev/null || true)"', script)
        self.assertIn('if [[ "$exit_code" == "0" ]]', script)


if __name__ == "__main__":
    unittest.main()
