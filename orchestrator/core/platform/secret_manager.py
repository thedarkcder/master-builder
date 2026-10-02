from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.platform.secrets import decrypt_value, encrypt_value
from orchestrator.storage.models import ManagedSecret
from orchestrator.storage.tenant_rls import (
    set_platform_system_rls_context,
    set_tenant_system_rls_context,
)

_SECRET_REF_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,255}$")
_SECRET_SCOPE_ALL = "all"
_SECRET_SCOPE_PLATFORM = "platform"
_SECRET_SCOPE_TENANT = "tenant"
_VALID_SECRET_SCOPES = {
    _SECRET_SCOPE_ALL,
    _SECRET_SCOPE_PLATFORM,
    _SECRET_SCOPE_TENANT,
}


@dataclass(frozen=True)
class SecretRefMetadata:
    secret_ref: str
    source: str
    updated_at: datetime | None


def normalize_secret_ref(secret_ref: str) -> str:
    normalized = secret_ref.strip()
    if not normalized:
        raise ValueError("secret_ref is required")
    if not _SECRET_REF_PATTERN.fullmatch(normalized):
        raise ValueError("secret_ref contains unsupported characters")
    return normalized


def list_managed_secret_refs(
    session: Session,
    *,
    scope: str = _SECRET_SCOPE_ALL,
    tenant_id: str | None = None,
) -> list[SecretRefMetadata]:
    _apply_secret_scope_context(session=session, scope=scope, tenant_id=tenant_id)
    rows = (
        session.execute(select(ManagedSecret).order_by(ManagedSecret.secret_ref.asc()))
        .scalars()
        .all()
    )
    metadata = [
        SecretRefMetadata(
            secret_ref=row.secret_ref,
            source="managed",
            updated_at=row.updated_at,
        )
        for row in rows
    ]
    if scope == _SECRET_SCOPE_PLATFORM:
        return [item for item in metadata if item.secret_ref.startswith("platform/")]
    if scope == _SECRET_SCOPE_TENANT:
        normalized_tenant_id = (tenant_id or "").strip()
        prefix = f"tenant/{normalized_tenant_id}/"
        return [item for item in metadata if item.secret_ref.startswith(prefix)]
    return metadata


def upsert_managed_secret(
    session: Session,
    *,
    secret_ref: str,
    plaintext_value: str,
    encryption_key: str,
    scope: str = _SECRET_SCOPE_ALL,
    tenant_id: str | None = None,
) -> SecretRefMetadata:
    _apply_secret_scope_context(session=session, scope=scope, tenant_id=tenant_id)
    normalized_ref = normalize_secret_ref(secret_ref)
    normalized_value = plaintext_value.strip()
    if not normalized_value:
        raise ValueError("Secret value is required")

    now = datetime.now(timezone.utc)
    encrypted_value = encrypt_value(
        plaintext=normalized_value, encryption_key=encryption_key
    )
    row = session.get(ManagedSecret, normalized_ref)
    if row is None:
        row = ManagedSecret(
            secret_ref=normalized_ref,
            value_encrypted=encrypted_value,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
    else:
        row.value_encrypted = encrypted_value
        row.updated_at = now
    session.commit()
    return SecretRefMetadata(
        secret_ref=normalized_ref, source="managed", updated_at=row.updated_at
    )


def resolve_secret_ref(
    session: Session,
    *,
    secret_ref: str,
    encryption_key: str,
    scope: str = _SECRET_SCOPE_ALL,
    tenant_id: str | None = None,
    allow_environment_fallback: bool = False,
) -> str | None:
    _apply_secret_scope_context(session=session, scope=scope, tenant_id=tenant_id)
    normalized_ref = normalize_secret_ref(secret_ref)
    row = session.get(ManagedSecret, normalized_ref)
    if row is not None:
        return decrypt_value(
            ciphertext=row.value_encrypted, encryption_key=encryption_key
        )
    if allow_environment_fallback:
        return os.environ.get(normalized_ref)
    return None


def scoped_secret_ref_candidates(
    *,
    secret_ref: str,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> list[str]:
    normalized_ref = normalize_secret_ref(secret_ref)
    candidates: list[str] = []

    def _append(value: str) -> None:
        if value not in candidates:
            candidates.append(value)

    normalized_tenant_id = (
        tenant_id.strip() if tenant_id and tenant_id.strip() else None
    )
    normalized_project_id = (
        project_id.strip() if project_id and project_id.strip() else None
    )

    if normalized_tenant_id and normalized_project_id:
        _append(
            f"project/{normalized_tenant_id}/{normalized_project_id}/{normalized_ref}"
        )
    if normalized_tenant_id:
        _append(f"tenant/{normalized_tenant_id}/{normalized_ref}")
    elif normalized_tenant_id is None:
        _append(f"platform/{normalized_ref}")
        _append(normalized_ref)
    return candidates


def resolve_scoped_secret_ref(
    session: Session,
    *,
    secret_ref: str,
    encryption_key: str,
    tenant_id: str | None = None,
    project_id: str | None = None,
) -> str | None:
    normalized_ref = normalize_secret_ref(secret_ref)
    normalized_tenant_id = (
        tenant_id.strip() if tenant_id and tenant_id.strip() else None
    )
    normalized_project_id = (
        project_id.strip() if project_id and project_id.strip() else None
    )

    candidates: list[str] = []
    scopes: list[str] = []

    def _append(candidate: str, scope: str) -> None:
        if candidate not in candidates:
            candidates.append(candidate)
            scopes.append(scope)

    if normalized_ref.startswith("project/"):
        _append(normalized_ref, _SECRET_SCOPE_ALL)
        parts = normalized_ref.split("/", 3)
        if len(parts) == 4:
            candidate_tenant_id, secret_suffix = parts[1], parts[3]
            if candidate_tenant_id and secret_suffix:
                _append(
                    f"tenant/{candidate_tenant_id}/{secret_suffix}",
                    _SECRET_SCOPE_TENANT,
                )
    elif normalized_ref.startswith("tenant/"):
        _append(normalized_ref, _SECRET_SCOPE_TENANT)
    elif normalized_ref.startswith("platform/"):
        _append(normalized_ref, _SECRET_SCOPE_PLATFORM)
    elif normalized_tenant_id and normalized_project_id:
        _append(
            f"project/{normalized_tenant_id}/{normalized_project_id}/{normalized_ref}",
            _SECRET_SCOPE_ALL,
        )
        _append(f"tenant/{normalized_tenant_id}/{normalized_ref}", _SECRET_SCOPE_TENANT)
    elif normalized_tenant_id:
        _append(f"tenant/{normalized_tenant_id}/{normalized_ref}", _SECRET_SCOPE_TENANT)
    else:
        return None

    for candidate, scope in zip(candidates, scopes, strict=False):
        value = resolve_secret_ref(
            session,
            secret_ref=candidate,
            encryption_key=encryption_key,
            scope=scope,
            tenant_id=normalized_tenant_id if scope == _SECRET_SCOPE_TENANT else None,
            allow_environment_fallback=False,
        )
        if value:
            return value
    return None


def resolve_platform_secret_ref(
    session: Session,
    *,
    secret_ref: str,
    encryption_key: str,
    allow_environment_fallback: bool = False,
) -> str | None:
    normalized_ref = normalize_secret_ref(secret_ref)
    if not normalized_ref.startswith("platform/"):
        raise ValueError("Platform secret refs must use platform/* prefix")
    if normalized_ref == "platform/":
        raise ValueError("Platform secret ref is required")

    return resolve_secret_ref(
        session,
        secret_ref=normalized_ref,
        encryption_key=encryption_key,
        scope=_SECRET_SCOPE_PLATFORM,
        allow_environment_fallback=allow_environment_fallback,
    )


def resolve_tenant_secret_ref(
    session: Session,
    *,
    secret_ref: str,
    encryption_key: str,
    tenant_id: str,
    allow_environment_fallback: bool = False,
) -> str | None:
    normalized_ref = normalize_secret_ref(secret_ref)
    normalized_tenant_id = (
        tenant_id.strip() if tenant_id and tenant_id.strip() else None
    )
    if normalized_tenant_id is None:
        raise ValueError("tenant_id is required for tenant secret resolution")

    if normalized_ref.startswith("platform/"):
        raise ValueError("Tenant secret refs must not include platform/* prefix")
    if normalized_ref.startswith("tenant/"):
        return resolve_secret_ref(
            session,
            secret_ref=normalized_ref,
            encryption_key=encryption_key,
            scope=_SECRET_SCOPE_TENANT,
            tenant_id=normalized_tenant_id,
            allow_environment_fallback=allow_environment_fallback,
        )
    if normalized_ref.startswith("project/"):
        raise ValueError("Tenant secret refs must not include project/* prefix")

    return resolve_secret_ref(
        session,
        secret_ref=f"tenant/{normalized_tenant_id}/{normalized_ref}",
        encryption_key=encryption_key,
        scope=_SECRET_SCOPE_TENANT,
        tenant_id=normalized_tenant_id,
        allow_environment_fallback=allow_environment_fallback,
    )


def resolve_secret_ref_metadata(
    session: Session,
    *,
    secret_ref: str,
    scope: str = _SECRET_SCOPE_ALL,
    tenant_id: str | None = None,
) -> SecretRefMetadata:
    _apply_secret_scope_context(session=session, scope=scope, tenant_id=tenant_id)
    normalized_ref = normalize_secret_ref(secret_ref)
    row = session.get(ManagedSecret, normalized_ref)
    if row is not None:
        return SecretRefMetadata(
            secret_ref=normalized_ref, source="managed", updated_at=row.updated_at
        )

    if os.environ.get(normalized_ref):
        return SecretRefMetadata(
            secret_ref=normalized_ref, source="environment", updated_at=None
        )
    return SecretRefMetadata(
        secret_ref=normalized_ref, source="missing", updated_at=None
    )


def _apply_secret_scope_context(
    *,
    session: Session,
    scope: str,
    tenant_id: str | None = None,
) -> None:
    if scope not in _VALID_SECRET_SCOPES:
        raise ValueError(f"Unsupported secret scope: {scope}")

    if scope == _SECRET_SCOPE_TENANT:
        normalized_tenant_id = (tenant_id or "").strip()
        if not normalized_tenant_id:
            raise ValueError("tenant_id is required for tenant secret scope")
    else:
        normalized_tenant_id = ""

    if scope == _SECRET_SCOPE_TENANT:
        set_tenant_system_rls_context(
            session,
            tenant_id=normalized_tenant_id,
            system_purpose="managed_secret_access",
        )
    else:
        set_platform_system_rls_context(session, system_purpose="managed_secret_access")
