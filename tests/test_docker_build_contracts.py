from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerBuildContractTests(unittest.TestCase):
    def test_compose_services_use_role_specific_docker_targets(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")

        expected_targets = {
            "api:": "app-runtime-base",
            "worker-runtime:": "android-runtime",
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

    def test_python_dependencies_are_installed_before_app_source_copy(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")

        deps_copy = 'COPY pyproject.toml ./'
        deps_install = '"${VIRTUAL_ENV}/bin/pip" install -r /tmp/requirements-base.txt'
        app_source_copy = "COPY orchestrator ./orchestrator"
        local_install = '"${VIRTUAL_ENV}/bin/pip" install --no-deps --no-build-isolation .'

        self.assertIn(deps_copy, dockerfile)
        self.assertIn(deps_install, dockerfile)
        self.assertIn(app_source_copy, dockerfile)
        self.assertIn(local_install, dockerfile)

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
            dockerfile.index(local_install),
            msg="Local package install should happen after app source copy.",
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


if __name__ == "__main__":
    unittest.main()
