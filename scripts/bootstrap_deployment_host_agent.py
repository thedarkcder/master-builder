#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class BootstrapConfigError(RuntimeError):
    pass


class BootstrapApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class DeploymentHostBootstrapConfig:
    api_base_url: str
    auth_header: str
    host_label: str
    infrastructure_provider: str | None
    region: str | None
    capabilities: tuple[str, ...]
    bootstrap_token_path: Path
    access_token_path: Path
    tenant_ids: tuple[str, ...]
    assign_configured_tenants: bool


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
        auth_header: str | None = None,
    ) -> dict[str, Any] | list[Any]:
        url = f"{self._base_url}{path}"
        headers = {
            "Accept": "application/json",
            "User-Agent": "master-builder-deployment-host-bootstrap",
        }
        effective_auth_header = _normalize_optional_string(auth_header) or self._auth_header
        if effective_auth_header is not None:
            headers["Authorization"] = effective_auth_header
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
            raise BootstrapApiError(f"{method} {path} failed with HTTP {exc.code}: {error_body}") from exc
        except URLError as exc:
            raise BootstrapApiError(f"{method} {path} failed: {exc.reason}") from exc

        if expected_statuses is not None and status_code not in expected_statuses:
            raise BootstrapApiError(
                f"{method} {path} returned HTTP {status_code}, expected one of {sorted(expected_statuses)}",
            )
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BootstrapApiError(f"{method} {path} returned invalid JSON: {raw[:200]}") from exc

    def list_deployment_hosts(self) -> list[dict[str, Any]]:
        response = self._request_json(
            method="GET",
            path="/api/admin/deployment-hosts",
            expected_statuses={200},
        )
        if not isinstance(response, list):
            raise BootstrapApiError("Expected deployment hosts response array")
        return [item for item in response if isinstance(item, dict)]

    def create_deployment_host(
        self,
        *,
        label: str,
        infrastructure_provider: str | None,
        region: str | None,
        capabilities: tuple[str, ...],
    ) -> dict[str, Any]:
        response = self._request_json(
            method="POST",
            path="/api/admin/deployment-hosts",
            payload={
                "label": label,
                "provider": "internal_coolify",
                "infrastructure_provider": infrastructure_provider,
                "region": region,
                "capabilities": list(capabilities),
            },
            expected_statuses={201},
        )
        if not isinstance(response, dict):
            raise BootstrapApiError("Expected deployment host creation response object")
        return response

    def list_tenants(self) -> list[dict[str, Any]]:
        response = self._request_json(
            method="GET",
            path="/api/admin/tenants",
            expected_statuses={200},
        )
        if not isinstance(response, list):
            raise BootstrapApiError("Expected tenants response array")
        return [item for item in response if isinstance(item, dict)]

    def get_tenant_deployment_plane(self, *, tenant_id: str) -> dict[str, Any]:
        response = self._request_json(
            method="GET",
            path=f"/api/admin/tenants/{tenant_id}/deployment-plane",
            expected_statuses={200},
        )
        if not isinstance(response, dict):
            raise BootstrapApiError("Expected tenant deployment plane response object")
        return response

    def update_tenant_deployment_plane(self, *, tenant_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._request_json(
            method="PUT",
            path=f"/api/admin/tenants/{tenant_id}/deployment-plane",
            payload=payload,
            expected_statuses={200},
        )
        if not isinstance(response, dict):
            raise BootstrapApiError("Expected tenant deployment plane update response object")
        return response

    def register_deployment_host(
        self,
        *,
        bootstrap_token: str,
        agent_version: str,
        advertised_capabilities: tuple[str, ...],
    ) -> dict[str, Any]:
        response = self._request_json(
            method="POST",
            path="/api/internal/deployment-hosts/register",
            payload={
                "bootstrap_token": bootstrap_token,
                "agent_version": agent_version,
                "advertised_capabilities": list(advertised_capabilities),
            },
            expected_statuses={200},
            auth_header=None,
        )
        if not isinstance(response, dict):
            raise BootstrapApiError("Expected deployment host registration response object")
        return response

    def heartbeat_deployment_host(
        self,
        *,
        access_token: str,
        agent_version: str,
        advertised_capabilities: tuple[str, ...],
        state: str = "active",
    ) -> dict[str, Any]:
        response = self._request_json(
            method="POST",
            path="/api/internal/deployment-hosts/heartbeat",
            payload={
                "agent_version": agent_version,
                "advertised_capabilities": list(advertised_capabilities),
                "state": state,
            },
            expected_statuses={200},
            auth_header=f"Bearer {access_token}",
        )
        if not isinstance(response, dict):
            raise BootstrapApiError("Expected deployment host heartbeat response object")
        return response


def _normalize_base_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized:
        raise BootstrapConfigError("api base url is required")
    if not normalized.startswith(("http://", "https://")):
        raise BootstrapConfigError("api base url must start with http:// or https://")
    return normalized


def _normalize_optional_string(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _normalize_required_string(value: object, *, field_name: str) -> str:
    normalized = _normalize_optional_string(value)
    if normalized is None:
        raise BootstrapConfigError(f"{field_name} is required")
    return normalized


def _normalize_capabilities(raw_value: object) -> tuple[str, ...]:
    if isinstance(raw_value, str):
        values = raw_value.replace("\n", ",").split(",")
    elif isinstance(raw_value, (list, tuple, set)):
        values = list(raw_value)
    else:
        values = []
    normalized: list[str] = []
    for item in values:
        capability = str(item or "").strip().lower()
        if capability and capability not in normalized:
            normalized.append(capability)
    return tuple(normalized)


def _resolve_auth_header(*, args: argparse.Namespace, environ: dict[str, str]) -> str:
    bearer = _normalize_optional_string(args.admin_bearer_token) or _normalize_optional_string(
        environ.get("MB_ADMIN_BEARER_TOKEN"),
    )
    if bearer is not None:
        return f"Bearer {bearer}"

    username = _normalize_optional_string(args.admin_username) or _normalize_optional_string(environ.get("MB_ADMIN_USERNAME"))
    password = _normalize_optional_string(args.admin_password) or _normalize_optional_string(environ.get("MB_ADMIN_PASSWORD"))
    if username is None or password is None:
        raise BootstrapConfigError(
            "Provide --admin-bearer-token or admin basic credentials "
            "(--admin-username/--admin-password or MB_ADMIN_USERNAME/MB_ADMIN_PASSWORD).",
        )
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return f"Basic {encoded}"


def _default_host_label() -> str:
    return f"managed-{socket.gethostname()}"


def _write_secret_file(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _has_access_token(path: Path) -> bool:
    try:
        return bool(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return False


def _existing_bootstrap_token(path: Path) -> str | None:
    try:
        return _normalize_optional_string(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def _clear_secret_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _plane_is_configured_for_internal_coolify(plane: dict[str, Any]) -> bool:
    if _normalize_optional_string(plane.get("provider")) != "internal_coolify":
        return False
    if _normalize_optional_string(plane.get("managed_host_id")) is not None:
        return False
    if _normalize_optional_string(plane.get("api_base_url")) is not None:
        return True
    if _normalize_optional_string(plane.get("coolify_project_uuid")) is not None:
        return True
    if _normalize_optional_string(plane.get("coolify_server_uuid")) is not None:
        return True
    if _normalize_optional_string(plane.get("coolify_destination_uuid")) is not None:
        return True
    if dict(plane.get("secret_refs") or {}):
        return True
    return _normalize_optional_string(plane.get("state")) not in {None, "unconfigured"}


def _resolved_tenant_ids(
    *,
    client: AdminApiClient,
    tenant_ids: tuple[str, ...],
    assign_configured_tenants: bool,
) -> tuple[str, ...]:
    if tenant_ids:
        return tenant_ids
    if not assign_configured_tenants:
        return ()
    tenants = client.list_tenants()
    return tuple(
        tenant_id
        for tenant in tenants
        if (tenant_id := _normalize_optional_string(tenant.get("tenant_id"))) is not None
    )


def _assign_host_to_tenants(
    *,
    client: AdminApiClient,
    host_id: str,
    tenant_ids: tuple[str, ...],
    assign_configured_tenants: bool,
) -> list[str]:
    updated: list[str] = []
    for tenant_id in _resolved_tenant_ids(
        client=client,
        tenant_ids=tenant_ids,
        assign_configured_tenants=assign_configured_tenants,
    ):
        plane = client.get_tenant_deployment_plane(tenant_id=tenant_id)
        if tenant_ids:
            should_update = True
        else:
            should_update = _plane_is_configured_for_internal_coolify(plane)
        if not should_update:
            continue
        if _normalize_optional_string(plane.get("managed_host_id")) == host_id:
            continue
        plane["managed_host_id"] = host_id
        client.update_tenant_deployment_plane(tenant_id=tenant_id, payload=plane)
        updated.append(tenant_id)
    return updated


def ensure_deployment_host_bootstrap(config: DeploymentHostBootstrapConfig) -> dict[str, object]:
    client = AdminApiClient(base_url=config.api_base_url, auth_header=config.auth_header)
    if _has_access_token(config.access_token_path):
        access_token = _normalize_optional_string(config.access_token_path.read_text(encoding="utf-8"))
        if access_token is not None:
            try:
                heartbeat = client.heartbeat_deployment_host(
                    access_token=access_token,
                    agent_version="bootstrap",
                    advertised_capabilities=config.capabilities,
                )
                host_id = _normalize_required_string(heartbeat.get("host_id"), field_name="host_id")
                assigned_tenants = _assign_host_to_tenants(
                    client=client,
                    host_id=host_id,
                    tenant_ids=config.tenant_ids,
                    assign_configured_tenants=config.assign_configured_tenants,
                )
                return {
                    "ok": True,
                    "action": "noop",
                    "reason": "access_token_valid",
                    "host_id": host_id,
                    "host_label": config.host_label,
                    "assigned_tenants": assigned_tenants,
                }
            except BootstrapApiError:
                _clear_secret_file(config.access_token_path)

    existing_bootstrap_token = _existing_bootstrap_token(config.bootstrap_token_path)
    if existing_bootstrap_token is not None:
        try:
            registration = client.register_deployment_host(
                bootstrap_token=existing_bootstrap_token,
                agent_version="bootstrap",
                advertised_capabilities=config.capabilities,
            )
            access_token = _normalize_required_string(registration.get("access_token"), field_name="access_token")
            host = dict(registration.get("host") or {})
            host_id = _normalize_required_string(host.get("host_id"), field_name="host_id")
            _write_secret_file(config.access_token_path, access_token)
            assigned_tenants = _assign_host_to_tenants(
                client=client,
                host_id=host_id,
                tenant_ids=config.tenant_ids,
                assign_configured_tenants=config.assign_configured_tenants,
            )
            return {
                "ok": True,
                "action": "registered",
                "reason": "bootstrap_token_valid",
                "host_id": host_id,
                "host_label": config.host_label,
                "bootstrap_token_path": str(config.bootstrap_token_path),
                "assigned_tenants": assigned_tenants,
            }
        except BootstrapApiError:
            _clear_secret_file(config.bootstrap_token_path)
            _clear_secret_file(config.access_token_path)

    created = client.create_deployment_host(
        label=config.host_label,
        infrastructure_provider=config.infrastructure_provider,
        region=config.region,
        capabilities=config.capabilities,
    )
    host = dict(created.get("host") or {})
    bootstrap_token = _normalize_required_string(created.get("bootstrap_token"), field_name="bootstrap_token")
    _write_secret_file(config.bootstrap_token_path, bootstrap_token)
    registration = client.register_deployment_host(
        bootstrap_token=bootstrap_token,
        agent_version="bootstrap",
        advertised_capabilities=config.capabilities,
    )
    access_token = _normalize_required_string(registration.get("access_token"), field_name="access_token")
    _write_secret_file(config.access_token_path, access_token)
    host_id = _normalize_required_string(host.get("host_id"), field_name="host_id")
    assigned_tenants = _assign_host_to_tenants(
        client=client,
        host_id=host_id,
        tenant_ids=config.tenant_ids,
        assign_configured_tenants=config.assign_configured_tenants,
    )
    return {
        "ok": True,
        "action": "created",
        "host_id": host_id,
        "host_label": config.host_label,
        "bootstrap_token_path": str(config.bootstrap_token_path),
        "assigned_tenants": assigned_tenants,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Provision and bootstrap the managed deployment host agent.")
    parser.add_argument("--api-base-url", required=True, help="Admin API base URL, for example http://api:4000")
    parser.add_argument("--admin-bearer-token", default="", help="Admin bearer token. If omitted, basic auth is used.")
    parser.add_argument("--admin-username", default="", help="Admin username for basic auth when bearer token is omitted.")
    parser.add_argument("--admin-password", default="", help="Admin password for basic auth when bearer token is omitted.")
    parser.add_argument("--host-label", default="", help="Managed host label. Defaults to managed-$HOSTNAME.")
    parser.add_argument("--infrastructure-provider", choices=("aws", "hetzner"), default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--capabilities",
        default="restore_database,postgres,mysql,mariadb,local_preview_routes",
        help="Comma or newline separated host capabilities.",
    )
    parser.add_argument(
        "--bootstrap-token-path",
        required=True,
        help="Private file path where the bootstrap recovery token should be written.",
    )
    parser.add_argument(
        "--access-token-path",
        required=True,
        help="Private file path where the agent persists its access token.",
    )
    parser.add_argument(
        "--tenant-id",
        action="append",
        default=[],
        help="Optional tenant IDs to attach explicitly. Repeat to attach multiple tenants.",
    )
    parser.add_argument(
        "--no-assign-configured-tenants",
        action="store_true",
        help="Do not auto-attach the host to currently configured internal Coolify tenant planes.",
    )
    return parser.parse_args(argv)


def build_config(*, args: argparse.Namespace, environ: dict[str, str] | None = None) -> DeploymentHostBootstrapConfig:
    env = os.environ if environ is None else environ
    host_label = _normalize_optional_string(args.host_label) or _normalize_optional_string(env.get("MASTER_BUILDER_DEPLOYMENT_HOST_LABEL")) or _default_host_label()
    infrastructure_provider = _normalize_optional_string(args.infrastructure_provider) or _normalize_optional_string(
        env.get("MASTER_BUILDER_DEPLOYMENT_HOST_INFRASTRUCTURE_PROVIDER"),
    )
    region = _normalize_optional_string(args.region) or _normalize_optional_string(env.get("MASTER_BUILDER_DEPLOYMENT_HOST_REGION"))
    capabilities = _normalize_capabilities(
        _normalize_optional_string(args.capabilities) or env.get("MASTER_BUILDER_DEPLOYMENT_HOST_CAPABILITIES"),
    )
    if not capabilities:
        capabilities = ("restore_database", "postgres", "mysql", "mariadb")
    tenant_ids = tuple(
        tenant_id
        for tenant_id in (_normalize_optional_string(value) for value in args.tenant_id)
        if tenant_id is not None
    )
    return DeploymentHostBootstrapConfig(
        api_base_url=_normalize_base_url(args.api_base_url),
        auth_header=_resolve_auth_header(args=args, environ=env),
        host_label=host_label,
        infrastructure_provider=infrastructure_provider,
        region=region,
        capabilities=capabilities,
        bootstrap_token_path=Path(args.bootstrap_token_path).expanduser(),
        access_token_path=Path(args.access_token_path).expanduser(),
        tenant_ids=tenant_ids,
        assign_configured_tenants=not bool(args.no_assign_configured_tenants),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        config = build_config(args=parse_args(argv))
        summary = ensure_deployment_host_bootstrap(config)
    except (BootstrapConfigError, BootstrapApiError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
