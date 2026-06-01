from pathlib import Path
import unittest


class RunHybridWorkersScriptTests(unittest.TestCase):
    def test_script_starts_and_configures_clickhouse_for_local_worker(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("clickhouse", script)
        self.assertIn(
            'export ORCHESTRATOR_CLICKHOUSE_HTTP_URL="${ORCHESTRATOR_CLICKHOUSE_HTTP_URL:-http://127.0.0.1:${MASTER_BUILDER_CLICKHOUSE_HTTP_PORT}}"',
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

    def test_script_starts_and_bootstraps_local_coolify_when_configured(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn('LOCAL_COOLIFY_ENABLED="${LOCAL_COOLIFY_ENABLED:-auto}"', script)
        self.assertIn('LOCAL_COOLIFY_DIR="${LOCAL_COOLIFY_DIR:-${HOME}/.master-builder-coolify/source}"', script)
        self.assertIn('LOCAL_COOLIFY_PROXY_DIR="${LOCAL_COOLIFY_PROXY_DIR:-${LOCAL_COOLIFY_DIR%/source}/proxy}"', script)
        self.assertIn(
            'LOCAL_COOLIFY_COMPOSE_PROJECT_NAME="${LOCAL_COOLIFY_COMPOSE_PROJECT_NAME:-master-builder-coolify}"',
            script,
        )
        self.assertIn('LOCAL_PREVIEW_PROXY_PORT="${LOCAL_PREVIEW_PROXY_PORT:-8088}"', script)
        self.assertIn("local_coolify_config_available()", script)
        self.assertIn("local_coolify_proxy_config_available()", script)
        self.assertIn("start_local_coolify_if_configured()", script)
        self.assertIn("start_local_coolify_proxy()", script)
        self.assertIn("bootstrap_local_coolify_if_configured()", script)
        self.assertIn("ensure_local_coolify_env()", script)
        self.assertIn("MUX_ENABLED=false", script)
        self.assertIn("docker network inspect coolify", script)
        self.assertIn("docker network create coolify", script)
        self.assertIn(
            'docker compose --project-name "$LOCAL_COOLIFY_COMPOSE_PROJECT_NAME" --env-file .env -f docker-compose.yml -f docker-compose.prod.yml up -d',
            script,
        )
        self.assertIn('docker compose -f docker-compose.yml up -d --force-recreate', script)
        self.assertIn('"${ROOT_DIR}/scripts/bootstrap_local_coolify.py"', script)
        self.assertIn('LOCAL_PREVIEW_BASE_DOMAIN="${LOCAL_PREVIEW_BASE_DOMAIN:-}"', script)
        self.assertIn('LOCAL_PREVIEW_LAN_IP="${LOCAL_PREVIEW_LAN_IP:-}"', script)
        self.assertIn('LOCAL_PREVIEW_PROXY_PORT="$LOCAL_PREVIEW_PROXY_PORT"', script)
        self.assertLess(script.rindex("ensure_local_coolify_env"), script.rindex("docker compose --project-name"))
        self.assertIn('  start_local_coolify_proxy\n  wait_for_local_coolify_ready', script)
        self.assertLess(script.rindex("start_local_coolify_if_configured"), script.rindex("run_compose_up"))
        self.assertLess(
            script.rindex('wait_for_local_database_ready "$DOCKER_WAIT_TIMEOUT_SECONDS"'),
            script.rindex("bootstrap_local_coolify_if_configured"),
        )

    def test_script_uses_fingerprint_gated_docker_builds(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn('HYBRID_DOCKER_BUILD_MODE="${HYBRID_DOCKER_BUILD_MODE:-auto}"', script)
        self.assertIn("DOCKER_BUILD_SERVICES=(", script)
        build_services_block = script[
            script.index("DOCKER_BUILD_SERVICES=(") : script.index("sha256_stream()")
        ]
        self.assertIn("deployment-host-bootstrap", build_services_block)
        self.assertIn("deployment-host-agent", build_services_block)
        self.assertIn("docker_build_images_available()", script)
        self.assertIn("Docker build fingerprint missing but Compose images exist", script)
        self.assertIn("docker_build_required()", script)
        self.assertIn("compose_build_args=()", script)
        self.assertIn('docker_compose build "${DOCKER_BUILD_SERVICES[@]}"', script)
        self.assertIn('compose_build_args=(--no-build)', script)
        self.assertIn("record_docker_build_fingerprint", script)
        self.assertNotIn("up --build -d --remove-orphans", script)
        self.assertNotIn("--exit-code-from migrate migrate", script)

        build_index = script.index('docker_compose build "${DOCKER_BUILD_SERVICES[@]}"')
        record_index = script.index("record_docker_build_fingerprint", build_index)
        self.assertLess(build_index, record_index)
        self.assertLess(
            record_index,
            script.index("docker_compose run --rm --no-deps migrate"),
        )

    def test_script_runs_migrations_between_infra_and_runtime_services(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        infra_index = script.index('"${DOCKER_BASE_SERVICES[@]}"')
        migrate_index = script.index("docker_compose run --rm --no-deps migrate")
        runtime_index = script.index('"${DOCKER_RUNTIME_SERVICES[@]}"', migrate_index)

        self.assertIn("DOCKER_RUNTIME_SERVICES=(", script)
        self.assertIn("DOCKER_POST_API_SERVICES=(", script)
        self.assertIn("DOCKER_STOP_BEFORE_MIGRATION_SERVICES=(", script)
        self.assertIn("--no-deps -d --remove-orphans", script)
        self.assertLess(infra_index, migrate_index)
        self.assertLess(migrate_index, runtime_index)

    def test_script_forces_container_database_url_for_compose_services(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn(
            'DOCKER_DATABASE_URL="${MASTER_BUILDER_DOCKER_DATABASE_URL:-postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator}"',
            script,
        )
        self.assertIn(
            'ORCHESTRATOR_DATABASE_URL="$DOCKER_DATABASE_URL" POSTGRES_URL="$DOCKER_DATABASE_URL" docker compose "$@"',
            script,
        )

    def test_script_includes_voice_workers_in_default_hybrid_startup(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("DOCKER_BASE_SERVICES=(", script)
        self.assertIn("DOCKER_VOICE_SERVICES=(", script)
        self.assertIn('DOCKER_RUNTIME_SERVICES=("${DOCKER_BASE_RUNTIME_SERVICES[@]}" "${DOCKER_VOICE_SERVICES[@]}")', script)
        self.assertIn('DOCKER_APP_SERVICES=("${DOCKER_BASE_APP_SERVICES[@]}" "${DOCKER_VOICE_SERVICES[@]}")', script)
        self.assertNotIn("HYBRID_ENABLE_VOICE", script)

    def test_script_skips_local_editable_install_when_dependency_inputs_are_unchanged(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("local_python_install_required()", script)
        self.assertIn("record_local_python_install_fingerprint", script)
        self.assertIn('echo "Local Python dependencies are current; skipping editable install."', script)

    def test_script_waits_for_local_database_before_starting_local_worker(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("wait_for_local_database_ready()", script)
        self.assertIn("local_database_ready()", script)
        self.assertIn('echo "Waiting for local database to accept SQL connections..."', script)
        self.assertLess(
            script.index("wait_for_local_database_ready"),
            script.index('"${VENV_DIR}/bin/python" -m orchestrator worker-runs'),
        )

    def test_script_defaults_public_local_api_port_away_from_common_port_4000(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn('export ADMIN_UI_PORT="${ADMIN_UI_PORT:-${MASTER_BUILDER_ADMIN_UI_PORT:-60002}}"', script)
        self.assertIn('export MASTER_BUILDER_API_PORT="${MASTER_BUILDER_API_PORT:-60001}"', script)
        self.assertIn('export MASTER_BUILDER_POSTGRES_PORT="${MASTER_BUILDER_POSTGRES_PORT:-60003}"', script)
        self.assertIn("LOCAL_PUBLIC_API_BASE_URL=\"http://localhost:${MASTER_BUILDER_API_PORT}\"", script)
        self.assertIn("LOCAL_ADMIN_UI_BASE_URL=\"http://localhost:${ADMIN_UI_PORT}\"", script)
        self.assertIn(
            "LOCAL_DATABASE_URL=\"postgresql+psycopg://orchestrator:orchestrator@127.0.0.1:${MASTER_BUILDER_POSTGRES_PORT}/orchestrator\"",
            script,
        )
        self.assertIn(
            'if [[ -z "${ORCHESTRATOR_ADMIN_UI_BASE_URL:-}" || "${ORCHESTRATOR_ADMIN_UI_BASE_URL}" == "http://localhost:4100" || "${ORCHESTRATOR_ADMIN_UI_BASE_URL}" == "http://127.0.0.1:4100" ]]; then',
            script,
        )
        self.assertIn('export ORCHESTRATOR_ADMIN_UI_BASE_URL="$LOCAL_ADMIN_UI_BASE_URL"', script)
        self.assertIn(
            'if [[ -z "${NEXT_PUBLIC_API_BASE_URL:-}" || "${NEXT_PUBLIC_API_BASE_URL}" == "http://localhost:4000" ]]; then',
            script,
        )
        self.assertIn('export NEXT_PUBLIC_API_BASE_URL="$LOCAL_PUBLIC_API_BASE_URL"', script)

    def test_script_rewrites_only_old_default_local_database_port(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn(
            'if [[ -z "${ORCHESTRATOR_DATABASE_URL:-}" || "${ORCHESTRATOR_DATABASE_URL}" == *"localhost:4402"* || "${ORCHESTRATOR_DATABASE_URL}" == *"127.0.0.1:4402"* ]]; then',
            script,
        )
        self.assertIn('export ORCHESTRATOR_DATABASE_URL="$LOCAL_DATABASE_URL"', script)
        self.assertIn(
            'if [[ -z "${POSTGRES_URL:-}" || "${POSTGRES_URL}" == *"localhost:4402"* || "${POSTGRES_URL}" == *"127.0.0.1:4402"* ]]; then',
            script,
        )
        self.assertIn('export POSTGRES_URL="$ORCHESTRATOR_DATABASE_URL"', script)

    def test_script_loads_env_file_before_deriving_port_defaults(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertLess(
            script.index('done < .env'),
            script.index('LOCAL_PUBLIC_API_BASE_URL="http://localhost:${MASTER_BUILDER_API_PORT}"'),
        )

    def test_script_stops_owned_docker_admin_ui_before_local_dev_ui(self) -> None:
        script_path = Path("scripts/run_hybrid_workers.sh")
        script = script_path.read_text(encoding="utf-8")

        self.assertIn("compose_admin_ui_container_id()", script)
        self.assertIn("compose_admin_ui_publishes_admin_port()", script)
        self.assertIn("wait_for_admin_ui_port_free()", script)
        self.assertIn("docker_compose ps -q admin-ui", script)
        self.assertIn(".NetworkSettings.Ports", script)
        self.assertIn("grep -qx \"$ADMIN_UI_PORT\"", script)
        self.assertIn("docker_compose stop admin-ui", script)
        self.assertIn('echo "Stopping existing Docker admin UI on port ${ADMIN_UI_PORT}..."', script)
        self.assertLess(
            script.index("if compose_admin_ui_publishes_admin_port; then"),
            script.index('echo "Port ${ADMIN_UI_PORT} is already in use by a non-admin-ui process'),
        )


if __name__ == "__main__":
    unittest.main()
