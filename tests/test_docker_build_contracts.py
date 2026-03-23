from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DockerBuildContractTests(unittest.TestCase):
    def test_python_dependencies_are_installed_before_app_source_copy(self) -> None:
        dockerfile = (ROOT / "orchestrator" / "Dockerfile").read_text(encoding="utf-8")

        deps_copy = 'COPY pyproject.toml ./'
        deps_install = '"${VIRTUAL_ENV}/bin/pip" install -r /tmp/requirements.txt'
        app_source_copy = "COPY orchestrator ./orchestrator"
        local_install = '"${VIRTUAL_ENV}/bin/pip" install --no-deps .'

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

    def test_dockerignore_excludes_local_workdirs_from_build_context(self) -> None:
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertIn(
            ".workdirs",
            {line.strip() for line in dockerignore},
            msg="Local run workdirs should not be sent into the Docker build context.",
        )


if __name__ == "__main__":
    unittest.main()
