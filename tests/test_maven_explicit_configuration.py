from pathlib import Path

import yaml

from orchestrator.core.deployment_setup.planner import (
    DeploymentPlan,
    _normalize_maven_service_builds,
)


def test_maven_build_preserves_explicit_configuration_without_private_injection(
    tmp_path: Path,
) -> None:
    (tmp_path / "pom.xml").write_text(
        "<project><artifactId>example-api</artifactId></project>"
    )
    resources = tmp_path / "src" / "main" / "resources"
    resources.mkdir(parents=True)
    properties = resources / "application.properties"
    properties.write_text("spring.config.import=optional:file:./custom.properties\n")
    explicit_environment = {
        "SPRING_PROFILES_ACTIVE": "custom",
        "APP_DATABASE_URL": "${APP_DATABASE_URL:?required}",
    }
    compose = {
        "services": {
            "api": {
                "environment": explicit_environment,
                "depends_on": {"database": {"condition": "service_started"}},
                "healthcheck": {"test": ["CMD", "java", "-version"]},
            },
            "database": {
                "image": "postgres:16",
                "environment": {"POSTGRES_PASSWORD": "${POSTGRES_PASSWORD:?required}"},
            },
        }
    }
    plan = DeploymentPlan.model_validate(
        {
            "name": "example",
            "services": [
                {
                    "key": "api",
                    "name": "API",
                    "kind": "api",
                    "source_path": ".",
                    "build_strategy": "maven",
                    "compose_service": "api",
                    "container_port": 8080,
                }
            ],
            "routes": [{"service_key": "api", "visibility": "public"}],
            "resources": [
                {
                    "key": "database",
                    "kind": "postgres",
                    "name": "Database",
                    "config": {"compose_service": "database"},
                }
            ],
            "compose_raw": yaml.safe_dump(compose),
        }
    )
    result = _normalize_maven_service_builds(plan=plan, checkout_path=str(tmp_path))
    result_compose = yaml.safe_load(result.compose_raw)
    service = result_compose["services"]["api"]
    assert service["environment"] == explicit_environment
    assert service["depends_on"] == compose["services"]["api"]["depends_on"]
    assert service["healthcheck"] == compose["services"]["api"]["healthcheck"]
    assert result_compose["services"]["database"] == compose["services"]["database"]
    dockerfile = service["build"]["dockerfile_inline"]
    assert "RUN mvn -pl . -am" in dockerfile
    assert 'ENTRYPOINT ["java","-jar","/app/app.jar"]' in dockerfile
    assert "master-builder-config" not in dockerfile
    assert "master-builder-logback" not in dockerfile
    assert "spring[.]config[.]import" not in dockerfile
    assert (
        properties.read_text()
        == "spring.config.import=optional:file:./custom.properties\n"
    )


def test_maven_generated_build_quotes_module_paths_and_rejects_ambiguous_jars(
    tmp_path: Path,
) -> None:
    import shlex
    import subprocess

    from orchestrator.core.deployment_setup.planner import (
        DeploymentPlanService,
        _MavenBuildScope,
        _maven_runtime_dockerfile,
    )

    module_path = "example module;echo unexpected"
    service = DeploymentPlanService(
        key="api",
        name="API",
        kind="api",
        source_path=module_path,
        build_strategy="maven",
        compose_service="api",
        container_port=8080,
    )
    dockerfile = _maven_runtime_dockerfile(
        service=service,
        scope=_MavenBuildScope(context_path=".", module_path=module_path),
    )
    build_line = next(
        line for line in dockerfile.splitlines() if line.startswith("RUN mvn")
    )
    assert shlex.split(build_line)[:4] == ["RUN", "mvn", "-pl", module_path]
    selection = next(
        line.removeprefix("RUN ")
        for line in dockerfile.splitlines()
        if line.startswith("RUN set -eu; find")
    )
    target = tmp_path / "target"
    target.mkdir()
    selection = selection.replace(
        shlex.quote(f"/workspace/{module_path}/target"), shlex.quote(str(target))
    )
    selection = selection.replace(
        "/tmp/runtime-jars", shlex.quote(str(tmp_path / "runtime-jars"))
    )
    selection = selection.replace(
        "/tmp/app.jar", shlex.quote(str(tmp_path / "app.jar"))
    )
    selection = selection.replace(
        "$$", "$"
    )  # Compose unescapes literal Dockerfile dollar signs.
    missing = subprocess.run(
        ["sh", "-c", selection], capture_output=True, text=True, check=False
    )
    assert missing.returncode != 0 and "exactly one runtime JAR" in missing.stderr
    (target / "app.jar").write_text("runtime artifact")
    selected = subprocess.run(
        ["sh", "-c", selection], capture_output=True, text=True, check=False
    )
    assert selected.returncode == 0
    assert (tmp_path / "app.jar").read_text() == "runtime artifact"
    (target / "other.jar").write_text("second runtime artifact")
    ambiguous = subprocess.run(
        ["sh", "-c", selection], capture_output=True, text=True, check=False
    )
    assert ambiguous.returncode != 0 and "exactly one runtime JAR" in ambiguous.stderr
