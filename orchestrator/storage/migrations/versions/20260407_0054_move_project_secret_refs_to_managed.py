"""move all project secret refs to project-managed secrets

Revision ID: 20260407_0054
Revises: 20260407_0053
Create Date: 2026-04-07 21:20:00.000000
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from alembic import op
from sqlalchemy import text

from orchestrator.core.config import get_settings
from orchestrator.core.platform.secret_manager import normalize_secret_ref
from orchestrator.core.platform.secrets import decrypt_value, encrypt_value


revision = "20260407_0054"
down_revision = "20260407_0053"
branch_labels = None
depends_on = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _loads_json(value: object) -> dict[str, str]:
    if isinstance(value, dict):
        return {
            str(key or "").strip(): str(item or "").strip()
            for key, item in value.items()
            if str(key or "").strip() and str(item or "").strip()
        }
    if isinstance(value, str):
        text_value = value.strip()
        if not text_value:
            return {}
        loaded = json.loads(text_value)
        if isinstance(loaded, dict):
            return {
                str(key or "").strip(): str(item or "").strip()
                for key, item in loaded.items()
                if str(key or "").strip() and str(item or "").strip()
            }
    return {}


def _dumps_json(value: dict[str, str]) -> str:
    return json.dumps(value, sort_keys=True)


def _project_secret_ref(*, tenant_id: str, project_id: str, secret_key: str) -> str:
    normalized_key = normalize_secret_ref(secret_key)
    if normalized_key.startswith(("platform/", "tenant/", "project/")):
        raise ValueError(
            "Project secret variable names must not include a scope prefix"
        )
    return f"project/{tenant_id}/{project_id}/{normalized_key}"


def _load_plaintext_secret(
    *,
    bind,
    secret_ref: str,
    tenant_id: str,
    project_id: str,
    encryption_key: str,
) -> str | None:
    normalized_ref = normalize_secret_ref(secret_ref)
    candidates: list[str] = []

    def _append(candidate: str) -> None:
        if candidate not in candidates:
            candidates.append(candidate)

    if normalized_ref.startswith(("platform/", "tenant/", "project/")):
        _append(normalized_ref)
    else:
        _append(f"project/{tenant_id}/{project_id}/{normalized_ref}")
        _append(f"tenant/{tenant_id}/{normalized_ref}")

    for candidate in candidates:
        row = (
            bind.execute(
                text(
                    """
                SELECT value_encrypted
                FROM managed_secrets
                WHERE secret_ref = :secret_ref
                """
                ),
                {"secret_ref": candidate},
            )
            .mappings()
            .first()
        )
        if row is None:
            continue
        encrypted_value = str(row.get("value_encrypted") or "").strip()
        if not encrypted_value:
            continue
        return decrypt_value(ciphertext=encrypted_value, encryption_key=encryption_key)
    return None


def _upsert_project_managed_secret(
    *,
    bind,
    secret_ref: str,
    plaintext_value: str,
    encryption_key: str,
    now: datetime,
) -> None:
    encrypted_value = encrypt_value(
        plaintext=plaintext_value, encryption_key=encryption_key
    )
    existing = bind.execute(
        text(
            """
            SELECT secret_ref
            FROM managed_secrets
            WHERE secret_ref = :secret_ref
            """
        ),
        {"secret_ref": secret_ref},
    ).scalar_one_or_none()
    if existing is None:
        bind.execute(
            text(
                """
                INSERT INTO managed_secrets (secret_ref, value_encrypted, created_at, updated_at)
                VALUES (:secret_ref, :value_encrypted, :created_at, :updated_at)
                """
            ),
            {
                "secret_ref": secret_ref,
                "value_encrypted": encrypted_value,
                "created_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        )
        return

    bind.execute(
        text(
            """
            UPDATE managed_secrets
            SET value_encrypted = :value_encrypted, updated_at = :updated_at
            WHERE secret_ref = :secret_ref
            """
        ),
        {
            "secret_ref": secret_ref,
            "value_encrypted": encrypted_value,
            "updated_at": now.isoformat(),
        },
    )


def upgrade() -> None:
    bind = op.get_bind()
    settings = get_settings()
    encryption_key = str(getattr(settings, "secrets_encryption_key", "") or "").strip()
    rows = (
        bind.execute(
            text(
                """
            SELECT project_id, tenant_id, secret_refs
            FROM projects
            """
            )
        )
        .mappings()
        .all()
    )

    for row in rows:
        project_id = str(row.get("project_id") or "").strip()
        tenant_id = str(row.get("tenant_id") or "").strip()
        current_secret_refs = _loads_json(row.get("secret_refs"))
        if not project_id or not tenant_id or not current_secret_refs:
            continue

        next_secret_refs: dict[str, str] = {}
        changed = False
        for secret_key, raw_value in current_secret_refs.items():
            managed_ref = _project_secret_ref(
                tenant_id=tenant_id,
                project_id=project_id,
                secret_key=secret_key,
            )
            candidate = str(raw_value or "").strip()
            if candidate == managed_ref:
                next_secret_refs[secret_key] = managed_ref
                continue

            plaintext_value: str | None = None
            normalized_candidate: str | None
            try:
                normalized_candidate = normalize_secret_ref(candidate)
            except ValueError:
                normalized_candidate = None

            if normalized_candidate is not None:
                plaintext_value = _load_plaintext_secret(
                    bind=bind,
                    secret_ref=normalized_candidate,
                    tenant_id=tenant_id,
                    project_id=project_id,
                    encryption_key=encryption_key,
                )
                if plaintext_value is None and normalized_candidate.startswith(
                    ("platform/", "tenant/", "project/")
                ):
                    raise ValueError(
                        f"Project secret '{secret_key}' references missing secret '{normalized_candidate}'"
                    )

            resolved_plaintext = (
                plaintext_value if plaintext_value is not None else candidate
            )
            _upsert_project_managed_secret(
                bind=bind,
                secret_ref=managed_ref,
                plaintext_value=resolved_plaintext,
                encryption_key=encryption_key,
                now=_now(),
            )
            next_secret_refs[secret_key] = managed_ref
            changed = True

        if changed:
            bind.execute(
                text(
                    """
                    UPDATE projects
                    SET secret_refs = :secret_refs, updated_at = :updated_at
                    WHERE project_id = :project_id
                    """
                ),
                {
                    "project_id": project_id,
                    "secret_refs": _dumps_json(next_secret_refs),
                    "updated_at": _now().isoformat(),
                },
            )


def downgrade() -> None:
    return None
