from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.storage.models import ManagedSecret

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
    rows = session.execute(select(ManagedSecret).order_by(ManagedSecret.secret_ref.asc())).scalars().all()
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
    encrypted_value = encrypt_value(plaintext=normalized_value, encryption_key=encryption_key)
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
    return SecretRefMetadata(secret_ref=normalized_ref, source="managed", updated_at=row.updated_at)


def resolve_secret_ref(
    session: Session,
    *,
    secret_ref: str,
    encryption_key: str,
    scope: str = _SECRET_SCOPE_ALL,
    tenant_id: str | None = None,
) -> str | None:
    _apply_secret_scope_context(session=session, scope=scope, tenant_id=tenant_id)
    normalized_ref = normalize_secret_ref(secret_ref)
    row = session.get(ManagedSecret, normalized_ref)
    if row is not None:
        return decrypt_value(ciphertext=row.value_encrypted, encryption_key=encryption_key)
    return os.environ.get(normalized_ref)


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

    normalized_tenant_id = tenant_id.strip() if tenant_id and tenant_id.strip() else None
    normalized_project_id = project_id.strip() if project_id and project_id.strip() else None

    if normalized_tenant_id and normalized_project_id:
        _append(f"project/{normalized_tenant_id}/{normalized_project_id}/{normalized_ref}")
    if normalized_tenant_id:
        _append(f"tenant/{normalized_tenant_id}/{normalized_ref}")
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
    for candidate in scoped_secret_ref_candidates(
        secret_ref=secret_ref,
        tenant_id=tenant_id,
        project_id=project_id,
    ):
        value = resolve_secret_ref(
            session,
            secret_ref=candidate,
            encryption_key=encryption_key,
        )
        if value:
            return value
    return None


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
        return SecretRefMetadata(secret_ref=normalized_ref, source="managed", updated_at=row.updated_at)

    if os.environ.get(normalized_ref):
        return SecretRefMetadata(secret_ref=normalized_ref, source="environment", updated_at=None)
    return SecretRefMetadata(secret_ref=normalized_ref, source="missing", updated_at=None)


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

    if session.bind is None or session.bind.dialect.name != "postgresql":
        return

    session.execute(
        text("SELECT set_config('app.secret_scope', :scope, true)"),
        {"scope": scope},
    )
    session.execute(
        text("SELECT set_config('app.secret_tenant_id', :tenant_id, true)"),
        {"tenant_id": normalized_tenant_id},
    )
