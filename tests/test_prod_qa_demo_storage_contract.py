from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_prod_env_example_declares_qa_demo_artifact_settings() -> None:
    text = (ROOT / "deploy" / "hetzner" / ".env.example").read_text()

    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT=" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_ACCESS_KEY=" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY=" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET=" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL=" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_SECURE=true" in text
    assert "ORCHESTRATOR_QA_DEMO_RECORDER_PROCESS_TIMEOUT_SECONDS=900" in text
    assert "ORCHESTRATOR_QA_DEMO_RELEASE_HEALTH_TIMEOUT_SECONDS=10" in text
    assert "ORCHESTRATOR_QA_DEMO_ARTIFACT_URL_TIMEOUT_SECONDS=10" in text
    assert "ORCHESTRATOR_QA_DEMO_IOS_RECORDER_COMMAND=" in text
    assert "ORCHESTRATOR_QA_DEMO_ANDROID_RECORDER_COMMAND=" in text
    assert "ORCHESTRATOR_QA_DEMO_ANDROID_WORKER_PLATFORM=" in text


def test_prod_compose_exposes_qa_demo_storage_to_run_and_review_workers() -> None:
    compose_path = ROOT / "deploy" / "hetzner" / "docker-compose.prod.yml"
    payload = yaml.safe_load(compose_path.read_text())
    services = payload["services"]

    run_environment = services["run-worker"]["environment"]
    assert run_environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT"] == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_ENDPOINT:-}"
    assert run_environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_ACCESS_KEY"] == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_ACCESS_KEY:-}"
    assert run_environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY"] == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY:-}"
    assert run_environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET"] == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET:-}"
    assert (
        run_environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL"]
        == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL:-}"
    )
    assert run_environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_SECURE"] == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_SECURE:-true}"
    assert (
        run_environment["ORCHESTRATOR_QA_DEMO_RECORDER_PROCESS_TIMEOUT_SECONDS"]
        == "${ORCHESTRATOR_QA_DEMO_RECORDER_PROCESS_TIMEOUT_SECONDS:-900}"
    )
    assert (
        run_environment["ORCHESTRATOR_QA_DEMO_RELEASE_HEALTH_TIMEOUT_SECONDS"]
        == "${ORCHESTRATOR_QA_DEMO_RELEASE_HEALTH_TIMEOUT_SECONDS:-10}"
    )
    assert (
        run_environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_URL_TIMEOUT_SECONDS"]
        == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_URL_TIMEOUT_SECONDS:-10}"
    )
    assert run_environment["ORCHESTRATOR_QA_DEMO_IOS_RECORDER_COMMAND"] == "${ORCHESTRATOR_QA_DEMO_IOS_RECORDER_COMMAND:-}"
    assert (
        run_environment["ORCHESTRATOR_QA_DEMO_ANDROID_RECORDER_COMMAND"]
        == "${ORCHESTRATOR_QA_DEMO_ANDROID_RECORDER_COMMAND:-}"
    )
    assert (
        run_environment["ORCHESTRATOR_QA_DEMO_ANDROID_WORKER_PLATFORM"]
        == "${ORCHESTRATOR_QA_DEMO_ANDROID_WORKER_PLATFORM:-}"
    )

    for service_name in ("api", "webhook-worker"):
        environment = services[service_name]["environment"]
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL"]
            == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL:-}"
        )
        assert (
            environment["ORCHESTRATOR_QA_DEMO_ARTIFACT_URL_TIMEOUT_SECONDS"]
            == "${ORCHESTRATOR_QA_DEMO_ARTIFACT_URL_TIMEOUT_SECONDS:-10}"
        )
