from __future__ import annotations

from pathlib import Path

import yaml


def test_env_example_declares_local_qa_demo_artifact_settings() -> None:
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    text = env_example.read_text()

    assert "MASTER_BUILDER_S3_PORT=60015" in text
    assert "SEAWEEDFS_ADMIN_ACCESS_KEY=masterbuilder-storage-admin" in text
    assert "SEAWEEDFS_ADMIN_SECRET_KEY=\n" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT=127.0.0.1:60015" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT_INTERNAL=seaweedfs-s3:8333" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET=qa-demos" in text
    assert (
        "ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL=http://localhost:60002/api/bff/api/qa-artifacts"
        in text
    )
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_SECURE=false" in text
    assert "ORCHESTRATOR_QA_DEMO_RECORDER_PROCESS_TIMEOUT_SECONDS=900" in text
    assert "ORCHESTRATOR_QA_DEMO_IOS_RECORDER_COMMAND=" in text
    assert "ORCHESTRATOR_QA_DEMO_ANDROID_RECORDER_COMMAND=" in text
    assert "ORCHESTRATOR_QA_DEMO_ANDROID_WORKER_PLATFORM=macos" in text


def test_hybrid_worker_exports_local_qa_demo_artifact_settings() -> None:
    script_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "run_hybrid_workers.sh"
    )
    text = script_path.read_text()

    assert (
        'export ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT="${ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT:?Configure QA storage endpoint}"'
        in text
    )
    assert (
        'export ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL="${ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL:?Configure authenticated QA delivery base URL}"'
        in text
    )
    assert (
        'export ORCHESTRATOR_QA_DEMO_ANDROID_WORKER_PLATFORM="${ORCHESTRATOR_QA_DEMO_ANDROID_WORKER_PLATFORM:-macos}"'
        in text
    )


def test_docker_compose_provisions_local_qa_demo_artifact_storage() -> None:
    compose_path = Path(__file__).resolve().parents[1] / "docker-compose.yml"
    payload = yaml.safe_load(compose_path.read_text())
    services = payload["services"]

    assert "seaweedfs" in services and "seaweedfs-s3" in services
    assert "minio" not in services
    assert services["seaweedfs-s3"]["ports"] == [
        "127.0.0.1:${MASTER_BUILDER_S3_PORT:-60015}:8333"
    ]
    assert (
        services["seaweedfs-init"]["depends_on"]["seaweedfs-s3"]["condition"]
        == "service_healthy"
    )
    for service_name in ("run-worker", "temporal-orchestrator"):
        assert (
            services[service_name]["depends_on"]["seaweedfs-init"]["condition"]
            == "service_completed_successfully"
        )

    for service_name in ("run-worker", "temporal-orchestrator"):
        environment = services[service_name]["environment"]
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT"]
            == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT_INTERNAL:?Configure internal QA storage endpoint}"
        )
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET"]
            == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET:-qa-demos}"
        )
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL"]
            == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL:?Set authenticated QA delivery base URL}"
        )
        assert (
            environment["ORCHESTRATOR_QA_DEMO_RECORDER_PROCESS_TIMEOUT_SECONDS"]
            == "${ORCHESTRATOR_QA_DEMO_RECORDER_PROCESS_TIMEOUT_SECONDS:-900}"
        )
        assert (
            environment["ORCHESTRATOR_QA_DEMO_IOS_RECORDER_COMMAND"]
            == "${ORCHESTRATOR_QA_DEMO_IOS_RECORDER_COMMAND:-}"
        )
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ANDROID_RECORDER_COMMAND"]
            == "${ORCHESTRATOR_QA_DEMO_ANDROID_RECORDER_COMMAND:-}"
        )
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ANDROID_WORKER_PLATFORM"]
            == "${ORCHESTRATOR_QA_DEMO_ANDROID_WORKER_PLATFORM:-macos}"
        )

    for service_name in ("api", "webhook-worker"):
        environment = services[service_name]["environment"]
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL"]
            == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL:?Set authenticated QA delivery base URL}"
        )
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_URL_TIMEOUT_SECONDS"]
            == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_URL_TIMEOUT_SECONDS:-10}"
        )
