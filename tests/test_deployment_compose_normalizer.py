from __future__ import annotations

import yaml

from orchestrator.core.deployment_setup.compose_normalizer import (
    normalize_compose_for_coolify,
    normalize_generated_compose_for_coolify,
)


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


def test_normalize_generated_compose_for_coolify_uses_legacy_peer_deps_for_node_builds() -> (
    None
):
    result = normalize_generated_compose_for_coolify(
        """
services:
  admin-website:
    build:
      context: ./web/www-admin-react
      dockerfile_inline: |
        FROM node:22-alpine AS build
        WORKDIR /app
        COPY package*.json ./
        RUN npm ci
        COPY . .
        RUN npx vite build
    ports:
      - "8080:80"
"""
    )

    parsed = yaml.safe_load(result.compose_raw)
    dockerfile_inline = parsed["services"]["admin-website"]["build"][
        "dockerfile_inline"
    ]

    assert "RUN npm ci --legacy-peer-deps" in dockerfile_inline
    assert "\nRUN npm ci\n" not in dockerfile_inline
    assert parsed["services"]["admin-website"]["expose"] == ["80"]


def test_normalize_generated_compose_for_coolify_extends_spring_health_startup_budget() -> (
    None
):
    result = normalize_generated_compose_for_coolify(
        """
services:
  customer-api:
    image: example/customer-api
    healthcheck:
      test:
        - CMD
        - curl
        - -f
        - http://localhost:8080/actuator/health
      interval: 30s
      timeout: 5s
      retries: 5
"""
    )

    parsed = yaml.safe_load(result.compose_raw)
    healthcheck = parsed["services"]["customer-api"]["healthcheck"]

    assert healthcheck["start_period"] == "600s"
    assert healthcheck["interval"] == "15s"
    assert healthcheck["timeout"] == "5s"
    assert healthcheck["retries"] == 40


def test_normalize_generated_compose_for_coolify_uses_internal_resource_aliases() -> (
    None
):
    result = normalize_generated_compose_for_coolify(
        """
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_DB: example-tenant
      POSTGRES_USER: postgres
      POSTGRES_PASSWORD: postgres
  activemq:
    image: apache/activemq-classic:6.1.7
  kafka:
    image: apache/kafka:4.2.0
  elasticsearch:
    image: bitnamilegacy/elasticsearch:8
  mailpit:
    image: axllent/mailpit:v1.28.2
  minio:
    image: minio/minio:latest
  tenants-api:
    image: example/tenants-api
    networks:
      - coolify
    environment:
      POSTGRES_DB_URL: jdbc:postgresql://postgres:5432/example-tenant
      SPRING_DATASOURCE_URL: jdbc:postgresql://postgres:5432/example-tenant
      SPRING_ACTIVEMQ_BROKER_URL: tcp://activemq:61616?wireFormat.maxInactivityDuration=0
      SPRING_KAFKA_BOOTSTRAP_SERVERS: kafka:9092
      SPRING_ELASTICSEARCH_URIS: http://elasticsearch:9200
      SPRING_ELASTICSEARCH_HOST: elasticsearch
      SPRING_MAIL_HOST: mailpit
      AWS_S3_PUBLIC_HTTP_URL: http://minio:9000/public-bucket/
"""
    )

    parsed = yaml.safe_load(result.compose_raw)
    services = parsed["services"]

    assert services["postgres"]["networks"]["default"]["aliases"] == ["mb-postgres"]
    assert services["activemq"]["networks"]["default"]["aliases"] == ["mb-activemq"]
    assert services["kafka"]["networks"]["default"]["aliases"] == ["mb-kafka"]
    assert services["elasticsearch"]["networks"]["default"]["aliases"] == [
        "mb-elasticsearch"
    ]
    assert services["mailpit"]["networks"]["default"]["aliases"] == ["mb-mailpit"]
    assert services["minio"]["networks"]["default"]["aliases"] == ["mb-minio"]

    environment = services["tenants-api"]["environment"]
    assert services["tenants-api"]["networks"] == {"coolify": {}, "default": {}}
    assert (
        environment["POSTGRES_DB_URL"]
        == "jdbc:postgresql://mb-postgres:5432/example-tenant"
    )
    assert (
        environment["SPRING_DATASOURCE_URL"]
        == "jdbc:postgresql://mb-postgres:5432/example-tenant"
    )
    assert (
        environment["SPRING_ACTIVEMQ_BROKER_URL"]
        == "tcp://mb-activemq:61616?wireFormat.maxInactivityDuration=0"
    )
    assert environment["SPRING_KAFKA_BOOTSTRAP_SERVERS"] == "mb-kafka:9092"
    assert environment["SPRING_ELASTICSEARCH_URIS"] == "http://mb-elasticsearch:9200"
    assert environment["SPRING_ELASTICSEARCH_HOST"] == "mb-elasticsearch"
    assert environment["SPRING_MAIL_HOST"] == "mb-mailpit"
    assert (
        environment["AWS_S3_PUBLIC_HTTP_URL"] == "http://mb-minio:9000/public-bucket/"
    )


def test_normalize_generated_compose_for_coolify_strips_planner_proxy_labels() -> None:
    result = normalize_generated_compose_for_coolify(
        """
services:
  admin-website:
    image: example/admin
    labels:
      traefik.enable: "true"
      traefik.http.routers.mb-admin.rule: Host(`admin.main.example.localhost`)
      caddy_0: http://admin.main.example.localhost
      com.example.retained: "true"
  app-website:
    image: example/app
    labels:
      - traefik.enable=true
      - traefik.http.routers.mb-app.rule=Host(`app.main.example.localhost`)
      - caddy_0=http://app.main.example.localhost
      - com.example.visible=true
"""
    )

    parsed = yaml.safe_load(result.compose_raw)
    admin_labels = parsed["services"]["admin-website"]["labels"]
    app_labels = parsed["services"]["app-website"]["labels"]

    assert admin_labels == {"com.example.retained": "true"}
    assert app_labels == ["com.example.visible=true"]
