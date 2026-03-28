from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from orchestrator.tools.jira_oauth_models import JiraOAuthError, JiraOAuthHttpError


class JiraOAuthHttpClient:
    def __init__(self, *, opener=urlopen):
        self._opener = opener

    def post_json(self, *, url: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = Request(
            url=url,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        return self._read_json_response(request=request, error_prefix="Jira OAuth request failed")

    def get_json(self, *, url: str, access_token: str) -> dict[str, Any] | list[Any]:
        request = Request(
            url=url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            },
            method="GET",
        )
        return self._read_json_response(request=request, error_prefix="Jira API request failed")

    def request_json(
        self,
        *,
        method: str,
        url: str,
        access_token: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any]:
        data: bytes | None = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = Request(
            url=url,
            data=data,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method=method,
        )
        return self._read_json_response(request=request, error_prefix="Jira API request failed")

    def get_bytes(self, *, url: str, access_token: str) -> bytes:
        request = Request(
            url=url,
            headers={
                "Accept": "*/*",
                "Authorization": f"Bearer {access_token}",
            },
            method="GET",
        )
        try:
            with self._opener(request, timeout=30) as response:
                return response.read()
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8", errors="ignore")
            raise JiraOAuthError(f"Jira API request failed ({exc.code}): {error_body}") from exc

    def post_multipart(
        self,
        *,
        url: str,
        access_token: str,
        filename: str,
        content: bytes,
        content_type: str = "application/octet-stream",
    ) -> dict[str, Any] | list[Any]:
        from uuid import uuid4

        boundary = f"--------------------------{uuid4().hex}"
        payload = BytesIO()
        payload.write(f"--{boundary}\r\n".encode("utf-8"))
        payload.write(
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode("utf-8")
        )
        payload.write(f"Content-Type: {content_type.strip()}\r\n\r\n".encode("utf-8"))
        payload.write(content)
        payload.write(f"\r\n--{boundary}--\r\n".encode("utf-8"))

        request = Request(
            url=url,
            data=payload.getvalue(),
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "X-Atlassian-Token": "no-check",
            },
            method="POST",
        )
        return self._read_json_response(request=request, error_prefix="Jira attachment upload failed")

    def _read_json_response(self, *, request: Request, error_prefix: str) -> dict[str, Any] | list[Any]:
        try:
            with self._opener(request, timeout=30) as response:
                response_body = response.read().decode("utf-8")
        except HTTPError as exc:
            error_body = exc.read().decode("utf-8")
            raise JiraOAuthHttpError(
                f"{error_prefix} ({exc.code}): {error_body}",
                status_code=exc.code,
                error_prefix=error_prefix,
                error_body=error_body,
            ) from exc

        if not response_body:
            return {}
        return json.loads(response_body)
