from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def _compose_service_block(compose: str, service_name: str) -> str:
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


class DockerBuildContractTests(unittest.TestCase):
    def test_compose_services_use_role_specific_docker_targets(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        expected_targets = {
            "api:": "app-runtime-base",
            "run-worker:": "android-runtime",
            "temporal-orchestrator:": "android-runtime",
            "webhook-worker:": "voice-runtime",
            "knowledge-sync:": "app-runtime-base",
            "discord-gateway:": "voice-runtime",
            "discord-live-voice:": "voice-runtime",
        }

        for service_marker, target in expected_targets.items():
            service_block = compose.split(service_marker, 1)[1]
            self.assertIn(
                f"target: {target}",
                service_block,
                msg=f"{service_marker.rstrip(':')} should build from target {target}.",
            )

        self.assertNotIn("worker-runtime:", compose, msg="Compose should no longer use the mixed worker service.")

    def test_python_dependencies_are_installed_before_app_source_copy(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")

        deps_copy = 'COPY pyproject.toml ./'
        deps_install = '"${VIRTUAL_ENV}/bin/pip" install --retries 10 --timeout 120 -r /tmp/requirements-base.txt'
        app_source_copy = "COPY orchestrator ./orchestrator"
        bootstrap_script_copy = "COPY scripts/bootstrap_deployment_host_agent.py ./scripts/bootstrap_deployment_host_agent.py"
        source_compile = '"${VIRTUAL_ENV}/bin/python" -m compileall -q -j 0 /app/orchestrator'
        local_console_shim = 'exec python -m orchestrator "$@"'

        self.assertIn(deps_copy, dockerfile)
        self.assertIn(deps_install, dockerfile)
        self.assertIn(app_source_copy, dockerfile)
        self.assertIn(bootstrap_script_copy, dockerfile)
        self.assertIn(source_compile, dockerfile)
        self.assertIn(local_console_shim, dockerfile)
        self.assertNotIn(
            '"${VIRTUAL_ENV}/bin/pip" install --no-deps --no-build-isolation .',
            dockerfile,
            msg="Runtime image rebuilds must not spend time building and reinstalling the local wheel.",
        )

        self.assertLess(
            dockerfile.index(deps_copy),
            dockerfile.index(deps_install),
            msg="Dependency manifest must be copied before third-party dependency install.",
        )
        self.assertLess(
            dockerfile.index(deps_install),
            dockerfile.index(app_source_copy),
            msg="Third-party dependency install must be cached ahead of app source changes.",
        )
        self.assertLess(
            dockerfile.index(app_source_copy),
            dockerfile.index(bootstrap_script_copy),
            msg="Deployment-host bootstrap script should be copied with the app runtime sources.",
        )
        self.assertLess(
            dockerfile.index(bootstrap_script_copy),
            dockerfile.index(local_console_shim),
            msg="The console shim should be created after the deployment host bootstrap script copy.",
        )
        self.assertLess(
            dockerfile.index(local_console_shim),
            dockerfile.index(source_compile),
            msg="API image should precompile app bytecode after the console shim is created.",
        )

    def test_voice_target_installs_voice_dependencies_before_source_copy(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")

        voice_target = dockerfile.split("FROM python-common-base AS voice-runtime", 1)[1]
        voice_deps_install = (
            '"${VIRTUAL_ENV}/bin/pip" install --retries 10 --timeout 120 -r /tmp/requirements-voice.txt'
        )
        app_source_copy = "COPY orchestrator ./orchestrator"
        local_console_shim = 'exec python -m orchestrator "$@"'

        self.assertIn(voice_deps_install, voice_target)
        self.assertLess(
            voice_target.index(voice_deps_install),
            voice_target.index(app_source_copy),
            msg="Voice dependencies must be cached ahead of app source changes.",
        )
        self.assertLess(
            voice_target.index(app_source_copy),
            voice_target.index(local_console_shim),
            msg="The console shim should remain after app source copy in the voice target.",
        )

    def test_qa_demo_recorder_helper_is_copied_to_runtime_images(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")
        helper_copy = "COPY scripts/qa_demo_release_context.py ./scripts/qa_demo_release_context.py"

        app_target = dockerfile.split("FROM python-common-base AS app-runtime-base", 1)[1].split(
            "FROM python-common-base AS python-android-base",
            1,
        )[0]
        android_target = dockerfile.split("FROM python-android-base AS android-runtime", 1)[1].split(
            "FROM python-common-base AS voice-runtime",
            1,
        )[0]

        self.assertIn(helper_copy, app_target)
        self.assertIn(helper_copy, android_target)

    def test_default_voice_image_keeps_existing_local_ml_dependencies(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        voice_extra = pyproject.split("voice = [", 1)[1].split("]", 1)[0]

        self.assertIn("faster-whisper", voice_extra)
        self.assertIn("pocket-tts", voice_extra)
        self.assertIn("py-cord[voice", voice_extra)
        self.assertIn("ORCHESTRATOR_VOICE_STT_PROVIDER: ${ORCHESTRATOR_VOICE_STT_PROVIDER:-whisper}", compose)
        self.assertIn("ORCHESTRATOR_VOICE_TTS_PROVIDER: ${ORCHESTRATOR_VOICE_TTS_PROVIDER:-pocket_tts}", compose)
        self.assertNotIn("INSTALL_LOCAL_STT", compose)
        self.assertNotIn("INSTALL_LOCAL_TTS", compose)

    def test_base_target_omits_android_and_voice_transport_layers(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")
        base_target = dockerfile.split("FROM python-common-base AS app-runtime-base", 1)[1].split(
            "FROM python-common-base AS python-android-base",
            1,
        )[0]

        self.assertNotIn("ANDROID_SDK_ROOT", base_target)
        self.assertNotIn("sdkmanager", base_target)
        self.assertNotIn("live-voice-transport-builder", base_target)
        self.assertNotIn("/usr/local/bin/live-voice-transport", base_target)

    def test_android_target_keeps_android_tooling_out_of_the_common_base(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")
        android_target = dockerfile.split("FROM python-common-base AS python-android-base", 1)[1].split(
            "FROM python-android-base AS android-runtime",
            1,
        )[0]

        self.assertIn("ANDROID_SDK_ROOT", android_target)
        self.assertIn('/usr/lib/android-sdk/build-tools/debian', android_target)
        self.assertIn("adb", android_target)
        self.assertIn("aapt", android_target)
        self.assertIn("android-sdk-build-tools", android_target)
        self.assertIn("android-sdk-platform-tools", android_target)
        self.assertIn("sdkmanager", android_target)
        self.assertIn("default-jdk-headless", android_target)
        self.assertIn("maven", android_target)
        self.assertNotIn('"platform-tools"', android_target)
        self.assertNotIn('"build-tools;', android_target)
        self.assertNotIn('"emulator"', android_target)
        self.assertNotIn("system-images;", android_target)

    def test_common_base_installs_browser_runtime_deps_without_playwright_with_deps(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")
        common_base = dockerfile.split("FROM python:3.11-slim-trixie AS python-common-base", 1)[1].split(
            "COPY pyproject.toml ./",
            1,
        )[0]

        self.assertIn("fonts-freefont-ttf", common_base)
        self.assertIn("fonts-unifont", common_base)
        self.assertIn("xvfb", common_base)
        self.assertIn("playwright install chromium", common_base)
        self.assertNotIn("playwright install --with-deps chromium", common_base)
        self.assertNotIn("ttf-unifont", common_base)
        self.assertNotIn("ttf-ubuntu-font-family", common_base)

    def test_common_base_extends_network_timeouts_for_browser_and_python_dependency_downloads(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")
        common_base = dockerfile.split("FROM python:3.11-slim-trixie AS python-common-base", 1)[1].split(
            "FROM python-common-base AS app-runtime-base",
            1,
        )[0]

        self.assertIn("PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000", common_base)
        self.assertIn('"${VIRTUAL_ENV}/bin/pip" install --retries 10 --timeout 120', common_base)

    def test_local_compose_api_does_not_repeat_migrations_after_migrate_service(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        api_block = _compose_service_block(compose, "api")

        self.assertIn("migrate:", api_block)
        self.assertIn("condition: service_completed_successfully", api_block)
        self.assertIn(
            "ORCHESTRATOR_AUTO_MIGRATE_ON_STARTUP: ${ORCHESTRATOR_AUTO_MIGRATE_ON_STARTUP:-false}",
            api_block,
        )

    def test_local_compose_api_defaults_to_single_worker_for_fast_dev_health(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        prod_compose = (ROOT / "deploy" / "hetzner" / "docker-compose.prod.yml").read_text(encoding="utf-8")
        api_block = _compose_service_block(compose, "api")
        prod_api_block = _compose_service_block(prod_compose, "api")

        self.assertIn("ORCHESTRATOR_API_WORKERS: ${ORCHESTRATOR_API_WORKERS:-1}", api_block)
        self.assertIn('uvicorn orchestrator.api.main:app --host 0.0.0.0 --port 4000 --workers "${ORCHESTRATOR_API_WORKERS:-1}"', api_block)
        self.assertIn("ORCHESTRATOR_API_WORKERS: ${ORCHESTRATOR_API_WORKERS:-4}", prod_api_block)
        self.assertIn('uvicorn orchestrator.api.main:app --host 0.0.0.0 --port 4000 --workers "${ORCHESTRATOR_API_WORKERS:-4}"', prod_api_block)

    def test_dockerignore_excludes_local_workdirs_from_build_context(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(
            ".workdirs",
            {line.strip() for line in dockerignore},
            msg="Local run workdirs should not be sent into the Docker build context.",
        )

    def test_local_compose_host_ports_use_master_builder_range(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        expected_ports = (
            "${MASTER_BUILDER_API_PORT:-60001}:4000",
            "${MASTER_BUILDER_ADMIN_UI_PORT:-60002}:4100",
            "${MASTER_BUILDER_POSTGRES_PORT:-60003}:5432",
            "${MASTER_BUILDER_MAILPIT_SMTP_PORT:-60004}:1025",
            "${MASTER_BUILDER_MAILPIT_UI_PORT:-60005}:8025",
            "${MASTER_BUILDER_CLICKHOUSE_HTTP_PORT:-60006}:8123",
            "${MASTER_BUILDER_CLICKHOUSE_NATIVE_PORT:-60007}:9000",
            "${MASTER_BUILDER_TEMPORAL_PORT:-60008}:7233",
            "${MASTER_BUILDER_OTEL_GRPC_PORT:-60009}:4317",
            "${MASTER_BUILDER_OTEL_HTTP_PORT:-60010}:4318",
            "${MASTER_BUILDER_OTEL_METRICS_PORT:-60011}:9464",
            "${MASTER_BUILDER_TEMPO_PORT:-60012}:3200",
            "${MASTER_BUILDER_PROMETHEUS_PORT:-60013}:9090",
            "${MASTER_BUILDER_GRAFANA_PORT:-60014}:3000",
        )

        for expected_port in expected_ports:
            self.assertIn(expected_port, compose)

        self.assertNotIn('"4000:4000"', compose)
        self.assertNotIn('"4100:4100"', compose)
        self.assertNotIn('"4402:5432"', compose)

    def test_run_worker_prewarms_models_before_processing_queue_in_dev_and_prod(self) -> None:
        expected_lines = (
            "HF_HOME: /root/.cache/huggingface",
            "HF_HUB_OFFLINE: ${HF_HUB_OFFLINE:-1}",
            "HF_HUB_OFFLINE=0 python -m orchestrator knowledge-prewarm",
            "python -m orchestrator worker-runs",
            "huggingface-cache:/root/.cache/huggingface",
        )

        for relative_path in ("docker-compose.yml", "deploy/hetzner/docker-compose.prod.yml"):
            compose = (ROOT / relative_path).read_text(encoding="utf-8")
            service_block = compose.split("run-worker:", 1)[1]
            for expected_line in expected_lines:
                self.assertIn(
                    expected_line,
                    service_block,
                    msg=f"run-worker in {relative_path} should preload cached HF models before starting the queue loop.",
                )

    def test_webhook_worker_prewarms_voice_models_before_processing_webhooks_in_dev_and_prod(self) -> None:
        expected_lines = (
            "HF_HOME: /root/.cache/huggingface",
            "HF_HUB_OFFLINE: ${HF_HUB_OFFLINE:-1}",
            "HF_HUB_OFFLINE=0 python -m orchestrator voice-prewarm",
            "python -m orchestrator worker-webhooks",
            "huggingface-cache:/root/.cache/huggingface",
            "ORCHESTRATOR_VOICE_STT_PROVIDER: ${ORCHESTRATOR_VOICE_STT_PROVIDER:-whisper}",
            "ORCHESTRATOR_VOICE_TTS_PROVIDER: ${ORCHESTRATOR_VOICE_TTS_PROVIDER:-pocket_tts}",
        )

        for relative_path in ("docker-compose.yml", "deploy/hetzner/docker-compose.prod.yml"):
            compose = (ROOT / relative_path).read_text(encoding="utf-8")
            service_block = compose.split("webhook-worker:", 1)[1]
            for expected_line in expected_lines:
                self.assertIn(
                    expected_line,
                    service_block,
                    msg=f"webhook-worker in {relative_path} should preload cached voice models before processing webhook events.",
                )

    def test_deployment_host_services_are_bootstrapped_in_local_compose(self) -> None:
        relative_path = "docker-compose.yml"
        compose = (ROOT / relative_path).read_text(encoding="utf-8")
        bootstrap_block = compose.split("deployment-host-bootstrap:", 1)[1]
        agent_block = compose.split("deployment-host-agent:", 1)[1]

        for expected_line in (
            "python",
            "scripts/bootstrap_deployment_host_agent.py",
            "--bootstrap-token-path",
            "/var/lib/master-builder/deployment-host/bootstrap-token",
            "--access-token-path",
            "/var/lib/master-builder/deployment-host/access-token",
            "deployment-host-state:/var/lib/master-builder/deployment-host",
        ):
            self.assertIn(
                expected_line,
                bootstrap_block,
                msg=f"deployment-host-bootstrap in {relative_path} should provision the host bootstrap token into the private state volume.",
            )

        for expected_line in (
            "service_completed_successfully",
            "ORCHESTRATOR_DEPLOYMENT_HOST_AGENT_BOOTSTRAP_TOKEN_PATH: /var/lib/master-builder/deployment-host/bootstrap-token",
            "ORCHESTRATOR_DEPLOYMENT_HOST_AGENT_ACCESS_TOKEN_PATH: /var/lib/master-builder/deployment-host/access-token",
            "deployment-host-agent",
            "- orchestrator",
            "/var/run/docker.sock:/var/run/docker.sock",
            "deployment-host-state:/var/lib/master-builder/deployment-host",
        ):
            self.assertIn(
                expected_line,
                agent_block,
                msg=f"deployment-host-agent in {relative_path} should run automatically after bootstrap and have host Docker access.",
            )

    def test_api_worker_startup_timeout_is_explicit_in_dev_and_prod(self) -> None:
        for relative_path in ("docker-compose.yml", "deploy/hetzner/docker-compose.prod.yml"):
            compose = (ROOT / relative_path).read_text(encoding="utf-8")
            api_block = _compose_service_block(compose, "api")

            self.assertIn(
                "ORCHESTRATOR_UVICORN_WORKER_HEALTHCHECK_TIMEOUT_SECONDS: "
                "${ORCHESTRATOR_UVICORN_WORKER_HEALTHCHECK_TIMEOUT_SECONDS:-180}",
                api_block,
                msg=f"api in {relative_path} should make Uvicorn worker startup timeout configurable.",
            )
            self.assertIn(
                '--timeout-worker-healthcheck "${ORCHESTRATOR_UVICORN_WORKER_HEALTHCHECK_TIMEOUT_SECONDS:-180}"',
                api_block,
                msg=f"api in {relative_path} should not use Uvicorn's 5s default worker startup healthcheck.",
            )

    def test_tailscale_bootstrap_clears_stale_funnel_config_before_enabling_funnel(self) -> None:
        script = (ROOT / "scripts" / "tailscale" / "funnel-bootstrap.sh").read_text(encoding="utf-8")

        funnel_reset = 'tailscale --socket="$SOCKET" funnel reset'
        serve_reset = 'tailscale --socket="$SOCKET" serve reset'
        funnel_enable = 'tailscale --socket="$SOCKET" funnel --bg --yes "$FUNNEL_PORT"'

        self.assertIn(funnel_reset, script)
        self.assertIn(serve_reset, script)
        self.assertIn(funnel_enable, script)
        self.assertLess(script.index(funnel_reset), script.index(funnel_enable))
        self.assertLess(script.index(serve_reset), script.index(funnel_enable))


if __name__ == "__main__":
    unittest.main()
