from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerBuildContractTests(unittest.TestCase):
    def test_compose_services_use_role_specific_docker_targets(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        expected_targets = {
            "api:": "app-runtime-base",
            "run-worker:": "android-runtime",
            "temporal-orchestrator:": "app-runtime-base",
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
        deps_install = '"${VIRTUAL_ENV}/bin/pip" install -r /tmp/requirements-base.txt'
        app_source_copy = "COPY orchestrator ./orchestrator"
        local_console_shim = 'exec python -m orchestrator "$@"'

        self.assertIn(deps_copy, dockerfile)
        self.assertIn(deps_install, dockerfile)
        self.assertIn(app_source_copy, dockerfile)
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
            dockerfile.index(local_console_shim),
            msg="The console shim should be created after the app source copy.",
        )

    def test_voice_target_installs_voice_dependencies_before_source_copy(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")

        voice_target = dockerfile.split("FROM python-common-base AS voice-runtime", 1)[1]
        voice_deps_install = '"${VIRTUAL_ENV}/bin/pip" install -r /tmp/requirements-voice.txt'
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
        self.assertIn("sdkmanager", android_target)
        self.assertIn("default-jdk-headless", android_target)
        self.assertIn("maven", android_target)

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
