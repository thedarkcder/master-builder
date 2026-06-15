from __future__ import annotations

from pathlib import Path

import yaml


def test_env_example_declares_local_qa_demo_artifact_settings() -> None:
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    text = env_example.read_text()

    assert "MASTER_BUILDER_MINIO_API_PORT=60015" in text
    assert "MASTER_BUILDER_MINIO_CONSOLE_PORT=60016" in text
    assert "MINIO_ROOT_USER=masterbuilder" in text
    assert "MINIO_ROOT_PASSWORD=masterbuildersecret" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT=127.0.0.1:60015" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT_INTERNAL=minio:9000" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET=qa-demos" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL=http://127.0.0.1:60015/qa-demos" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_SECURE=false" in text
    assert "ORCHESTRATOR_QA_DEMO_RECORDER_PROCESS_TIMEOUT_SECONDS=900" in text
    assert "ORCHESTRATOR_QA_DEMO_IOS_RECORDER_COMMAND=" in text
    assert "ORCHESTRATOR_QA_DEMO_ANDROID_RECORDER_COMMAND=" in text


def test_docker_compose_provisions_local_qa_demo_artifact_storage() -> None:
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    payload = yaml.safe_load(compose_path.read_text())
    services = payload["services"]

    assert "minio" in services
    assert "minio-init" in services
    assert services["minio"]["ports"] == [
        "${MASTER_BUILDER_MINIO_API_PORT:-60015}:9000",
        "${MASTER_BUILDER_MINIO_CONSOLE_PORT:-60016}:9001",
    ]
    assert services["minio-init"]["depends_on"]["minio"]["condition"] == "service_healthy"
    assert services["run-worker"]["depends_on"]["minio-init"]["condition"] == "service_completed_successfully"
    assert (
        services["temporal-orchestrator"]["depends_on"]["minio-init"]["condition"]
        == "service_completed_successfully"
    )

    for service_name in ("run-worker", "temporal-orchestrator"):
        environment = services[service_name]["environment"]
        assert environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT"] == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT_INTERNAL:-minio:9000}"
        assert environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET"] == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET:-qa-demos}"
        assert environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL"] == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL:-http://127.0.0.1:60015/qa-demos}"
        assert environment["ORCHESTRATOR_QA_DEMO_RECORDER_PROCESS_TIMEOUT_SECONDS"] == "${ORCHESTRATOR_QA_DEMO_RECORDER_PROCESS_TIMEOUT_SECONDS:-900}"
        assert environment["ORCHESTRATOR_QA_DEMO_IOS_RECORDER_COMMAND"] == "${ORCHESTRATOR_QA_DEMO_IOS_RECORDER_COMMAND:-}"
        assert environment["ORCHESTRATOR_QA_DEMO_ANDROID_RECORDER_COMMAND"] == "${ORCHESTRATOR_QA_DEMO_ANDROID_RECORDER_COMMAND:-}"

    for service_name in ("api", "webhook-worker"):
        environment = services[service_name]["environment"]
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL"]
            == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL:-http://127.0.0.1:60015/qa-demos}"
        )
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_URL_TIMEOUT_SECONDS"]
            == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_URL_TIMEOUT_SECONDS:-10}"
        )
