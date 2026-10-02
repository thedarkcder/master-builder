from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_local_stack_publishes_only_loopback_ports():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name, service in services.items():
        for port in service.get("ports", []):
            assert port.startswith("127.0.0.1:"), (
                f"{name} exposes {port} outside loopback"
            )


def test_local_stack_requires_private_service_credentials():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for service, key in (
        ("postgres", "POSTGRES_PASSWORD"),
        ("seaweedfs", "SEAWEEDFS_ADMIN_SECRET_KEY"),
        ("seaweedfs-s3", "ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY"),
        ("grafana", "GF_SECURITY_ADMIN_PASSWORD"),
        ("clickhouse", "CLICKHOUSE_PASSWORD"),
    ):
        assert ":?" in services[service]["environment"][key]
    assert services["grafana"]["environment"]["GF_AUTH_ANONYMOUS_ENABLED"] == "false"
    assert ":?" in services["api"]["environment"]["ORCHESTRATOR_ADMIN_TOKEN_SECRET"]
    assert ":?" in services["api"]["environment"]["ORCHESTRATOR_AUTH_TOKEN_SECRET"]


def test_image_contexts_exclude_local_configuration():
    for filename in (".dockerignore", "admin-ui/.dockerignore"):
        patterns = (ROOT / filename).read_text().splitlines()
        assert ".env" in patterns
        assert ".env.*" in patterns


def test_runtime_image_does_not_bake_database_credentials():
    instructions = (ROOT / "orchestrator/Dockerfile").read_text().splitlines()
    assert not any(
        line.startswith("ENV ORCHESTRATOR_DATABASE_URL=") for line in instructions
    ), "Database credentials must be supplied by the deployment environment"


def test_deployment_packages_supply_required_authentication_configuration():
    for filename in ("docker-compose.yml", "deploy/hetzner/docker-compose.prod.yml"):
        services = yaml.safe_load((ROOT / filename).read_text())["services"]
        for name, service in services.items():
            environment = service.get("environment", {})
            if "ORCHESTRATOR_DATABASE_URL" not in environment:
                continue
            for key in (
                "ORCHESTRATOR_ADMIN_TOKEN_SECRET",
                "ORCHESTRATOR_AUTH_TOKEN_SECRET",
            ):
                assert key in environment, f"{filename}: {name} lacks {key}"
                assert ":?" in environment[key]
