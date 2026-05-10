from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
LONG_RUNNING_SERVICES = (
    "api",
    "run-worker",
    "webhook-worker",
    "project-automation",
    "discord-gateway",
    "temporal-worker",
    "discord-live-voice",
    "knowledge-sync",
)


def _service_block(compose: str, service_name: str) -> str:
    marker = f"  {service_name}:"
    start = compose.index(marker)
    next_service = compose.find("\n  ", start + len(marker))
    while next_service != -1:
        candidate = compose[next_service + 1 :]
        line = candidate.splitlines()[0]
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
            return compose[start:next_service]
        next_service = compose.find("\n  ", next_service + 1)
    return compose[start:]


class DeploymentMigrationStartupTests(unittest.TestCase):
    def test_compose_files_use_one_shot_migration_service(self) -> None:
        for relative_path in ("docker-compose.yml", "deploy/hetzner/docker-compose.prod.yml"):
            compose = (ROOT / relative_path).read_text(encoding="utf-8")
            migrate_block = _service_block(compose, "migrate")

            for expected_line in ("- python", "- -m", "- orchestrator", "- migrate"):
                self.assertIn(expected_line, migrate_block)
            self.assertIn('restart: "no"', migrate_block)

            for service_name in LONG_RUNNING_SERVICES:
                if f"  {service_name}:" not in compose:
                    continue
                service_block = _service_block(compose, service_name)
                self.assertNotIn(
                    "python -m orchestrator migrate",
                    service_block,
                    msg=f"{service_name} in {relative_path} must not run migrations during service startup.",
                )
                self.assertIn(
                    "migrate:",
                    service_block,
                    msg=f"{service_name} in {relative_path} must depend on the one-shot migration service.",
                )
                self.assertIn(
                    "condition: service_completed_successfully",
                    service_block,
                    msg=f"{service_name} in {relative_path} must wait for migrations to complete.",
                )

    def test_dockerfile_default_commands_do_not_run_migrations(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")

        self.assertNotIn("python -m orchestrator migrate && uvicorn", dockerfile)

    def test_hetzner_bootstrap_runs_migration_job_before_runtime_stack(self) -> None:
        bootstrap = (ROOT / "deploy" / "hetzner" / "bootstrap.sh").read_text(encoding="utf-8")

        migrate_index = bootstrap.index("up --build --force-recreate --exit-code-from migrate migrate")
        stack_index = bootstrap.index("up -d --build")

        self.assertLess(
            migrate_index,
            stack_index,
            msg="Hetzner bootstrap must run the explicit migration job before starting long-running services.",
        )

    def test_hybrid_worker_script_runs_migration_job_before_runtime_stack(self) -> None:
        script = (ROOT / "scripts" / "run_hybrid_workers.sh").read_text(encoding="utf-8")

        migrate_index = script.index("up --build --force-recreate --exit-code-from migrate migrate")
        stack_index = script.index("up --build -d --remove-orphans")

        self.assertLess(
            migrate_index,
            stack_index,
            msg="Hybrid worker launcher must run the explicit migration job before starting Docker services.",
        )


if __name__ == "__main__":
    unittest.main()
