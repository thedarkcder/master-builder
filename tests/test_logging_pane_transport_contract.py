from __future__ import annotations

from pathlib import Path

import pytest

from orchestrator.core.observability.logging_pane import emit_logging_pane_event


def test_operation_scoped_logging_pane_event_requires_attempt_id() -> None:
    with pytest.raises(ValueError, match="attempt_id"):
        emit_logging_pane_event(
            session=object(),
            tenant_id="tenant-a",
            project_id="project-a",
            workflow_id="workflow-a",
            operation_id="operation-a",
            attempt_id=None,
            run_id="run-a",
            issue_key="TP-1",
            agent_id="worker-1",
            invocation_id="inv-1",
            channel="worker",
            command="workflow.dev",
            working_dir="/tmp/repo",
            stage="dev",
            attempt=1,
            stream="stdout",
            message="line",
        )


def test_old_log_transport_modules_are_removed() -> None:
    root = Path(__file__).resolve().parents[1]
    assert not (root / "orchestrator" / "core" / "run_logs.py").exists()
    assert not (root / "orchestrator" / "core" / "log_event_bus.py").exists()
    assert not (root / "orchestrator" / "api" / "admin" / "live_telemetry_service.py").exists()
    assert not (root / "orchestrator" / "api" / "admin" / "codex_logs_service.py").exists()
    assert not (root / "orchestrator" / "api" / "admin" / "run_event_stream_service.py").exists()
    assert not (root / "orchestrator" / "storage" / "run_event_stream.py").exists()
    assert not (root / "ops" / "observability" / "loki-config.yaml").exists()


def test_stack_config_uses_clickhouse_not_redis_or_loki() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    prod_compose = (root / "deploy" / "hetzner" / "docker-compose.prod.yml").read_text(encoding="utf-8")
    datasources = (root / "ops" / "observability" / "grafana" / "provisioning" / "datasources" / "datasources.yaml").read_text(
        encoding="utf-8"
    )
    assert "clickhouse" in compose
    assert "redis" not in compose.lower()
    assert "loki" not in compose.lower()
    assert "clickhouse" in prod_compose
    assert "redis" not in prod_compose.lower()
    assert "loki" not in prod_compose.lower()
    assert "grafana-clickhouse-datasource" in datasources
    assert "loki" not in datasources.lower()


def test_deployment_docs_do_not_require_redis_or_loki() -> None:
    root = Path(__file__).resolve().parents[1]
    checked_paths = [
        root / "deploy" / "common" / "service-profile.md",
        root / "deploy" / "common" / "env.required.md",
        root / "deploy" / "hetzner" / ".env.example",
        root / "deploy" / "hetzner" / "bootstrap.sh",
        root / "deploy" / "hetzner" / "coolify.yaml",
        root / "deploy" / "hetzner" / "README.md",
        root / "deploy" / "aws" / "README.md",
        root / "deploy" / "gcp" / "README.md",
        root / "docs" / "deployment-packaging.md",
    ]
    for path in checked_paths:
        content = path.read_text(encoding="utf-8").lower()
        assert "redis" not in content, path
        assert "loki" not in content, path


def test_grafana_provisions_clickhouse_prometheus_tempo_and_dashboards() -> None:
    root = Path(__file__).resolve().parents[1]
    datasources = (root / "ops" / "observability" / "grafana" / "provisioning" / "datasources" / "datasources.yaml").read_text(
        encoding="utf-8"
    )
    dashboard_provider = (
        root / "ops" / "observability" / "grafana" / "provisioning" / "dashboards" / "dashboards.yaml"
    ).read_text(encoding="utf-8")
    dashboard = (
        root
        / "ops"
        / "observability"
        / "grafana"
        / "provisioning"
        / "dashboards"
        / "master-builder"
        / "observability-overview.json"
    ).read_text(encoding="utf-8")

    assert "grafana-clickhouse-datasource" in datasources
    assert "uid: prometheus" in datasources
    assert "uid: tempo" in datasources
    assert "loki" not in datasources.lower()
    assert "master-builder" in dashboard_provider
    assert "Workflow Operation Failures By Type" in dashboard
    assert "Log Ingestion Rate" in dashboard
    assert "Audit Write" in dashboard
    assert "ClickHouse Log Lag" in dashboard
    assert "Workflow Attempt Latency" in dashboard
