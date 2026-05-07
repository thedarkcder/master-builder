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
            "webhook-worker:": "app-runtime-base",
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
        bootstrap_script_copy = "COPY scripts/bootstrap_deployment_host_agent.py ./scripts/bootstrap_deployment_host_agent.py"
        local_install = '"${VIRTUAL_ENV}/bin/pip" install --no-deps --no-build-isolation .'
        source_compile = '"${VIRTUAL_ENV}/bin/python" -m compileall -q -j 0 /app/orchestrator'

        self.assertIn(deps_copy, dockerfile)
        self.assertIn(deps_install, dockerfile)
        self.assertIn(app_source_copy, dockerfile)
        self.assertIn(bootstrap_script_copy, dockerfile)
        self.assertIn(local_install, dockerfile)
        self.assertIn(source_compile, dockerfile)

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
            dockerfile.index(local_install),
            msg="Local package install should happen after app source and runtime script copy.",
        )
        self.assertLess(
            dockerfile.index(local_install),
            dockerfile.index(source_compile),
            msg="API image should precompile app bytecode after local package installation.",
        )

    def test_voice_target_installs_voice_dependencies_before_source_copy(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")

        voice_target = dockerfile.split("FROM python-common-base AS voice-runtime", 1)[1]
        voice_deps_install = '"${VIRTUAL_ENV}/bin/pip" install -r /tmp/requirements-voice.txt'
        app_source_copy = "COPY orchestrator ./orchestrator"
        local_install = '"${VIRTUAL_ENV}/bin/pip" install --no-deps --no-build-isolation .'

        self.assertIn(voice_deps_install, voice_target)
        self.assertLess(
            voice_target.index(voice_deps_install),
            voice_target.index(app_source_copy),
            msg="Voice dependencies must be cached ahead of app source changes.",
        )
        self.assertLess(
            voice_target.index(app_source_copy),
            voice_target.index(local_install),
            msg="Local package install should remain the last Python install step in the voice target.",
        )

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

    def test_dockerignore_excludes_local_workdirs_from_build_context(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(
            ".workdirs",
            {line.strip() for line in dockerignore},
            msg="Local run workdirs should not be sent into the Docker build context.",
        )

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
            "pocket-tts-cache:/root/.cache/pocket_tts",
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


if __name__ == "__main__":
    unittest.main()
