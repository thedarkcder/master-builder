from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class CoolifyApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class CoolifyApiConfig:
    base_url: str
    bearer_token: str


@dataclass(frozen=True)
class CoolifyDeploymentSubmission:
    application_uuid: str
    deployment_uuid: str | None


class CoolifyApiClient:
    def __init__(self, config: CoolifyApiConfig):
        self._config = config

    def _request_json(
        self,
        *,
        method: str,
        path: str,
        payload: dict | None = None,
    ) -> dict:
        base_url = self._config.base_url.rstrip("/")
        url = f"{base_url}{path}"
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._config.bearer_token}",
            "User-Agent": "master-builder-orchestrator",
        }
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")

        request = Request(url=url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise CoolifyApiError(f"Coolify API request failed ({exc.code}) for {method} {path}: {error_body}") from exc

        if not response_body:
            return {}
        return json.loads(response_body)

    def create_public_application(self, *, payload: dict) -> str:
        response = self._request_json(
            method="POST",
            path="/applications/public",
            payload=payload,
        )
        application_uuid = response.get("uuid") or response.get("application_uuid")
        if not isinstance(application_uuid, str) or not application_uuid.strip():
            raise CoolifyApiError("Coolify create application response did not include uuid")
        return application_uuid.strip()

    def create_private_github_app_application(self, *, payload: dict) -> str:
        response = self._request_json(
            method="POST",
            path="/applications/private-github-app",
            payload=payload,
        )
        application_uuid = response.get("uuid") or response.get("application_uuid")
        if not isinstance(application_uuid, str) or not application_uuid.strip():
            raise CoolifyApiError("Coolify create private GitHub application response did not include uuid")
        return application_uuid.strip()

    def update_application(self, *, application_uuid: str, payload: dict) -> dict:
        return self._request_json(
            method="PATCH",
            path=f"/applications/{application_uuid}",
            payload=payload,
        )

    def get_application(self, *, application_uuid: str) -> dict:
        return self._request_json(
            method="GET",
            path=f"/applications/{application_uuid}",
        )

    def delete_application(self, *, application_uuid: str) -> dict:
        return self._request_json(
            method="DELETE",
            path=f"/applications/{application_uuid}",
        )

    def bulk_update_application_envs(self, *, application_uuid: str, payload: dict) -> dict:
        return self._request_json(
            method="PATCH",
            path=f"/applications/{application_uuid}/envs/bulk",
            payload=payload,
        )

    def bulk_update_service_envs(self, *, service_uuid: str, payload: dict) -> dict:
        return self._request_json(
            method="PATCH",
            path=f"/services/{service_uuid}/envs/bulk",
            payload=payload,
        )

    def start_application(self, *, application_uuid: str) -> str | None:
        response = self._request_json(
            method="GET",
            path=f"/applications/{application_uuid}/start",
        )
        deployment_uuid = response.get("deployment_uuid") or response.get("uuid")
        if deployment_uuid is None:
            return None
        if not isinstance(deployment_uuid, str):
            raise CoolifyApiError("Coolify start application response returned non-string deployment uuid")
        normalized = deployment_uuid.strip()
        return normalized or None

    def create_application_storage(self, *, application_uuid: str, payload: dict) -> str | None:
        response = self._request_json(
            method="POST",
            path=f"/applications/{application_uuid}/storages",
            payload=payload,
        )
        storage_uuid = response.get("uuid") or response.get("storage_uuid")
        if storage_uuid is None:
            return None
        if not isinstance(storage_uuid, str):
            raise CoolifyApiError("Coolify create application storage response returned non-string uuid")
        normalized = storage_uuid.strip()
        return normalized or None

    def update_application_storage(self, *, application_uuid: str, payload: dict) -> dict:
        return self._request_json(
            method="PATCH",
            path=f"/applications/{application_uuid}/storages",
            payload=payload,
        )

    def create_database(self, *, database_type: str, payload: dict) -> str:
        response = self._request_json(
            method="POST",
            path=f"/databases/{database_type}",
            payload=payload,
        )
        database_uuid = response.get("uuid") or response.get("database_uuid")
        if not isinstance(database_uuid, str) or not database_uuid.strip():
            raise CoolifyApiError("Coolify create database response did not include uuid")
        return database_uuid.strip()

    def update_database(self, *, database_uuid: str, payload: dict) -> dict:
        return self._request_json(
            method="PATCH",
            path=f"/databases/{database_uuid}",
            payload=payload,
        )

    def create_database_backup(self, *, database_uuid: str, payload: dict) -> str | None:
        response = self._request_json(
            method="POST",
            path=f"/databases/{database_uuid}/backups",
            payload=payload,
        )
        backup_uuid = response.get("uuid") or response.get("backup_uuid")
        if backup_uuid is None:
            return None
        if not isinstance(backup_uuid, str):
            raise CoolifyApiError("Coolify create database backup response returned non-string uuid")
        normalized = backup_uuid.strip()
        return normalized or None

    def update_database_backup(self, *, database_uuid: str, backup_uuid: str, payload: dict) -> dict:
        return self._request_json(
            method="PATCH",
            path=f"/databases/{database_uuid}/backups/{backup_uuid}",
            payload=payload,
        )

    def trigger_database_backup(self, *, database_uuid: str, backup_uuid: str) -> dict:
        return self.update_database_backup(
            database_uuid=database_uuid,
            backup_uuid=backup_uuid,
            payload={"backup_now": True},
        )

    def list_database_backups(self, *, database_uuid: str) -> list[dict]:
        response = self._request_json(
            method="GET",
            path=f"/databases/{database_uuid}/backups",
        )
        backups = response.get("backups")
        if isinstance(backups, list):
            return [backup for backup in backups if isinstance(backup, dict)]
        if isinstance(response, list):
            return [backup for backup in response if isinstance(backup, dict)]
        return []

    def list_database_backup_executions(self, *, database_uuid: str, backup_uuid: str) -> list[dict]:
        response = self._request_json(
            method="GET",
            path=f"/databases/{database_uuid}/backups/{backup_uuid}/executions",
        )
        executions = response.get("executions")
        if isinstance(executions, list):
            return [execution for execution in executions if isinstance(execution, dict)]
        if isinstance(response, list):
            return [execution for execution in response if isinstance(execution, dict)]
        return []

    def create_service(self, *, payload: dict) -> str:
        response = self._request_json(
            method="POST",
            path="/services",
            payload=payload,
        )
        service_uuid = response.get("uuid") or response.get("service_uuid")
        if not isinstance(service_uuid, str) or not service_uuid.strip():
            raise CoolifyApiError("Coolify create service response did not include uuid")
        return service_uuid.strip()

    def update_service(self, *, service_uuid: str, payload: dict) -> dict:
        return self._request_json(
            method="PATCH",
            path=f"/services/{service_uuid}",
            payload=payload,
        )

    def get_service(self, *, service_uuid: str) -> dict:
        return self._request_json(
            method="GET",
            path=f"/services/{service_uuid}",
        )

    def start_service(self, *, service_uuid: str) -> None:
        self._request_json(
            method="POST",
            path=f"/services/{service_uuid}/start",
        )

    def restart_service(self, *, service_uuid: str) -> None:
        self._request_json(
            method="POST",
            path=f"/services/{service_uuid}/restart",
        )

    def get_deployment(self, *, deployment_uuid: str) -> dict:
        return self._request_json(
            method="GET",
            path=f"/deployments/{deployment_uuid}",
        )

    def list_application_deployments(
        self,
        *,
        application_uuid: str,
        skip: int = 0,
        take: int = 10,
    ) -> list[dict]:
        response = self._request_json(
            method="GET",
            path=f"/deployments/applications/{application_uuid}?skip={max(0, int(skip))}&take={max(1, int(take))}",
        )
        if isinstance(response, list):
            return [item for item in response if isinstance(item, dict)]
        raise CoolifyApiError("Coolify list application deployments response was not a list")
