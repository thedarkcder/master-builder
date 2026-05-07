from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from orchestrator.core.secret_crypto import SecretCipherEnvelope
from orchestrator.core.secret_crypto_cutover import (
    build_secret_crypto_cutover_report,
    render_secret_crypto_cutover_inventory,
)
from orchestrator.storage.models import AtlassianOAuthConnection, Base, ManagedSecret, Tenant


def _provider_payload() -> str:
    return SecretCipherEnvelope(
        version="v1",
        provider="vault_transit",
        key_ref="master-builder",
        wrapped_data_key="wrapped-key",
        nonce_b64="bm9uY2U=",
        ciphertext_b64="Y2lwaGVydGV4dA==",
    ).dumps()


def _build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(
        engine,
        tables=[
            Tenant.__table__,
            AtlassianOAuthConnection.__table__,
            ManagedSecret.__table__,
        ],
    )
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    return session_factory()


def test_build_secret_crypto_cutover_report_classifies_legacy_rows() -> None:
    now = datetime.now(UTC)
    with _build_session() as session:
        session.add_all(
            [
                ManagedSecret(
                    secret_ref="platform/GITHUB_APP_ID",
                    value_encrypted="legacy-platform-ciphertext",
                    created_at=now,
                    updated_at=now,
                ),
                ManagedSecret(
                    secret_ref="tenant/route25/JIRA_CLIENT_SECRET",
                    value_encrypted=_provider_payload(),
                    created_at=now,
                    updated_at=now,
                ),
                Tenant(
                    tenant_id="route25",
                    name="Route 25",
                    is_enabled=True,
                    archived_at=None,
                    purge_after_at=None,
                    jira_config={"connection_id": "conn-legacy"},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config=None,
                    experience_config={},
                    setup_state={},
                    created_at=now,
                    updated_at=now,
                ),
                AtlassianOAuthConnection(
                    connection_id="conn-legacy",
                    account_id="account-1",
                    account_email="owner@example.com",
                    cloud_id="cloud-1",
                    site_url="https://route25.atlassian.net",
                    scopes=["read:jira-work"],
                    access_token_encrypted="legacy-access-ciphertext",
                    refresh_token_encrypted=_provider_payload(),
                    access_token_expires_at=now,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )
        session.commit()

        report = build_secret_crypto_cutover_report(session)

    assert [item.secret_ref for item in report.legacy_managed_secret_refs] == ["platform/GITHUB_APP_ID"]
    assert [item.connection_id for item in report.atlassian_connections_requiring_relink] == ["conn-legacy"]
    assert report.atlassian_connections_requiring_relink[0].tenant_ids == ("route25",)


def test_render_secret_crypto_cutover_inventory_omits_ciphertext_values() -> None:
    now = datetime.now(UTC)
    with _build_session() as session:
        session.add(
            ManagedSecret(
                secret_ref="platform/DISCORD_BOT_TOKEN",
                value_encrypted="legacy-secret-ciphertext",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            AtlassianOAuthConnection(
                connection_id="conn-1",
                account_id="account-1",
                account_email=None,
                cloud_id="cloud-1",
                site_url="https://example.atlassian.net",
                scopes=["read:jira-work"],
                access_token_encrypted="legacy-access-token-ciphertext",
                refresh_token_encrypted="legacy-refresh-token-ciphertext",
                access_token_expires_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

        rendered = render_secret_crypto_cutover_inventory(build_secret_crypto_cutover_report(session))

    assert "platform/DISCORD_BOT_TOKEN" in rendered
    assert "conn-1" in rendered
    assert "legacy-secret-ciphertext" not in rendered
    assert "legacy-access-token-ciphertext" not in rendered
    assert "legacy-refresh-token-ciphertext" not in rendered
