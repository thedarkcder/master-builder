from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from orchestrator.api.dependencies import get_session
from orchestrator.api.schemas import (
    ManagedSecretRead,
    ManagedSecretResolveRequest,
    ManagedSecretResolveResult,
    ManagedSecretUpsert,
)
from orchestrator.core.platform.secret_service import platform_secret_service
from orchestrator.core.config import get_settings
from orchestrator.core.platform.tenant_secret_service import tenant_secret_service
from orchestrator.core.platform.secret_manager import (
    normalize_secret_ref,
)
from orchestrator.core.security import require_admin
from orchestrator.storage.models import ManagedSecret, Tenant

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _secret_metadata_to_schema(metadata) -> ManagedSecretRead:  # noqa: ANN001
    return ManagedSecretRead(
        secret_ref=metadata.secret_ref,
        source=metadata.source,
        updated_at=metadata.updated_at,
    )


def _build_tenant_secret_ref(*, tenant_id: str, secret_key: str) -> str:
    normalized_key = normalize_secret_ref(secret_key)
    if normalized_key.startswith(("platform/", "tenant/", "project/")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Tenant secret key must not include a scope prefix",
        )
    return f"tenant/{tenant_id}/{normalized_key}"


def _require_tenant_exists(*, session: Session, tenant_id: str) -> None:
    if session.get(Tenant, tenant_id) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tenant not found"
        )


@router.get("/secrets", response_model=list[ManagedSecretRead])
def list_secrets(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ManagedSecretRead]:
    refs = platform_secret_service.list_secret_refs(session=session)
    return [_secret_metadata_to_schema(metadata) for metadata in refs]


@router.put("/secrets/{secret_ref:path}", response_model=ManagedSecretRead)
def upsert_secret(
    secret_ref: str,
    payload: ManagedSecretUpsert,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ManagedSecretRead:
    settings = get_settings()
    try:
        metadata = platform_secret_service.upsert_secret(
            session=session,
            secret_ref=secret_ref,
            plaintext_value=payload.value,
            encryption_key=settings.secrets_encryption_key,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _secret_metadata_to_schema(metadata)


@router.post("/secrets/resolve", response_model=ManagedSecretResolveResult)
def resolve_secret(
    payload: ManagedSecretResolveRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ManagedSecretResolveResult:
    settings = get_settings()
    try:
        secret_ref = payload.secret_ref
        metadata = platform_secret_service.resolve_secret_metadata(
            session=session,
            secret_ref=secret_ref,
        )
        resolved_value = platform_secret_service.get(
            session=session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return ManagedSecretResolveResult(
        secret_ref=metadata.secret_ref,
        source=metadata.source,
        resolved=bool(resolved_value),
    )


@router.delete("/secrets/{secret_ref:path}", status_code=status.HTTP_204_NO_CONTENT)
def delete_secret(
    secret_ref: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    metadata = platform_secret_service.resolve_secret_metadata(
        session=session, secret_ref=secret_ref
    )
    row = session.get(ManagedSecret, metadata.secret_ref)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed secret not found"
        )
    session.delete(row)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/tenants/{tenant_id}/secrets", response_model=list[ManagedSecretRead])
def list_tenant_secrets(
    tenant_id: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ManagedSecretRead]:
    _require_tenant_exists(session=session, tenant_id=tenant_id)
    refs = tenant_secret_service.list_secret_refs(session=session, tenant_id=tenant_id)
    return [_secret_metadata_to_schema(metadata) for metadata in refs]


@router.put(
    "/tenants/{tenant_id}/secrets/{secret_key:path}", response_model=ManagedSecretRead
)
def upsert_tenant_secret(
    tenant_id: str,
    secret_key: str,
    payload: ManagedSecretUpsert,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ManagedSecretRead:
    settings = get_settings()
    _require_tenant_exists(session=session, tenant_id=tenant_id)
    secret_ref = _build_tenant_secret_ref(tenant_id=tenant_id, secret_key=secret_key)
    try:
        metadata = tenant_secret_service.upsert_secret(
            session=session,
            secret_ref=secret_ref,
            plaintext_value=payload.value,
            encryption_key=settings.secrets_encryption_key,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return _secret_metadata_to_schema(metadata)


@router.post(
    "/tenants/{tenant_id}/secrets/resolve", response_model=ManagedSecretResolveResult
)
def resolve_tenant_secret(
    tenant_id: str,
    payload: ManagedSecretResolveRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ManagedSecretResolveResult:
    settings = get_settings()
    _require_tenant_exists(session=session, tenant_id=tenant_id)
    secret_ref = _build_tenant_secret_ref(
        tenant_id=tenant_id, secret_key=payload.secret_ref
    )
    metadata = tenant_secret_service.resolve_secret_metadata(
        session=session,
        secret_ref=secret_ref,
        tenant_id=tenant_id,
    )
    resolved_value = tenant_secret_service.resolve_secret_ref(
        session=session,
        secret_ref=secret_ref,
        encryption_key=settings.secrets_encryption_key,
        tenant_id=tenant_id,
    )
    return ManagedSecretResolveResult(
        secret_ref=secret_ref,
        source=metadata.source,
        resolved=bool(resolved_value),
    )


@router.delete(
    "/tenants/{tenant_id}/secrets/{secret_key:path}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_tenant_secret(
    tenant_id: str,
    secret_key: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    _require_tenant_exists(session=session, tenant_id=tenant_id)
    secret_ref = _build_tenant_secret_ref(tenant_id=tenant_id, secret_key=secret_key)
    tenant_secret_service.resolve_secret_metadata(
        session=session,
        secret_ref=secret_ref,
        tenant_id=tenant_id,
    )
    row = session.get(ManagedSecret, secret_ref)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Managed secret not found"
        )
    session.delete(row)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
