#!/usr/bin/env python3
from __future__ import annotations

import json
import ipaddress
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from cryptography.fernet import Fernet


COOLIFY_API_TOKEN_SECRET_NAME = "COOLIFY_API_TOKEN"
COOLIFY_API_TOKEN_REF_SUFFIX = f"/{COOLIFY_API_TOKEN_SECRET_NAME}"
DEFAULT_COOLIFY_API_BASE_URL = "http://host.docker.internal:8000/api/v1"
DEFAULT_LOCAL_COOLIFY_API_BASE_URL = "http://localhost:8000/api/v1"
DEFAULT_LOCAL_PREVIEW_PROXY_PORT = "8088"
LOCAL_SSH_HOST_CONTAINER = "master-builder-coolify-ssh-host"
LOCAL_SSH_HOST_IMAGE = "master-builder-coolify-ssh-host:latest"
LOCAL_SSH_HOST_NAME = "master-builder-coolify-ssh-host"
LOCAL_SSH_HOST_KEY_NAME = "Master Builder local Docker host key"


@dataclass(frozen=True)
class TenantPlane:
    tenant_id: str
    name: str
    github_config: dict[str, Any]
    deployment_plane_config: dict[str, Any]


def main() -> int:
    root_dir = Path(__file__).resolve().parents[1]
    _load_dotenv(root_dir / ".env")

    database_url = _psycopg_database_url(_required_env("ORCHESTRATOR_DATABASE_URL"))
    encryption_key = _required_env("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY")
    coolify_local_api_base_url = os.environ.get("LOCAL_COOLIFY_API_BASE_URL", DEFAULT_LOCAL_COOLIFY_API_BASE_URL)
    coolify_container_name = os.environ.get("LOCAL_COOLIFY_CONTAINER_NAME", "coolify")
    local_preview_base_domain = os.environ.get("LOCAL_PREVIEW_BASE_DOMAIN", "").strip()
    local_preview_lan_ip = os.environ.get("LOCAL_PREVIEW_LAN_IP", "").strip()
    local_preview_proxy_port = os.environ.get("LOCAL_PREVIEW_PROXY_PORT", DEFAULT_LOCAL_PREVIEW_PROXY_PORT).strip()

    _wait_for_coolify_api(coolify_local_api_base_url)
    _enable_coolify_api()
    ssh_private_key = _ensure_local_ssh_host(root_dir=root_dir, coolify_container_name=coolify_container_name)

    with psycopg.connect(database_url) as connection:
        tenants = _local_coolify_tenants(connection)
        if not tenants:
            print("No local Coolify deployment planes configured.")
            return 0

        token = _coolify_token(connection, encryption_key, coolify_local_api_base_url, coolify_container_name, tenants)
        server_uuid, destination_uuid = _configure_coolify_local_server(
            token=token,
            coolify_local_api_base_url=coolify_local_api_base_url,
            private_key=ssh_private_key,
        )
        app_uuid_by_tenant = {
            tenant.tenant_id: _ensure_github_app(
                connection=connection,
                encryption_key=encryption_key,
                token=token,
                tenant=tenant,
                coolify_local_api_base_url=coolify_local_api_base_url,
            )
            for tenant in tenants
        }

        for tenant in tenants:
            project_uuid = _ensure_project_and_environment(
                token=token,
                tenant=tenant,
                coolify_local_api_base_url=coolify_local_api_base_url,
            )
            plane = dict(tenant.deployment_plane_config)
            plane["api_base_url"] = _resolve_local_coolify_api_base_url(plane.get("api_base_url"))
            plane["base_domain"] = _resolve_local_preview_base_domain(
                existing_base_domain=plane.get("base_domain"),
                configured_base_domain=local_preview_base_domain,
                configured_lan_ip=local_preview_lan_ip,
                proxy_port=local_preview_proxy_port,
            )
            plane["coolify_project_uuid"] = project_uuid
            plane["coolify_environment_name"] = str(plane.get("coolify_environment_name") or "production")
            plane["coolify_server_uuid"] = server_uuid
            plane["coolify_destination_uuid"] = destination_uuid
            plane["coolify_github_app_uuid"] = app_uuid_by_tenant[tenant.tenant_id]
            secret_refs = dict(plane.get("secret_refs") or {})
            secret_refs["coolify_api_token"] = _coolify_token_ref(tenant)
            plane["secret_refs"] = secret_refs
            _update_tenant_deployment_plane(connection, tenant.tenant_id, plane)

    print("Local Coolify ready for Master Builder previews.")
    return 0


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _psycopg_database_url(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url.removeprefix("postgresql+psycopg://")
    return database_url


def _wait_for_coolify_api(base_url: str) -> None:
    deadline = time.monotonic() + 90
    last_error = ""
    health_url = f"{base_url.rstrip('/').removesuffix('/api/v1')}/api/health"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=5) as response:
                if response.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"Local Coolify API did not become ready at {health_url}: {last_error}")


def _enable_coolify_api() -> None:
    _run_coolify_db_sql("update instance_settings set is_api_enabled = true where id = 0;")


def _ensure_local_ssh_host(*, root_dir: Path, coolify_container_name: str) -> str:
    key_path = Path.home() / ".master-builder-coolify" / "ssh" / "master-builder-local-host"
    public_key_path = key_path.with_suffix(".pub")
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if not key_path.exists() or not public_key_path.exists():
        subprocess.check_call(["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key_path), "-C", "master-builder-local-coolify"])
    key_path.chmod(0o600)
    subprocess.check_call(
        [
            "docker",
            "build",
            "-t",
            LOCAL_SSH_HOST_IMAGE,
            "-f",
            str(root_dir / "scripts" / "local_coolify_ssh_host.Dockerfile"),
            str(root_dir / "scripts"),
        ],
        stdout=subprocess.DEVNULL,
    )
    if _docker_container_exists(LOCAL_SSH_HOST_CONTAINER):
        _ensure_container_running(LOCAL_SSH_HOST_CONTAINER)
        _ensure_container_network(LOCAL_SSH_HOST_CONTAINER, "coolify")
    else:
        subprocess.check_call(
            [
                "docker",
                "run",
                "-d",
                "--name",
                LOCAL_SSH_HOST_CONTAINER,
                "--network",
                "coolify",
                "--restart",
                "unless-stopped",
                "--label",
                "com.docker.compose.project=master-builder-coolify",
                "--label",
                "com.docker.compose.service=local-ssh-host",
                "-e",
                "AUTHORIZED_KEY_FILE=/run/secrets/authorized_key.pub",
                "-v",
                "/var/run/docker.sock:/var/run/docker.sock",
                "-v",
                f"{public_key_path}:/run/secrets/authorized_key.pub:ro",
                LOCAL_SSH_HOST_IMAGE,
            ],
            stdout=subprocess.DEVNULL,
        )
    private_key = key_path.read_text(encoding="utf-8")
    _wait_for_local_ssh_host(coolify_container_name=coolify_container_name, private_key=private_key)
    return private_key


def _docker_container_exists(container_name: str) -> bool:
    result = subprocess.run(
        ["docker", "container", "inspect", container_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _ensure_container_running(container_name: str) -> None:
    running = subprocess.check_output(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        text=True,
    ).strip()
    if running != "true":
        subprocess.check_call(["docker", "start", container_name], stdout=subprocess.DEVNULL)


def _ensure_container_network(container_name: str, network_name: str) -> None:
    networks = subprocess.check_output(
        ["docker", "inspect", "--format", "{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}", container_name],
        text=True,
    ).splitlines()
    if network_name not in {network.strip() for network in networks}:
        subprocess.check_call(["docker", "network", "connect", network_name, container_name], stdout=subprocess.DEVNULL)


def _wait_for_local_ssh_host(*, coolify_container_name: str, private_key: str) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        result = subprocess.run(
            [
                "docker",
                "exec",
                "-i",
                coolify_container_name,
                "sh",
                "-lc",
                (
                    "key_file=$(mktemp) && "
                    "trap 'rm -f \"$key_file\"' EXIT && "
                    "cat > \"$key_file\" && "
                    "chmod 600 \"$key_file\" && "
                    "ssh -i \"$key_file\" "
                    "-o BatchMode=yes "
                    "-o StrictHostKeyChecking=no "
                    "-o UserKnownHostsFile=/dev/null "
                    "-o ConnectTimeout=5 "
                    f"root@{LOCAL_SSH_HOST_CONTAINER} "
                    "\"docker version --format '{{.Server.Version}}' && docker compose version\""
                ),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            input=private_key,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return
        time.sleep(2)
    raise RuntimeError("Local Coolify SSH Docker host did not become reachable")


def _run_coolify_db_sql(sql: str) -> str:
    normalized_sql = " ".join(sql.split())
    return subprocess.check_output(
        [
            "docker",
            "exec",
            "coolify-db",
            "sh",
            "-lc",
            f"psql -U \"$POSTGRES_USER\" -d \"$POSTGRES_DB\" -tAc {json.dumps(normalized_sql)}",
        ],
        text=True,
    ).strip()


def _configure_coolify_local_server(
    *,
    token: str,
    coolify_local_api_base_url: str,
    private_key: str,
) -> tuple[str, str]:
    private_key_uuid = _create_coolify_private_key(
        token=token,
        coolify_local_api_base_url=coolify_local_api_base_url,
        private_key=private_key,
        name=LOCAL_SSH_HOST_KEY_NAME,
        description="SSH key used by local Coolify to run Docker preview deployments",
    )
    private_key_id = _run_coolify_db_sql(
        f"select id from private_keys where uuid={_sql(private_key_uuid)} order by id desc limit 1;"
    )
    if not private_key_id:
        raise RuntimeError("Coolify local Docker host private key was not persisted")
    _run_coolify_db_sql(
        f"""
        update servers
        set ip={_sql(LOCAL_SSH_HOST_CONTAINER)},
            port=22,
            "user"='root',
            private_key_id={int(private_key_id)},
            updated_at=now()
        where name='localhost';
        """
    )
    _run_coolify_db_sql(
        """
        update server_settings
        set is_reachable=true,
            is_usable=true,
            force_disabled=false,
            updated_at=now()
        where server_id in (select id from servers where name='localhost');
        """
    )
    return _local_coolify_server_and_destination()


def _local_coolify_tenants(connection: psycopg.Connection) -> list[TenantPlane]:
    with connection.cursor() as cursor:
        cursor.execute("select set_config('app.principal_type','platform_system',true)")
        cursor.execute(
            """
            select tenant_id, name, github_config, deployment_plane_config
            from tenants
            where deployment_plane_config ->> 'provider' = 'internal_coolify'
              and coalesce(deployment_plane_config ->> 'state', 'active') in ('active', 'degraded')
              and (
                deployment_plane_config ->> 'api_base_url' is null
                or deployment_plane_config ->> 'api_base_url' like 'http://host.docker.internal:8000%'
                or deployment_plane_config ->> 'api_base_url' like 'http://localhost:8000%'
                or deployment_plane_config ->> 'api_base_url' like 'http://127.0.0.1:8000%'
              )
            order by tenant_id
            """
        )
        rows = cursor.fetchall()
    return [
        TenantPlane(
            tenant_id=row[0],
            name=row[1],
            github_config=row[2] or {},
            deployment_plane_config=row[3] or {},
        )
        for row in rows
    ]


def _coolify_token(
    connection: psycopg.Connection,
    encryption_key: str,
    coolify_local_api_base_url: str,
    coolify_container_name: str,
    tenants: list[TenantPlane],
) -> str:
    for tenant in tenants:
        token_ref = _coolify_token_ref(tenant)
        existing = _managed_secret(connection, encryption_key, token_ref)
        if existing and _coolify_token_works(existing, coolify_local_api_base_url):
            for target_tenant in tenants:
                if not _managed_secret(connection, encryption_key, _coolify_token_ref(target_tenant)):
                    _upsert_managed_secret(connection, encryption_key, _coolify_token_ref(target_tenant), existing)
            return existing

    token = _create_coolify_token(coolify_container_name)
    if not _coolify_token_works(token, coolify_local_api_base_url):
        raise RuntimeError("Generated Coolify API token did not authenticate")
    for tenant in tenants:
        _upsert_managed_secret(connection, encryption_key, _coolify_token_ref(tenant), token)
    return token


def _resolve_local_coolify_api_base_url(existing_api_base_url: object) -> str:
    configured = str(existing_api_base_url or "").strip()
    if not configured:
        return DEFAULT_COOLIFY_API_BASE_URL
    normalized = configured.removesuffix("/")
    if normalized.startswith("http://localhost:8000") or normalized.startswith("http://127.0.0.1:8000"):
        return DEFAULT_COOLIFY_API_BASE_URL
    return configured


def _coolify_token_ref(tenant: TenantPlane) -> str:
    refs = tenant.deployment_plane_config.get("secret_refs")
    if isinstance(refs, dict):
        configured = str(refs.get("coolify_api_token") or "").strip()
        if configured:
            return configured
    return f"tenant/{tenant.tenant_id}/{COOLIFY_API_TOKEN_SECRET_NAME}"


def _create_coolify_token(container_name: str) -> str:
    subprocess.check_call(
        [
            "docker",
            "exec",
            container_name,
            "sh",
            "-lc",
            "php artisan tinker --execute='session([\"currentTeam\" => App\\\\Models\\\\Team::findOrFail(0)]); "
            "file_put_contents(\"/tmp/master-builder-local-token\", App\\\\Models\\\\User::findOrFail(0)"
            "->createToken(\"master-builder-local\", [\"root\", \"read\", \"write\", \"deploy\"])->plainTextToken);' >/dev/null",
        ]
    )
    token = subprocess.check_output(
        ["docker", "exec", container_name, "sh", "-lc", "cat /tmp/master-builder-local-token && rm -f /tmp/master-builder-local-token"],
        text=True,
    ).strip()
    if not token:
        raise RuntimeError("Coolify did not return a local API token")
    return token


def _coolify_token_works(token: str, coolify_local_api_base_url: str) -> bool:
    status, _body = _coolify_request(
        token=token,
        base_url=coolify_local_api_base_url,
        method="GET",
        path="/version",
        payload=None,
        raise_on_error=False,
    )
    return status == 200


def _resolve_local_preview_base_domain(
    *,
    existing_base_domain: object,
    configured_base_domain: str,
    configured_lan_ip: str,
    proxy_port: str,
) -> str:
    configured = configured_base_domain.strip()
    if configured:
        return configured

    existing = str(existing_base_domain or "").strip()
    if existing and not _is_localhost_base_domain(existing):
        return existing

    effective_port = _base_domain_port(existing) or _normalize_proxy_port(proxy_port)
    lan_ip = configured_lan_ip.strip() or _detect_lan_ipv4()
    return _preview_base_domain_for_lan_ip(lan_ip=lan_ip, port=effective_port)


def _is_localhost_base_domain(value: str) -> bool:
    host = _base_domain_hostname(value)
    if host is None:
        return False
    return host == "localhost" or host.endswith(".localhost") or host.startswith("127.")


def _base_domain_hostname(value: str) -> str | None:
    normalized = value.strip().lower().rstrip(".")
    if not normalized:
        return None
    parsed = urllib.parse.urlsplit(normalized if "://" in normalized else f"//{normalized}")
    return parsed.hostname


def _base_domain_port(value: str) -> str | None:
    normalized = value.strip().lower().rstrip(".")
    if not normalized:
        return None
    parsed = urllib.parse.urlsplit(normalized if "://" in normalized else f"//{normalized}")
    return str(parsed.port) if parsed.port is not None else None


def _normalize_proxy_port(value: str) -> str:
    normalized = str(value or "").strip() or DEFAULT_LOCAL_PREVIEW_PROXY_PORT
    try:
        port = int(normalized)
    except ValueError as exc:
        raise RuntimeError("LOCAL_PREVIEW_PROXY_PORT must be an integer port") from exc
    if port < 1 or port > 65535:
        raise RuntimeError("LOCAL_PREVIEW_PROXY_PORT must be between 1 and 65535")
    return str(port)


def _preview_base_domain_for_lan_ip(*, lan_ip: str, port: str) -> str:
    normalized_ip = str(ipaddress.ip_address(lan_ip.strip()))
    ip = ipaddress.ip_address(normalized_ip)
    if ip.version != 4:
        raise RuntimeError("LOCAL_PREVIEW_LAN_IP must be an IPv4 address")
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise RuntimeError("LOCAL_PREVIEW_LAN_IP must be reachable from another LAN device")
    wildcard_host = normalized_ip.replace(".", "-")
    return f"{wildcard_host}.sslip.io:{_normalize_proxy_port(port)}"


def _detect_lan_ipv4() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))
            detected = str(probe.getsockname()[0])
    except OSError as exc:
        raise RuntimeError(
            "Could not detect a LAN IPv4 address for local preview URLs. "
            "Set LOCAL_PREVIEW_LAN_IP or LOCAL_PREVIEW_BASE_DOMAIN."
        ) from exc
    _preview_base_domain_for_lan_ip(lan_ip=detected, port=DEFAULT_LOCAL_PREVIEW_PROXY_PORT)
    return detected


def _managed_secret(connection: psycopg.Connection, encryption_key: str, secret_ref: str) -> str | None:
    with connection.cursor() as cursor:
        cursor.execute("select value_encrypted from managed_secrets where secret_ref=%s", (secret_ref,))
        row = cursor.fetchone()
    if row is None:
        return None
    return Fernet(encryption_key.encode("utf-8")).decrypt(row[0].encode("utf-8")).decode("utf-8")


def _upsert_managed_secret(connection: psycopg.Connection, encryption_key: str, secret_ref: str, value: str) -> None:
    encrypted = Fernet(encryption_key.encode("utf-8")).encrypt(value.encode("utf-8")).decode("utf-8")
    with connection.cursor() as cursor:
        cursor.execute(
            """
            insert into managed_secrets (secret_ref, value_encrypted, created_at, updated_at)
            values (%s, %s, now(), now())
            on conflict (secret_ref)
            do update set value_encrypted=excluded.value_encrypted, updated_at=excluded.updated_at
            """,
            (secret_ref, encrypted),
        )
    connection.commit()


def _local_coolify_server_and_destination() -> tuple[str, str]:
    sql = """
    select s.uuid, d.uuid
    from servers s
    join standalone_dockers d on d.server_id = s.id
    where s.name = 'localhost'
    order by s.id asc, d.id asc
    limit 1;
    """
    output = _run_coolify_db_sql(sql)
    if "|" in output:
        server_uuid, destination_uuid = output.split("|", 1)
    else:
        parts = output.split()
        if len(parts) != 2:
            raise RuntimeError("Local Coolify server/destination was not found")
        server_uuid, destination_uuid = parts
    return server_uuid.strip(), destination_uuid.strip()


def _ensure_project_and_environment(token: str, tenant: TenantPlane, coolify_local_api_base_url: str) -> str:
    configured_uuid = str(tenant.deployment_plane_config.get("coolify_project_uuid") or "").strip()
    project_uuid = configured_uuid or _coolify_uuid()
    project_name = _safe_name(tenant.name or tenant.tenant_id)
    environment_name = str(tenant.deployment_plane_config.get("coolify_environment_name") or "production")
    environment_uuid = _coolify_uuid()
    _run_coolify_db_sql(
        f"""
        insert into projects (uuid, name, description, team_id, created_at, updated_at)
        values ({_sql(project_uuid)}, {_sql(project_name)}, 'Master Builder local previews', 0, now(), now())
        on conflict (uuid) do update set name=excluded.name, updated_at=excluded.updated_at;
        """
    )
    _run_coolify_db_sql(
        f"""
        insert into environments (uuid, name, project_id, description, created_at, updated_at)
        select {_sql(environment_uuid)}, {_sql(environment_name)}, p.id, 'Master Builder local previews', now(), now()
        from projects p
        where p.uuid = {_sql(project_uuid)}
        on conflict (name, project_id) do update set updated_at=excluded.updated_at;
        """
    )
    status, _body = _coolify_request(
        token=token,
        base_url=coolify_local_api_base_url,
        method="GET",
        path=f"/projects/{project_uuid}",
        payload=None,
        raise_on_error=False,
    )
    if status != 200:
        raise RuntimeError(f"Coolify project {project_uuid} was not readable after bootstrap")
    return project_uuid


def _ensure_github_app(
    *,
    connection: psycopg.Connection,
    encryption_key: str,
    token: str,
    tenant: TenantPlane,
    coolify_local_api_base_url: str,
) -> str:
    app_id_ref = str(tenant.github_config.get("app_id_ref") or "GITHUB_APP_ID")
    private_key_ref = str(tenant.github_config.get("private_key_ref") or "GITHUB_APP_PRIVATE_KEY")
    app_id = _managed_secret(connection, encryption_key, _platform_ref(app_id_ref))
    private_key = _managed_secret(connection, encryption_key, _platform_ref(private_key_ref))
    installation_id = str(tenant.github_config.get("installation_id") or "").strip()
    if not app_id or not private_key or not installation_id:
        raise RuntimeError(f"Tenant {tenant.tenant_id} is missing GitHub App configuration required by local Coolify")

    status, apps = _coolify_request(
        token=token,
        base_url=coolify_local_api_base_url,
        method="GET",
        path="/github-apps",
        payload=None,
    )
    if status != 200 or not isinstance(apps, list):
        raise RuntimeError("Coolify GitHub app list returned an unexpected response")
    for app in apps:
        if (
            isinstance(app, dict)
            and str(app.get("app_id") or "") == str(app_id)
            and str(app.get("installation_id") or "") == str(installation_id)
            and not app.get("is_public")
        ):
            return str(app["uuid"])

    key_uuid = _create_coolify_private_key(
        token=token,
        coolify_local_api_base_url=coolify_local_api_base_url,
        private_key=private_key,
    )
    client_secret = _managed_secret(connection, encryption_key, "GITHUB_CLIENT_SECRET") or secrets.token_urlsafe(32)
    status, created = _coolify_request(
        token=token,
        base_url=coolify_local_api_base_url,
        method="POST",
        path="/github-apps",
        payload={
            "name": "Master Builder GitHub App",
            "organization": _github_organization(tenant),
            "api_url": "https://api.github.com",
            "html_url": "https://github.com",
            "custom_user": "git",
            "custom_port": 22,
            "app_id": int(app_id),
            "installation_id": int(installation_id),
            "client_id": str(app_id),
            "client_secret": client_secret,
            "webhook_secret": secrets.token_urlsafe(32),
            "private_key_uuid": key_uuid,
            "is_system_wide": False,
        },
    )
    if status != 201 or not isinstance(created, dict) or not created.get("uuid"):
        raise RuntimeError(f"Coolify GitHub app creation failed with status {status}")
    return str(created["uuid"])


def _create_coolify_private_key(
    *,
    token: str,
    coolify_local_api_base_url: str,
    private_key: str,
    name: str = "Master Builder GitHub App key",
    description: str = "Master Builder local preview deployments",
) -> str:
    status, created = _coolify_request(
        token=token,
        base_url=coolify_local_api_base_url,
        method="POST",
        path="/security/keys",
        payload={
            "name": name,
            "description": description,
            "private_key": private_key,
        },
        raise_on_error=False,
    )
    if status == 201 and isinstance(created, dict) and created.get("uuid"):
        return str(created["uuid"])
    if status == 422 and "already exists" in json.dumps(created).lower():
        existing = _run_coolify_db_sql(
            f"select uuid from private_keys where name={_sql(name)} order by id desc limit 1;"
        )
        if existing:
            return existing
    raise RuntimeError(f"Coolify private key creation failed with status {status}")


def _coolify_request(
    *,
    token: str,
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None,
    raise_on_error: bool = True,
) -> tuple[int, Any]:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(f"{base_url.rstrip('/')}{path}", data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response_body = response.read().decode("utf-8")
            return response.status, _json_or_text(response_body)
    except urllib.error.HTTPError as exc:
        response_body = exc.read().decode("utf-8")
        parsed = _json_or_text(response_body)
        if raise_on_error:
            raise RuntimeError(f"Coolify API {method} {path} failed with status {exc.code}")
        return exc.code, parsed


def _update_tenant_deployment_plane(connection: psycopg.Connection, tenant_id: str, plane: dict[str, Any]) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "update tenants set deployment_plane_config=%s, updated_at=now() where tenant_id=%s",
            (json.dumps(plane), tenant_id),
        )
    connection.commit()


def _json_or_text(response_body: str) -> Any:
    if not response_body:
        return {}
    try:
        return json.loads(response_body)
    except json.JSONDecodeError:
        return response_body


def _platform_ref(secret_ref: str) -> str:
    return secret_ref if secret_ref.startswith("platform/") else f"platform/{secret_ref}"


def _github_organization(tenant: TenantPlane) -> str:
    repository = str(tenant.github_config.get("repository") or "").strip()
    if repository and "/" in repository:
        return repository.split("/", 1)[0].removeprefix("https://github.com/")
    return tenant.tenant_id


def _safe_name(value: str) -> str:
    normalized = " ".join(value.replace("_", " ").split())
    return normalized[:255] or "Master Builder previews"


def _coolify_uuid() -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _sql(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Local Coolify bootstrap failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
