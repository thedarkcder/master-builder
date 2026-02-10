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
from orchestrator.core.config import get_settings
from orchestrator.core.secret_manager import (
    list_managed_secret_refs,
    normalize_secret_ref,
    resolve_secret_ref,
    resolve_secret_ref_metadata,
    upsert_managed_secret,
)
from orchestrator.core.security import require_admin
from orchestrator.storage.models import ManagedSecret

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _secret_metadata_to_schema(metadata) -> ManagedSecretRead:  # noqa: ANN001
    return ManagedSecretRead(
        secret_ref=metadata.secret_ref,
        source=metadata.source,
        updated_at=metadata.updated_at,
    )


@router.get("/secrets", response_model=list[ManagedSecretRead])
def list_secrets(
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> list[ManagedSecretRead]:
    refs = list_managed_secret_refs(session)
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
        metadata = upsert_managed_secret(
            session,
            secret_ref=secret_ref,
            plaintext_value=payload.value,
            encryption_key=settings.secrets_encryption_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return _secret_metadata_to_schema(metadata)


@router.post("/secrets/resolve", response_model=ManagedSecretResolveResult)
def resolve_secret(
    payload: ManagedSecretResolveRequest,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> ManagedSecretResolveResult:
    settings = get_settings()
    try:
        secret_ref = normalize_secret_ref(payload.secret_ref)
        metadata = resolve_secret_ref_metadata(session, secret_ref=secret_ref)
        resolved_value = resolve_secret_ref(
            session,
            secret_ref=secret_ref,
            encryption_key=settings.secrets_encryption_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return ManagedSecretResolveResult(
        secret_ref=secret_ref,
        source=metadata.source,
        resolved=bool(resolved_value),
    )


@router.delete("/secrets/{secret_ref:path}", status_code=status.HTTP_204_NO_CONTENT)
def delete_secret(
    secret_ref: str,
    _: str = Depends(require_admin),
    session: Session = Depends(get_session),
) -> Response:
    normalized_ref = normalize_secret_ref(secret_ref)
    row = session.get(ManagedSecret, normalized_ref)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Managed secret not found")
    session.delete(row)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
