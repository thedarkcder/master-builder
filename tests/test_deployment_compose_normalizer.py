from __future__ import annotations

import yaml

from orchestrator.core.deployment_setup.compose_normalizer import normalize_compose_for_coolify


def test_normalize_compose_for_coolify_removes_host_port_bindings() -> None:
    result = normalize_compose_for_coolify(
        """
services:
  api:
    image: example/api
    ports:
      - "127.0.0.1:9000:3000"
      - "9001:3001/tcp"
      - target: 8080
        published: 9180
  worker:
    image: example/worker
    expose:
      - "7000"
"""
    )

    parsed = yaml.safe_load(result.compose_raw)

    assert "ports" not in parsed["services"]["api"]
    assert parsed["services"]["api"]["expose"] == ["3000", "3001", "8080"]
    assert parsed["services"]["worker"]["expose"] == ["7000"]
    assert result.exposed_ports_by_service == {
        "api": ["3000", "3001", "8080"],
        "worker": ["7000"],
    }


def test_normalize_compose_for_coolify_requires_services() -> None:
    try:
        normalize_compose_for_coolify("name: empty\n")
    except ValueError as exc:
        assert "must define services" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected invalid compose content to fail")


def test_normalize_compose_for_coolify_binds_temporal_to_all_interfaces() -> None:
    result = normalize_compose_for_coolify(
        """
services:
  temporal:
    image: temporalio/auto-setup:1.25.2
    environment:
      DB: postgres12
"""
    )

    parsed = yaml.safe_load(result.compose_raw)

    assert parsed["services"]["temporal"]["environment"]["DB"] == "postgres12"
    assert parsed["services"]["temporal"]["environment"]["BIND_ON_IP"] == "0.0.0.0"


def test_normalize_compose_for_coolify_does_not_create_proxy_route_labels() -> None:
    normalized = normalize_compose_for_coolify(
        """
services:
  web:
    image: example/web
    ports:
      - "3000:8080"
"""
    )

    assert "traefik." not in normalized.compose_raw
    assert "coolify:" not in normalized.compose_raw
