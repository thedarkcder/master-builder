"""Authenticated stable delivery for retained, run-scoped QA artifacts."""

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from minio.error import S3Error
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.core.config import get_settings
from orchestrator.core.qa.artifact_storage import (
    private_storage_client,
    validate_object_key,
)
from orchestrator.core.qa.demo_service import storage_config_from_settings
from orchestrator.core.security import (
    AuthenticatedPrincipal,
    require_authenticated_principal,
    require_tenant_workspace_access,
)
from orchestrator.storage.models import Run

router = APIRouter(tags=["qa-artifacts"])


def _range(value: str | None, size: int) -> tuple[int, int, int]:
    if not value:
        return 0, size, 200
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", value)
    if match is None or not any(match.groups()) or size <= 0:
        raise HTTPException(
            416,
            "Unsupported artifact byte range",
            headers={"Content-Range": f"bytes */{size}"},
        )
    left, right = match.groups()
    if not left:
        length = min(int(right), size)
        start = size - length
    else:
        start = int(left)
        end = min(int(right), size - 1) if right else size - 1
        length = end - start + 1
    if start >= size or length <= 0:
        raise HTTPException(
            416,
            "Artifact byte range outside object",
            headers={"Content-Range": f"bytes */{size}"},
        )
    return start, length, 206


@router.get("/api/qa-artifacts/{tenant_id}/{project_id}/{run_id}/{artifact_path:path}")
def download_qa_artifact(
    tenant_id: str,
    project_id: str,
    run_id: str,
    artifact_path: str,
    request: Request,
    principal: AuthenticatedPrincipal = Depends(require_authenticated_principal),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    require_tenant_workspace_access(principal=principal, tenant_id=tenant_id)
    run = session.get(Run, run_id)
    if run is None or run.tenant_id != tenant_id or run.project_id != project_id:
        raise HTTPException(404, "QA artifact run not found")
    try:
        key = validate_object_key(f"{tenant_id}/{project_id}/{run_id}/{artifact_path}")
    except ValueError:
        raise HTTPException(404, "Invalid QA artifact path") from None
    settings = get_settings()
    storage = storage_config_from_settings(settings)
    client = private_storage_client(
        storage, timeout_seconds=settings.qa_demo_artifact_url_timeout_seconds
    )
    try:
        info = client.stat_object(storage.bucket, key)
        size = int(info.size)
        if size <= 0 or size > settings.qa_demo_artifact_max_download_bytes:
            raise HTTPException(413, "QA artifact exceeds configured download size")
        offset, length, response_status = _range(request.headers.get("range"), size)
        response = client.get_object(storage.bucket, key, offset=offset, length=length)
    except S3Error as exc:
        if exc.code in {"NoSuchKey", "NoSuchObject", "NoSuchBucket"}:
            raise HTTPException(
                404, "QA artifact is unavailable or has expired"
            ) from None
        raise HTTPException(503, "QA artifact storage unavailable") from None
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(length),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; media-src 'self'",
    }
    media_type = (
        info.content_type
        if info.content_type in {"video/mp4", "video/webm"}
        else "application/octet-stream"
    )
    disposition = "inline" if media_type.startswith("video/") else "attachment"
    headers["Content-Disposition"] = (
        f"{disposition}; filename*=UTF-8''{quote(key.rsplit('/', 1)[-1], safe='')}"
    )
    if response_status == 206:
        headers["Content-Range"] = f"bytes {offset}-{offset + length - 1}/{size}"

    def chunks():
        try:
            yield from response.stream(64 * 1024)
        finally:
            response.close()
            response.release_conn()

    return StreamingResponse(
        chunks(), status_code=response_status, media_type=media_type, headers=headers
    )
