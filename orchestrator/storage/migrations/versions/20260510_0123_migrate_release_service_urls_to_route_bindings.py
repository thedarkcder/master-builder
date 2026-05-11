"""migrate deployment release service urls to route bindings

Revision ID: 20260510_0123
Revises: 20260509_0122
Create Date: 2026-05-10 20:30:00.000000
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

from alembic import op
from sqlalchemy import inspect, text


revision = "20260510_0123"
down_revision = "20260509_0122"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _loads_json(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    if isinstance(value, str):
        decoded = json.loads(value) if value.strip() else {}
        if isinstance(decoded, dict):
            return dict(decoded)
    return {}


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _route_binding_from_service_url(value: object, *, release_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Deployment release {release_id} has invalid provider_context.service_urls item")
    service_key = _normalize_optional_string(value.get("service_key"))
    service_name = _normalize_optional_string(value.get("service_name")) or service_key
    url = _normalize_optional_string(value.get("url"))
    if service_key is None or service_name is None or url is None:
        raise ValueError(f"Deployment release {release_id} has incomplete provider_context.service_urls item")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"Deployment release {release_id} service URL for {service_key} is not absolute http(s)")
    binding: dict[str, Any] = {
        "service_key": service_key,
        "service_name": service_name,
        "service_kind": "api" if value.get("service_kind") == "api" else "website",
        "scheme": parsed.scheme,
        "host": parsed.hostname.lower(),
        "path": parsed.path or "",
        "url_kind": "custom" if value.get("url_kind") == "custom" else "generated",
        "status": value.get("status") if value.get("status") in {"pending", "active", "failed"} else "pending",
    }
    if parsed.port is not None:
        binding["proxy_port"] = parsed.port
    if value.get("port") is not None:
        binding["port"] = value["port"]
    if value.get("internal_url") is not None:
        binding["internal_url"] = value["internal_url"]
    if value.get("domain_key") is not None:
        binding["domain_key"] = value["domain_key"]
    return binding


def _rewrite_provider_context(value: object, *, release_id: str) -> tuple[dict[str, Any], bool]:
    provider_context = _loads_json(value)
    service_urls = provider_context.get("service_urls")
    if service_urls is None:
        return provider_context, False
    if not isinstance(service_urls, list):
        raise ValueError(f"Deployment release {release_id} provider_context.service_urls must be a list")
    route_bindings = provider_context.get("route_bindings")
    if route_bindings is not None and not isinstance(route_bindings, list):
        raise ValueError(f"Deployment release {release_id} provider_context.route_bindings must be a list")
    existing_bindings = list(route_bindings or [])
    existing_service_keys = {
        str(item.get("service_key") or "").strip()
        for item in existing_bindings
        if isinstance(item, dict)
    }
    for service_url in service_urls:
        binding = _route_binding_from_service_url(service_url, release_id=release_id)
        if binding["service_key"] in existing_service_keys:
            continue
        existing_bindings.append(binding)
        existing_service_keys.add(binding["service_key"])
    provider_context["route_bindings"] = existing_bindings
    provider_context.pop("service_urls", None)
    return provider_context, True


def upgrade() -> None:
    if not _table_exists("project_deployment_releases"):
        return
    bind = op.get_bind()
    rows = bind.execute(
        text("SELECT release_id, provider_context FROM project_deployment_releases")
    ).mappings().all()
    for row in rows:
        release_id = str(row.get("release_id") or "").strip()
        updated, changed = _rewrite_provider_context(row.get("provider_context"), release_id=release_id)
        if not changed:
            continue
        bind.execute(
            text(
                "UPDATE project_deployment_releases "
                "SET provider_context = :provider_context "
                "WHERE release_id = :release_id"
            ),
            {
                "provider_context": json.dumps(updated, sort_keys=True),
                "release_id": release_id,
            },
        )


def downgrade() -> None:
    return None
