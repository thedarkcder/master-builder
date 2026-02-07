from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.secrets import decrypt_value, encrypt_value
from orchestrator.storage.models import ManagedSecret

_SECRET_REF_PATTERN = re.compile(r"^[A-Za-z0-9._:/-]{1,255}$")


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


def list_managed_secret_refs(session: Session) -> list[SecretRefMetadata]:
    rows = session.execute(select(ManagedSecret).order_by(ManagedSecret.secret_ref.asc())).scalars().all()
    return [
        SecretRefMetadata(
            secret_ref=row.secret_ref,
            source="managed",
            updated_at=row.updated_at,
        )
        for row in rows
    ]


def upsert_managed_secret(
    session: Session,
    *,
    secret_ref: str,
    plaintext_value: str,
    encryption_key: str,
) -> SecretRefMetadata:
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
) -> str | None:
    normalized_ref = normalize_secret_ref(secret_ref)
    row = session.get(ManagedSecret, normalized_ref)
    if row is not None:
        return decrypt_value(ciphertext=row.value_encrypted, encryption_key=encryption_key)
    return os.environ.get(normalized_ref)


def resolve_secret_ref_metadata(
    session: Session,
    *,
    secret_ref: str,
) -> SecretRefMetadata:
    normalized_ref = normalize_secret_ref(secret_ref)
    row = session.get(ManagedSecret, normalized_ref)
    if row is not None:
        return SecretRefMetadata(secret_ref=normalized_ref, source="managed", updated_at=row.updated_at)

    if os.environ.get(normalized_ref):
        return SecretRefMetadata(secret_ref=normalized_ref, source="environment", updated_at=None)
    return SecretRefMetadata(secret_ref=normalized_ref, source="missing", updated_at=None)
