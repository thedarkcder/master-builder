#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


DEPLOYMENT_PLANE_STATES = ("unconfigured", "provisioning", "active", "degraded", "paused", "failed")
INFRASTRUCTURE_PROVIDERS = ("aws", "hetzner")


class WireConfigError(RuntimeError):
    pass


class ApiRequestError(RuntimeError):
    pass


@dataclass(frozen=True)
class WireConfig:
    execute: bool
    api_base_url: str
    public_api_base_url: str
    tenant_id: str
    auth_header: str
    infrastructure_provider: str | None
    region: str | None
    base_domain: str | None
    platform_subdomain: str | None
    coolify_api_base_url: str | None
    coolify_project_uuid: str | None
    coolify_environment_name: str | None
    coolify_server_uuid: str | None
    coolify_destination_uuid: str | None
    deployment_state: str | None
    tenant_secret_values: dict[str, str]
    deployment_plane_secret_ref_updates: dict[str, str]
    webhook_token_value: str
    selected_project_ids: tuple[str, ...]
    webhook_token_source: str


class AdminApiClient:
    def __init__(self, *, base_url: str, auth_header: str) -> None:
        self._base_url = _normalize_base_url(base_url)
        self._auth_header = auth_header

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict[str, Any] | list[Any]:
        url = f"{self._base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": self._auth_header,
            "User-Agent": "master-builder-wire-internal-coolify",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        request = Request(url=url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                status_code = getattr(response, "status", response.getcode())
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise ApiRequestError(f"{method} {path} failed with HTTP {exc.code}: {error_body}") from exc
        except URLError as exc:
            raise ApiRequestError(f"{method} {path} failed: {exc.reason}") from exc

        if expected_statuses is not None and status_code not in expected_statuses:
            raise ApiRequestError(
                f"{method} {path} returned HTTP {status_code}, expected one of {sorted(expected_statuses)}"
            )
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ApiRequestError(f"{method} {path} returned invalid JSON: {raw[:200]}") from exc

    def get_tenant_deployment_plane(self, *, tenant_id: str) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path=f"/api/admin/tenants/{quote(tenant_id, safe='')}/deployment-plane",
            expected_statuses={200},
        )
        if not isinstance(response, dict):
            raise ApiRequestError("Expected deployment-plane response object")
        return response

    def update_tenant_deployment_plane(self, *, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request_json(
            method="PUT",
            path=f"/api/admin/tenants/{quote(tenant_id, safe='')}/deployment-plane",
            payload=payload,
            expected_statuses={200},
        )
        if not isinstance(response, dict):
            raise ApiRequestError("Expected deployment-plane update response object")
        return response

    def upsert_tenant_secret(self, *, tenant_id: str, secret_key: str, value: str) -> dict[str, Any]:
        response = self._request_json(
            method="PUT",
            path=f"/api/admin/tenants/{quote(tenant_id, safe='')}/secrets/{quote(secret_key, safe='')}",
            payload={"value": value},
            expected_statuses={200},
        )
        if not isinstance(response, dict):
            raise ApiRequestError("Expected tenant secret upsert response object")
        return response

    def list_projects(self, *, tenant_id: str) -> list[dict[str, Any]]:
        response = self._request_json(
            method="GET",
            path=f"/api/admin/tenants/{quote(tenant_id, safe='')}/projects",
            expected_statuses={200},
        )
        if not isinstance(response, list):
            raise ApiRequestError("Expected projects response array")
        return [item for item in response if isinstance(item, dict)]


def _normalize_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        raise WireConfigError("Base URL must be provided")
    if not normalized.startswith(("http://", "https://")):
        raise WireConfigError("Base URL must start with http:// or https://")
    return normalized


def _normalize_optional(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def tenant_secret_ref(*, tenant_id: str, secret_key: str) -> str:
    normalized_tenant_id = _normalize_required(tenant_id, field_name="tenant_id")
    normalized_secret_key = _normalize_required(secret_key, field_name="secret_key")
    if "/" in normalized_secret_key:
        raise WireConfigError("Tenant secret key must not include '/'")
    return f"tenant/{normalized_tenant_id}/{normalized_secret_key}"


def deployment_plane_secret_ref(*, tenant_id: str, secret_key_or_ref: str) -> str:
    normalized = _normalize_required(secret_key_or_ref, field_name="secret ref")
    if normalized.startswith(("platform/", "tenant/", "project/")):
        return normalized
    return tenant_secret_ref(tenant_id=tenant_id, secret_key=normalized)


def build_webhook_url(*, public_api_base_url: str, tenant_id: str, project_id: str, token: str) -> str:
    base = _normalize_base_url(public_api_base_url)
    return (
        f"{base}/deployments/coolify/webhook/"
        f"{quote(_normalize_required(tenant_id, field_name='tenant_id'), safe='')}/"
        f"{quote(_normalize_required(project_id, field_name='project_id'), safe='')}/"
        f"{quote(_normalize_required(token, field_name='webhook token'), safe='')}"
    )


def merge_deployment_plane_payload(
    *,
    existing_plane: dict[str, Any],
    deployment_state: str | None,
    infrastructure_provider: str | None,
    region: str | None,
    base_domain: str | None,
    platform_subdomain: str | None,
    coolify_api_base_url: str | None,
    coolify_project_uuid: str | None,
    coolify_environment_name: str | None,
    coolify_server_uuid: str | None,
    coolify_destination_uuid: str | None,
    secret_ref_updates: dict[str, str],
) -> dict[str, Any]:
    merged = dict(existing_plane or {})
    merged["provider"] = "internal_coolify"
    if deployment_state is not None:
        merged["state"] = deployment_state
    if infrastructure_provider is not None:
        merged["infrastructure_provider"] = infrastructure_provider
    if region is not None:
        merged["region"] = region
    if base_domain is not None:
        merged["base_domain"] = base_domain
    if platform_subdomain is not None:
        merged["platform_subdomain"] = platform_subdomain
    if coolify_api_base_url is not None:
        merged["api_base_url"] = coolify_api_base_url
    if coolify_project_uuid is not None:
        merged["coolify_project_uuid"] = coolify_project_uuid
    if coolify_environment_name is not None:
        merged["coolify_environment_name"] = coolify_environment_name
    if coolify_server_uuid is not None:
        merged["coolify_server_uuid"] = coolify_server_uuid
    if coolify_destination_uuid is not None:
        merged["coolify_destination_uuid"] = coolify_destination_uuid

    secret_refs = dict(merged.get("secret_refs") or {})
    secret_refs.update(secret_ref_updates)
    merged["secret_refs"] = secret_refs
    merged.setdefault("last_error", None)
    return merged


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Wire tenant-specific internal Coolify secrets + deployment plane + webhook URLs.",
    )
    parser.add_argument("--execute", action="store_true", help="Actually write secrets and deployment-plane configuration.")
    parser.add_argument("--api-base-url", required=True, help="Admin API base URL, for example https://orchestrator.example.com")
    parser.add_argument("--public-api-base-url", default="", help="Public API base URL used for webhook URLs. Defaults to --api-base-url.")
    parser.add_argument("--tenant-id", required=True, help="Target tenant identifier.")
    parser.add_argument("--project-id", action="append", default=[], help="Optional project ID filter. Repeat to target multiple projects.")

    parser.add_argument("--admin-bearer-token", default="", help="Admin bearer token. If omitted, basic auth is used.")
    parser.add_argument("--admin-username", default="", help="Admin username for basic auth when bearer token is omitted.")
    parser.add_argument("--admin-password", default="", help="Admin password for basic auth when bearer token is omitted.")

    parser.add_argument("--infrastructure-provider", choices=INFRASTRUCTURE_PROVIDERS, default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument("--base-domain", default=None)
    parser.add_argument("--platform-subdomain", default=None)
    parser.add_argument("--coolify-api-base-url", default=None, help="Coolify API base URL, for example https://coolify.example.com/api/v1")
    parser.add_argument("--coolify-project-uuid", default=None)
    parser.add_argument("--coolify-environment-name", default=None)
    parser.add_argument("--coolify-server-uuid", default=None)
    parser.add_argument("--coolify-destination-uuid", default=None)
    parser.add_argument("--deployment-state", choices=DEPLOYMENT_PLANE_STATES, default=None)

    parser.add_argument("--coolify-api-token-env", default="COOLIFY_API_TOKEN")
    parser.add_argument("--coolify-api-token-secret-key", default="COOLIFY_API_TOKEN")
    parser.add_argument("--coolify-webhook-token-env", default="COOLIFY_WEBHOOK_TOKEN")
    parser.add_argument("--coolify-webhook-token-secret-key", default="COOLIFY_WEBHOOK_TOKEN")
    parser.add_argument("--generate-webhook-token", action="store_true", help="Generate a random webhook token if env var is missing.")
    parser.add_argument("--webhook-token-bytes", type=int, default=32, help="Random bytes for generated webhook token.")

    parser.add_argument(
        "--tenant-secret-env",
        action="append",
        default=[],
        help="Additional tenant secret from env in KEY=ENV_VAR form (repeatable).",
    )
    parser.add_argument(
        "--plane-secret-ref",
        action="append",
        default=[],
        help="Additional deployment-plane secret ref mapping in REF_KEY=TENANT_SECRET_KEY_OR_REF form (repeatable).",
    )
    return parser.parse_args(argv)


def _normalize_required(value: str, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise WireConfigError(f"Missing required value for {field_name}")
    return normalized


def _parse_assignment(raw: str, *, flag_name: str) -> tuple[str, str]:
    normalized = str(raw or "").strip()
    if "=" not in normalized:
        raise WireConfigError(f"Invalid {flag_name} value '{raw}'. Expected KEY=VALUE.")
    left, right = normalized.split("=", 1)
    key = _normalize_required(left, field_name=f"{flag_name} key")
    value = _normalize_required(right, field_name=f"{flag_name} value")
    return key, value


def _load_env_value(*, env_name: str, environ: dict[str, str]) -> str | None:
    value = str(environ.get(env_name, "")).strip()
    return value or None


def _resolve_auth_header(*, args: argparse.Namespace, environ: dict[str, str]) -> str:
    bearer = _normalize_optional(args.admin_bearer_token) or _load_env_value(
        env_name="MB_ADMIN_BEARER_TOKEN",
        environ=environ,
    )
    if bearer:
        return f"Bearer {bearer}"

    username = _normalize_optional(args.admin_username) or _load_env_value(
        env_name="MB_ADMIN_USERNAME",
        environ=environ,
    )
    password = _normalize_optional(args.admin_password) or _load_env_value(
        env_name="MB_ADMIN_PASSWORD",
        environ=environ,
    )
    if not username or not password:
        raise WireConfigError(
            "Provide --admin-bearer-token or admin basic credentials "
            "(--admin-username/--admin-password or MB_ADMIN_USERNAME/MB_ADMIN_PASSWORD)."
        )
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def _add_tenant_secret_value(*, target: dict[str, str], key: str, value: str) -> None:
    existing = target.get(key)
    if existing is not None and existing != value:
        raise WireConfigError(f"Tenant secret key '{key}' was provided with conflicting values.")
    target[key] = value


def build_wire_config(*, args: argparse.Namespace, environ: dict[str, str] | None = None) -> WireConfig:
    env = os.environ if environ is None else environ
    tenant_id = _normalize_required(args.tenant_id, field_name="tenant_id")
    api_base_url = _normalize_base_url(args.api_base_url)
    public_api_base_url = _normalize_base_url(args.public_api_base_url or api_base_url)
    auth_header = _resolve_auth_header(args=args, environ=env)

    api_token_env = _normalize_required(args.coolify_api_token_env, field_name="coolify_api_token_env")
    api_token_value = _load_env_value(env_name=api_token_env, environ=env)
    if api_token_value is None:
        raise WireConfigError(f"Missing required environment variable: {api_token_env}")

    webhook_token_env = _normalize_required(args.coolify_webhook_token_env, field_name="coolify_webhook_token_env")
    webhook_token_value = _load_env_value(env_name=webhook_token_env, environ=env)
    webhook_token_source = f"env:{webhook_token_env}"
    if webhook_token_value is None:
        if args.generate_webhook_token:
            size = max(16, int(args.webhook_token_bytes or 32))
            webhook_token_value = secrets.token_urlsafe(size)
            webhook_token_source = "generated"
        else:
            raise WireConfigError(
                f"Missing required environment variable: {webhook_token_env} "
                "(or pass --generate-webhook-token)"
            )

    tenant_secret_values: dict[str, str] = {}
    for raw_mapping in args.tenant_secret_env:
        secret_key, env_name = _parse_assignment(raw_mapping, flag_name="--tenant-secret-env")
        env_value = _load_env_value(env_name=env_name, environ=env)
        if env_value is None:
            raise WireConfigError(f"Missing required environment variable: {env_name}")
        _add_tenant_secret_value(target=tenant_secret_values, key=secret_key, value=env_value)

    api_token_secret_key = _normalize_required(
        args.coolify_api_token_secret_key,
        field_name="coolify_api_token_secret_key",
    )
    webhook_token_secret_key = _normalize_required(
        args.coolify_webhook_token_secret_key,
        field_name="coolify_webhook_token_secret_key",
    )
    _add_tenant_secret_value(target=tenant_secret_values, key=api_token_secret_key, value=api_token_value)
    _add_tenant_secret_value(target=tenant_secret_values, key=webhook_token_secret_key, value=webhook_token_value)

    secret_ref_updates = {
        "coolify_api_token": tenant_secret_ref(tenant_id=tenant_id, secret_key=api_token_secret_key),
        "coolify_webhook_token": tenant_secret_ref(tenant_id=tenant_id, secret_key=webhook_token_secret_key),
    }
    for raw_mapping in args.plane_secret_ref:
        ref_key, secret_key_or_ref = _parse_assignment(raw_mapping, flag_name="--plane-secret-ref")
        secret_ref_updates[ref_key] = deployment_plane_secret_ref(
            tenant_id=tenant_id,
            secret_key_or_ref=secret_key_or_ref,
        )

    selected_project_ids = tuple(
        project_id
        for project_id in (_normalize_optional(item) for item in args.project_id)
        if project_id is not None
    )

    return WireConfig(
        execute=bool(args.execute),
        api_base_url=api_base_url,
        public_api_base_url=public_api_base_url,
        tenant_id=tenant_id,
        auth_header=auth_header,
        infrastructure_provider=_normalize_optional(args.infrastructure_provider),
        region=_normalize_optional(args.region),
        base_domain=_normalize_optional(args.base_domain),
        platform_subdomain=_normalize_optional(args.platform_subdomain),
        coolify_api_base_url=_normalize_optional(args.coolify_api_base_url),
        coolify_project_uuid=_normalize_optional(args.coolify_project_uuid),
        coolify_environment_name=_normalize_optional(args.coolify_environment_name),
        coolify_server_uuid=_normalize_optional(args.coolify_server_uuid),
        coolify_destination_uuid=_normalize_optional(args.coolify_destination_uuid),
        deployment_state=_normalize_optional(args.deployment_state),
        tenant_secret_values=tenant_secret_values,
        deployment_plane_secret_ref_updates=secret_ref_updates,
        webhook_token_value=webhook_token_value,
        selected_project_ids=selected_project_ids,
        webhook_token_source=webhook_token_source,
    )


def _selected_projects(
    *,
    projects: list[dict[str, Any]],
    selected_project_ids: tuple[str, ...],
) -> list[dict[str, Any]]:
    if not selected_project_ids:
        return projects
    selected = [project for project in projects if str(project.get("project_id") or "").strip() in selected_project_ids]
    unknown = sorted(
        project_id
        for project_id in selected_project_ids
        if all(str(project.get("project_id") or "").strip() != project_id for project in projects)
    )
    if unknown:
        raise WireConfigError("Unknown project ids for tenant: " + ", ".join(unknown))
    return selected


def run(config: WireConfig) -> tuple[int, dict[str, Any]]:
    client = AdminApiClient(base_url=config.api_base_url, auth_header=config.auth_header)
    existing_plane = client.get_tenant_deployment_plane(tenant_id=config.tenant_id)
    merged_plane = merge_deployment_plane_payload(
        existing_plane=existing_plane,
        deployment_state=config.deployment_state,
        infrastructure_provider=config.infrastructure_provider,
        region=config.region,
        base_domain=config.base_domain,
        platform_subdomain=config.platform_subdomain,
        coolify_api_base_url=config.coolify_api_base_url,
        coolify_project_uuid=config.coolify_project_uuid,
        coolify_environment_name=config.coolify_environment_name,
        coolify_server_uuid=config.coolify_server_uuid,
        coolify_destination_uuid=config.coolify_destination_uuid,
        secret_ref_updates=config.deployment_plane_secret_ref_updates,
    )

    upserted_secret_keys: list[str] = []
    if config.execute:
        for secret_key, value in sorted(config.tenant_secret_values.items()):
            client.upsert_tenant_secret(
                tenant_id=config.tenant_id,
                secret_key=secret_key,
                value=value,
            )
            upserted_secret_keys.append(secret_key)
        updated_plane = client.update_tenant_deployment_plane(
            tenant_id=config.tenant_id,
            payload=merged_plane,
        )
    else:
        updated_plane = merged_plane

    projects = client.list_projects(tenant_id=config.tenant_id)
    selected_projects = _selected_projects(projects=projects, selected_project_ids=config.selected_project_ids)
    webhook_rows = [
        {
            "project_id": project_id,
            "webhook_url": build_webhook_url(
                public_api_base_url=config.public_api_base_url,
                tenant_id=config.tenant_id,
                project_id=project_id,
                token=config.webhook_token_value,
            ),
        }
        for project_id in sorted(
            str(project.get("project_id") or "").strip()
            for project in selected_projects
            if str(project.get("project_id") or "").strip()
        )
    ]

    summary = {
        "ok": True,
        "execute": config.execute,
        "tenant_id": config.tenant_id,
        "webhook_token_source": config.webhook_token_source,
        "upserted_secret_keys": upserted_secret_keys,
        "deployment_plane": {
            "provider": updated_plane.get("provider"),
            "infrastructure_provider": updated_plane.get("infrastructure_provider"),
            "region": updated_plane.get("region"),
            "base_domain": updated_plane.get("base_domain"),
            "platform_subdomain": updated_plane.get("platform_subdomain"),
            "api_base_url": updated_plane.get("api_base_url"),
            "coolify_project_uuid": updated_plane.get("coolify_project_uuid"),
            "coolify_environment_name": updated_plane.get("coolify_environment_name"),
            "coolify_server_uuid": updated_plane.get("coolify_server_uuid"),
            "coolify_destination_uuid": updated_plane.get("coolify_destination_uuid"),
            "state": updated_plane.get("state"),
            "secret_refs": dict(updated_plane.get("secret_refs") or {}),
        },
        "project_webhooks": webhook_rows,
    }
    return 0, summary


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        config = build_wire_config(args=args)
    except WireConfigError as exc:
        print(f"[error] {exc}")
        return 2

    if not config.execute:
        print("[plan] Dry-run mode. Add --execute to persist secrets and deployment-plane config.")
    try:
        exit_code, summary = run(config)
    except (WireConfigError, ApiRequestError) as exc:
        print(f"[error] {exc}")
        return 1

    for item in summary["project_webhooks"]:
        print(f"[webhook] project={item['project_id']} url={item['webhook_url']}")
    print(json.dumps(summary))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
