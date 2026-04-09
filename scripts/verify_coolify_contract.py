#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4


REQUIRED_ENV_VARS = (
    "COOLIFY_VERIFY_BASE_URL",
    "COOLIFY_VERIFY_API_TOKEN",
    "COOLIFY_VERIFY_PROJECT_UUID",
    "COOLIFY_VERIFY_SERVER_UUID",
    "COOLIFY_VERIFY_DESTINATION_UUID",
    "COOLIFY_VERIFY_REPOSITORY",
)

DEFAULT_BRANCH = "main"
DEFAULT_ENVIRONMENT_NAME = "production"
DEFAULT_BUILD_PACK = "dockerfile"
DEFAULT_TIMEOUT_SECONDS = 600
DEFAULT_POLL_INTERVAL_SECONDS = 5

SUCCESS_STATUSES = {
    "success",
    "succeeded",
    "completed",
    "done",
}

FAILURE_STATUSES = {
    "failed",
    "error",
    "cancelled",
    "cancelled-by-user",
    "stopped",
    "timeout",
}


@dataclass(frozen=True)
class CoolifyVerifyConfig:
    base_url: str
    api_token: str
    project_uuid: str
    server_uuid: str
    destination_uuid: str
    repository: str
    branch: str
    environment_name: str
    build_pack: str
    application_name: str
    timeout_seconds: int
    poll_interval_seconds: int
    keep_application: bool
    force_deploy: bool
    instant_deploy: bool


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


class CoolifyVerificationError(RuntimeError):
    pass


class CoolifyHttpClient:
    def __init__(self, *, base_url: str, api_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_token = api_token

    def request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        expected_status: set[int] | None = None,
    ) -> dict[str, Any] | list[Any]:
        query_string = f"?{urlencode({key: str(value) for key, value in (query or {}).items() if value is not None})}" if query else ""
        url = f"{self._base_url}{path}{query_string}"
        body = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_token}",
            "User-Agent": "master-builder-coolify-contract-verifier",
        }
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        request = Request(url=url, method=method, data=body, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                status_code = getattr(response, "status", response.getcode())
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="replace")
            raise CoolifyVerificationError(
                f"{method} {path} failed with HTTP {exc.code}: {error_body}"
            ) from exc
        except URLError as exc:
            raise CoolifyVerificationError(f"{method} {path} failed: {exc.reason}") from exc

        if expected_status is not None and status_code not in expected_status:
            raise CoolifyVerificationError(
                f"{method} {path} returned HTTP {status_code}, expected one of {sorted(expected_status)}"
            )
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CoolifyVerificationError(f"{method} {path} returned invalid JSON: {raw[:200]}") from exc

    def get_project(self, project_uuid: str) -> dict[str, Any]:
        data = self.request_json(method="GET", path=f"/projects/{project_uuid}")
        if not isinstance(data, dict):
            raise CoolifyVerificationError("Coolify project response was not an object")
        return data

    def get_environment(self, project_uuid: str, environment_name: str) -> dict[str, Any]:
        data = self.request_json(method="GET", path=f"/projects/{project_uuid}/{environment_name}")
        if not isinstance(data, dict):
            raise CoolifyVerificationError("Coolify environment response was not an object")
        return data

    def get_server(self, server_uuid: str) -> dict[str, Any]:
        data = self.request_json(method="GET", path=f"/servers/{server_uuid}")
        if not isinstance(data, dict):
            raise CoolifyVerificationError("Coolify server response was not an object")
        return data

    def create_public_application(self, *, payload: dict[str, Any]) -> str:
        data = self.request_json(
            method="POST",
            path="/applications/public",
            payload=payload,
            expected_status={200, 201},
        )
        if not isinstance(data, dict):
            raise CoolifyVerificationError("Coolify create application response was not an object")
        application_uuid = str(data.get("uuid") or data.get("application_uuid") or "").strip()
        if not application_uuid:
            raise CoolifyVerificationError("Coolify create application response did not include uuid")
        return application_uuid

    def get_application(self, application_uuid: str) -> dict[str, Any]:
        data = self.request_json(method="GET", path=f"/applications/{application_uuid}")
        if not isinstance(data, dict):
            raise CoolifyVerificationError("Coolify application response was not an object")
        return data

    def update_application(self, *, application_uuid: str, payload: dict[str, Any]) -> dict[str, Any]:
        data = self.request_json(
            method="PATCH",
            path=f"/applications/{application_uuid}",
            payload=payload,
            expected_status={200, 201},
        )
        if not isinstance(data, dict):
            raise CoolifyVerificationError("Coolify update application response was not an object")
        return data

    def bulk_update_application_envs(self, *, application_uuid: str, envs: list[dict[str, Any]]) -> dict[str, Any]:
        data = self.request_json(
            method="PATCH",
            path=f"/applications/{application_uuid}/envs/bulk",
            payload={"data": envs},
            expected_status={200, 201},
        )
        if not isinstance(data, dict):
            raise CoolifyVerificationError("Coolify bulk env response was not an object")
        return data

    def list_application_envs(self, application_uuid: str) -> list[dict[str, Any]]:
        data = self.request_json(method="GET", path=f"/applications/{application_uuid}/envs")
        if not isinstance(data, list):
            raise CoolifyVerificationError("Coolify env list response was not a list")
        return [item for item in data if isinstance(item, dict)]

    def start_application(self, application_uuid: str, *, force: bool, instant_deploy: bool) -> str:
        response = self.request_json(
            method="GET",
            path=f"/applications/{application_uuid}/start",
            query={"force": str(force).lower(), "instant_deploy": str(instant_deploy).lower()},
            expected_status={200},
        )
        if not isinstance(response, dict):
            raise CoolifyVerificationError("Coolify start response was not an object")
        deployment_uuid = str(response.get("deployment_uuid") or "").strip()
        if not deployment_uuid:
            raise CoolifyVerificationError("Coolify start response did not include deployment_uuid")
        return deployment_uuid

    def get_deployment(self, deployment_uuid: str) -> dict[str, Any]:
        data = self.request_json(method="GET", path=f"/deployments/{deployment_uuid}")
        if not isinstance(data, dict):
            raise CoolifyVerificationError("Coolify deployment response was not an object")
        return data

    def list_application_deployments(self, application_uuid: str) -> list[dict[str, Any]]:
        data = self.request_json(method="GET", path=f"/deployments/applications/{application_uuid}")
        if not isinstance(data, list):
            raise CoolifyVerificationError("Coolify deployment list response was not a list")
        return [item for item in data if isinstance(item, dict)]

    def delete_application(self, application_uuid: str) -> dict[str, Any]:
        data = self.request_json(
            method="DELETE",
            path=f"/applications/{application_uuid}",
            query={
                "delete_configurations": "true",
                "delete_volumes": "true",
                "docker_cleanup": "true",
                "delete_connected_networks": "true",
            },
            expected_status={200, 204},
        )
        if not isinstance(data, dict):
            raise CoolifyVerificationError("Coolify delete application response was not an object")
        return data


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify a live Coolify contract against an existing instance.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually perform the live create/update/deploy/status cycle.",
    )
    parser.add_argument(
        "--keep-application",
        action="store_true",
        help="Skip cleanup so the temporary application remains available for debugging.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Maximum time to wait for the deployment to reach a terminal status.",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="Polling interval while waiting for the deployment status to change.",
    )
    return parser.parse_args(argv)


def load_config_from_env(*, args: argparse.Namespace, environ: dict[str, str] | None = None) -> CoolifyVerifyConfig:
    env = os.environ if environ is None else environ
    missing = [name for name in REQUIRED_ENV_VARS if not str(env.get(name, "")).strip()]
    if missing:
        raise CoolifyVerificationError(
            "Missing required env vars: " + ", ".join(missing)
        )

    timeout_seconds = _parse_int_env(
        env.get("COOLIFY_VERIFY_TIMEOUT_SECONDS"),
        default=args.timeout_seconds,
        field_name="COOLIFY_VERIFY_TIMEOUT_SECONDS",
    )
    poll_interval_seconds = _parse_int_env(
        env.get("COOLIFY_VERIFY_POLL_INTERVAL_SECONDS"),
        default=args.poll_interval_seconds,
        field_name="COOLIFY_VERIFY_POLL_INTERVAL_SECONDS",
    )
    if timeout_seconds < 1:
        raise CoolifyVerificationError("COOLIFY_VERIFY_TIMEOUT_SECONDS must be at least 1")
    if poll_interval_seconds < 1:
        raise CoolifyVerificationError("COOLIFY_VERIFY_POLL_INTERVAL_SECONDS must be at least 1")

    application_name = str(env.get("COOLIFY_VERIFY_APP_NAME", "")).strip()
    if not application_name:
        application_name = f"mb-contract-{uuid4().hex[:10]}"

    return CoolifyVerifyConfig(
        base_url=str(env["COOLIFY_VERIFY_BASE_URL"]).strip(),
        api_token=str(env["COOLIFY_VERIFY_API_TOKEN"]).strip(),
        project_uuid=str(env["COOLIFY_VERIFY_PROJECT_UUID"]).strip(),
        server_uuid=str(env["COOLIFY_VERIFY_SERVER_UUID"]).strip(),
        destination_uuid=str(env["COOLIFY_VERIFY_DESTINATION_UUID"]).strip(),
        repository=str(env["COOLIFY_VERIFY_REPOSITORY"]).strip(),
        branch=str(env.get("COOLIFY_VERIFY_BRANCH", DEFAULT_BRANCH)).strip() or DEFAULT_BRANCH,
        environment_name=str(env.get("COOLIFY_VERIFY_ENVIRONMENT_NAME", DEFAULT_ENVIRONMENT_NAME)).strip()
        or DEFAULT_ENVIRONMENT_NAME,
        build_pack=str(env.get("COOLIFY_VERIFY_BUILD_PACK", DEFAULT_BUILD_PACK)).strip() or DEFAULT_BUILD_PACK,
        application_name=application_name,
        timeout_seconds=timeout_seconds,
        poll_interval_seconds=poll_interval_seconds,
        keep_application=bool(args.keep_application),
        force_deploy=str(env.get("COOLIFY_VERIFY_FORCE_DEPLOY", "")).strip().lower() in {"1", "true", "yes"},
        instant_deploy=str(env.get("COOLIFY_VERIFY_INSTANT_DEPLOY", "")).strip().lower() in {"1", "true", "yes"},
    )


def _parse_int_env(value: object, *, default: int, field_name: str) -> int:
    normalized = str(value or "").strip()
    if not normalized:
        return int(default)
    try:
        return int(normalized)
    except ValueError as exc:
        raise CoolifyVerificationError(f"{field_name} must be an integer") from exc


def _now_stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_value(value: object) -> str:
    return str(value or "").strip()


def _build_application_payload(config: CoolifyVerifyConfig) -> dict[str, Any]:
    description = (
        "Temporary live contract verification application created by master-builder. "
        f"Run at {_now_stamp()}."
    )
    return {
        "project_uuid": config.project_uuid,
        "server_uuid": config.server_uuid,
        "environment_name": config.environment_name,
        "destination_uuid": config.destination_uuid,
        "git_repository": config.repository,
        "git_branch": config.branch,
        "build_pack": config.build_pack,
        "name": config.application_name,
        "description": description,
        "ports_exposes": "",
        "is_auto_deploy_enabled": False,
        "is_force_https_enabled": True,
    }


def _build_env_payload(run_id: str) -> list[dict[str, Any]]:
    return [
        {
            "key": "MB_CONTRACT_RUN_ID",
            "value": run_id,
            "is_literal": True,
            "is_preview": False,
        },
        {
            "key": "MB_CONTRACT_VERIFICATION",
            "value": "true",
            "is_literal": True,
            "is_preview": False,
        },
    ]


def _status_bucket(status: str) -> str:
    normalized = status.strip().lower()
    if normalized in SUCCESS_STATUSES:
        return "success"
    if normalized in FAILURE_STATUSES:
        return "failure"
    return "pending"


def _format_check(name: str, ok: bool, detail: str) -> str:
    return f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}"


def _print_check(result: CheckResult) -> None:
    print(_format_check(result.name, result.ok, result.detail))


def wait_for_terminal_deployment(
    *,
    client: CoolifyHttpClient,
    deployment_uuid: str,
    timeout_seconds: int,
    poll_interval_seconds: int,
    sleep_fn=time.sleep,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_payload: dict[str, Any] | None = None
    while True:
        payload = client.get_deployment(deployment_uuid)
        last_payload = payload
        status = _normalize_value(payload.get("status"))
        if status and _status_bucket(status) != "pending":
            return payload
        if time.monotonic() >= deadline:
            break
        sleep_fn(poll_interval_seconds)
    if last_payload is None:
        raise CoolifyVerificationError("Deployment status polling returned no payload")
    raise CoolifyVerificationError(
        f"Deployment {deployment_uuid} did not reach a terminal status within {timeout_seconds}s "
        f"(last status: {_normalize_value(last_payload.get('status')) or 'unknown'})"
    )


def verify_contract(
    *,
    config: CoolifyVerifyConfig,
    client: CoolifyHttpClient,
    sleep_fn=time.sleep,
) -> tuple[int, list[CheckResult], dict[str, Any]]:
    checks: list[CheckResult] = []
    application_uuid: str | None = None
    deployment_uuid: str | None = None
    run_id = uuid4().hex
    cleanup_error: str | None = None

    try:
        project_payload = client.get_project(config.project_uuid)
        checks.append(
            CheckResult(
                name="project lookup",
                ok=True,
                detail=f"found project {project_payload.get('uuid') or config.project_uuid}",
            )
        )

        environment_payload = client.get_environment(config.project_uuid, config.environment_name)
        checks.append(
            CheckResult(
                name="environment lookup",
                ok=True,
                detail=f"found environment {environment_payload.get('name') or config.environment_name}",
            )
        )

        server_payload = client.get_server(config.server_uuid)
        checks.append(
            CheckResult(
                name="server lookup",
                ok=True,
                detail=f"found server {server_payload.get('uuid') or config.server_uuid}",
            )
        )

        application_payload = _build_application_payload(config)
        application_uuid = client.create_public_application(payload=application_payload)
        checks.append(
            CheckResult(
                name="application create",
                ok=True,
                detail=f"created temporary application {application_uuid}",
            )
        )

        created_application = client.get_application(application_uuid)
        checks.append(
            CheckResult(
                name="application read",
                ok=_normalize_value(created_application.get("uuid")) == application_uuid,
                detail=f"round-tripped application uuid {created_application.get('uuid')}",
            )
        )

        updated_description = f"{application_payload['description']} Updated for live contract verification."
        update_payload = {
            **application_payload,
            "description": updated_description,
        }
        client.update_application(application_uuid=application_uuid, payload=update_payload)
        updated_application = client.get_application(application_uuid)
        checks.append(
            CheckResult(
                name="application update",
                ok=_normalize_value(updated_application.get("description")) == updated_description,
                detail="application description updated and persisted",
            )
        )

        env_payload = _build_env_payload(run_id)
        client.bulk_update_application_envs(application_uuid=application_uuid, envs=env_payload)
        env_rows = client.list_application_envs(application_uuid)
        env_keys = {str(item.get("key") or "").strip() for item in env_rows}
        checks.append(
            CheckResult(
                name="environment bulk update",
                ok={"MB_CONTRACT_RUN_ID", "MB_CONTRACT_VERIFICATION"}.issubset(env_keys),
                detail=f"env keys present: {', '.join(sorted(env_keys)) or 'none'}",
            )
        )

        deployment_uuid = client.start_application(
            application_uuid,
            force=config.force_deploy,
            instant_deploy=config.instant_deploy,
        )
        checks.append(
            CheckResult(
                name="deployment start",
                ok=True,
                detail=f"deployment queued as {deployment_uuid}",
            )
        )

        deployment_payload = wait_for_terminal_deployment(
            client=client,
            deployment_uuid=deployment_uuid,
            timeout_seconds=config.timeout_seconds,
            poll_interval_seconds=config.poll_interval_seconds,
            sleep_fn=sleep_fn,
        )
        final_status = _normalize_value(deployment_payload.get("status"))
        checks.append(
            CheckResult(
                name="deployment status",
                ok=_status_bucket(final_status) == "success",
                detail=f"final status {final_status or 'unknown'}",
            )
        )

        listed_deployments = client.list_application_deployments(application_uuid)
        deployment_ids = {str(item.get("uuid") or "").strip() for item in listed_deployments}
        checks.append(
            CheckResult(
                name="deployment history",
                ok=deployment_uuid in deployment_ids,
                detail=f"deployment history contains {deployment_uuid}",
            )
        )

        ok = all(result.ok for result in checks)
    except Exception as exc:  # noqa: BLE001
        checks.append(CheckResult(name="execution", ok=False, detail=str(exc)))
        ok = False
    finally:
        if application_uuid and not config.keep_application:
            try:
                client.delete_application(application_uuid)
                checks.append(
                    CheckResult(
                        name="cleanup",
                        ok=True,
                        detail=f"deleted temporary application {application_uuid}",
                    )
                )
            except Exception as exc:  # noqa: BLE001
                cleanup_error = str(exc)
                checks.append(
                    CheckResult(
                        name="cleanup",
                        ok=False,
                        detail=f"failed to delete temporary application {application_uuid}: {exc}",
                    )
                )
                ok = False

    summary = {
        "ok": ok,
        "application_uuid": application_uuid,
        "deployment_uuid": deployment_uuid,
        "keep_application": config.keep_application,
        "cleanup_error": cleanup_error,
        "run_id": run_id,
        "checks": [
            {"name": result.name, "ok": result.ok, "detail": result.detail}
            for result in checks
        ],
    }
    return (0 if ok else 1), checks, summary


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.execute:
        print("Refusing to execute without --execute.")
        print("Use --execute together with the required COOLIFY_VERIFY_* env vars.")
        return 2

    try:
        config = load_config_from_env(args=args)
    except CoolifyVerificationError as exc:
        print(f"Configuration error: {exc}")
        return 2

    client = CoolifyHttpClient(base_url=config.base_url, api_token=config.api_token)
    exit_code, checks, summary = verify_contract(config=config, client=client)
    for check in checks:
        _print_check(check)
    print(json.dumps(summary, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
