from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from orchestrator.core.secret_crypto import SecretCipherEnvelope
from orchestrator.storage.models import AtlassianOAuthConnection, ManagedSecret, Tenant


def _is_provider_ciphertext(value: str | None) -> bool:
    normalized = str(value or "").strip()
    if not normalized:
        return False
    try:
        SecretCipherEnvelope.loads(normalized)
    except Exception:  # noqa: BLE001
        return False
    return True


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True)
class ManagedSecretCutoverInventoryItem:
    secret_ref: str
    provider_encrypted: bool
    updated_at: datetime | None


@dataclass(frozen=True)
class AtlassianOAuthCutoverInventoryItem:
    connection_id: str
    site_url: str
    cloud_id: str
    tenant_ids: tuple[str, ...]
    account_email: str | None
    access_token_provider_encrypted: bool
    refresh_token_provider_encrypted: bool
    updated_at: datetime | None

    @property
    def requires_relink(self) -> bool:
        return not self.access_token_provider_encrypted or not self.refresh_token_provider_encrypted


@dataclass(frozen=True)
class SecretCryptoCutoverReport:
    managed_secrets: tuple[ManagedSecretCutoverInventoryItem, ...]
    atlassian_oauth_connections: tuple[AtlassianOAuthCutoverInventoryItem, ...]

    @property
    def legacy_managed_secret_refs(self) -> tuple[ManagedSecretCutoverInventoryItem, ...]:
        return tuple(item for item in self.managed_secrets if not item.provider_encrypted)

    @property
    def atlassian_connections_requiring_relink(self) -> tuple[AtlassianOAuthCutoverInventoryItem, ...]:
        return tuple(item for item in self.atlassian_oauth_connections if item.requires_relink)


def build_secret_crypto_cutover_report(session: Session) -> SecretCryptoCutoverReport:
    managed_secret_rows = session.execute(select(ManagedSecret).order_by(ManagedSecret.secret_ref.asc())).scalars().all()
    tenant_rows = session.execute(select(Tenant).order_by(Tenant.tenant_id.asc())).scalars().all()
    connection_rows = session.execute(
        select(AtlassianOAuthConnection).order_by(AtlassianOAuthConnection.connection_id.asc())
    ).scalars().all()

    tenant_ids_by_connection: dict[str, list[str]] = {}
    for tenant in tenant_rows:
        connection_id = str(((tenant.jira_config or {}) if isinstance(tenant.jira_config, dict) else {}).get("connection_id") or "").strip()
        if not connection_id:
            continue
        tenant_ids_by_connection.setdefault(connection_id, []).append(tenant.tenant_id)

    return SecretCryptoCutoverReport(
        managed_secrets=tuple(
            ManagedSecretCutoverInventoryItem(
                secret_ref=row.secret_ref,
                provider_encrypted=_is_provider_ciphertext(row.value_encrypted),
                updated_at=row.updated_at,
            )
            for row in managed_secret_rows
        ),
        atlassian_oauth_connections=tuple(
            AtlassianOAuthCutoverInventoryItem(
                connection_id=row.connection_id,
                site_url=row.site_url,
                cloud_id=row.cloud_id,
                tenant_ids=tuple(sorted(tenant_ids_by_connection.get(row.connection_id, []))),
                account_email=row.account_email,
                access_token_provider_encrypted=_is_provider_ciphertext(row.access_token_encrypted),
                refresh_token_provider_encrypted=_is_provider_ciphertext(row.refresh_token_encrypted),
                updated_at=row.updated_at,
            )
            for row in connection_rows
        ),
    )


def render_secret_crypto_cutover_inventory(report: SecretCryptoCutoverReport) -> str:
    lines = [
        "# Secret Crypto Cutover Inventory",
        "",
        "Generated from database metadata only. This report never includes secret plaintext or ciphertext.",
        "",
        "## Summary",
        f"- Managed secret refs in database: {len(report.managed_secrets)}",
        f"- Managed secret refs still using legacy DB ciphertext: {len(report.legacy_managed_secret_refs)}",
        f"- Atlassian OAuth connections in database: {len(report.atlassian_oauth_connections)}",
        f"- Atlassian OAuth connections requiring relink after cutover: {len(report.atlassian_connections_requiring_relink)}",
        "",
        "## Managed Secret Refs",
    ]
    if not report.managed_secrets:
        lines.append("- None")
    else:
        for item in report.managed_secrets:
            storage = "provider-encrypted" if item.provider_encrypted else "legacy-db-ciphertext"
            lines.append(
                f"- `{item.secret_ref}` | storage={storage} | updated_at={_format_timestamp(item.updated_at)}"
            )
    lines.extend(["", "## Atlassian OAuth Connections"])
    if not report.atlassian_oauth_connections:
        lines.append("- None")
    else:
        for item in report.atlassian_oauth_connections:
            tenant_list = ", ".join(item.tenant_ids) if item.tenant_ids else "-"
            lines.append(
                "- "
                f"`{item.connection_id}` | site={item.site_url} | cloud_id={item.cloud_id} | "
                f"tenants={tenant_list} | account_email={item.account_email or '-'} | "
                f"access_token={'provider-encrypted' if item.access_token_provider_encrypted else 'legacy-db-ciphertext'} | "
                f"refresh_token={'provider-encrypted' if item.refresh_token_provider_encrypted else 'legacy-db-ciphertext'} | "
                f"updated_at={_format_timestamp(item.updated_at)}"
            )
    lines.extend(
        [
            "",
            "## Cutover Actions",
            "- Re-add every managed secret ref that still uses `legacy-db-ciphertext` after enabling provider-backed secret crypto.",
            "- Re-link every Atlassian OAuth connection whose access or refresh token still uses `legacy-db-ciphertext`.",
        ]
    )
    return "\n".join(lines) + "\n"
