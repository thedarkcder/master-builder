from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from orchestrator.core.platform.secret_manager import (
    list_managed_secret_refs,
    normalize_secret_ref,
    resolve_scoped_secret_ref as _resolve_scoped_secret_ref,
    resolve_secret_ref_metadata as _resolve_secret_ref_metadata,
    upsert_managed_secret as _upsert_managed_secret,
)


def _validate_tenant_context(secret_ref: str, tenant_id: str) -> str:
    normalized_ref = normalize_secret_ref(secret_ref)
    if normalized_ref.startswith("platform/"):
        raise ValueError("tenant secret refs must not include platform/* prefix")

    if normalized_ref.startswith("tenant/"):
        _, *parts = normalized_ref.split("/", 2)
        if not parts or parts[0] != tenant_id:
            raise ValueError("tenant secret refs must target the active tenant")
        return normalized_ref

    if normalized_ref.startswith("project/"):
        parts = normalized_ref.split("/", 3)
        if len(parts) != 4 or parts[1] != tenant_id:
            raise ValueError("tenant secret refs must target the active tenant")
        return normalized_ref

    return f"tenant/{tenant_id}/{normalized_ref}"


@dataclass(frozen=True)
class TenantSecretService:
    """Service for tenant-scoped secret operations."""

    def resolve_secret_ref(
        self,
        *,
        session: Session,
        secret_ref: str,
        encryption_key: str,
        tenant_id: str,
        project_id: str | None = None,
    ) -> str | None:
        normalized_ref = _validate_tenant_context(
            secret_ref=secret_ref, tenant_id=tenant_id
        )
        return _resolve_scoped_secret_ref(
            session,
            secret_ref=normalized_ref,
            encryption_key=encryption_key,
            tenant_id=tenant_id,
            project_id=project_id,
        )

    def resolve_secret_metadata(
        self,
        *,
        session: Session,
        secret_ref: str,
        tenant_id: str,
    ):
        normalized_ref = _validate_tenant_context(
            secret_ref=secret_ref, tenant_id=tenant_id
        )
        return _resolve_secret_ref_metadata(
            session,
            secret_ref=normalized_ref,
            scope="tenant",
            tenant_id=tenant_id,
        )

    def list_secret_refs(
        self,
        *,
        session: Session,
        tenant_id: str,
    ) -> list:
        return list_managed_secret_refs(session, scope="tenant", tenant_id=tenant_id)

    def upsert_secret(
        self,
        *,
        session: Session,
        secret_ref: str,
        plaintext_value: str,
        encryption_key: str,
        tenant_id: str,
    ):
        normalized_ref = _validate_tenant_context(
            secret_ref=secret_ref, tenant_id=tenant_id
        )
        return _upsert_managed_secret(
            session,
            secret_ref=normalized_ref,
            plaintext_value=plaintext_value,
            encryption_key=encryption_key,
            scope="tenant",
            tenant_id=tenant_id,
        )


tenant_secret_service = TenantSecretService()


def resolve_scoped_secret_ref(
    session: Session,
    *,
    secret_ref: str,
    encryption_key: str,
    tenant_id: str,
    project_id: str | None = None,
) -> str | None:
    return tenant_secret_service.resolve_secret_ref(
        session=session,
        secret_ref=secret_ref,
        encryption_key=encryption_key,
        tenant_id=tenant_id,
        project_id=project_id,
    )
