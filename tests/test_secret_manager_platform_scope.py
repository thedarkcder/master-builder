import os
import pytest
from unittest.mock import MagicMock, patch

from orchestrator.core.platform_secret_service import (
    platform_secret_service,
)
from orchestrator.core.secret_manager import (
    resolve_platform_secret_ref,
    resolve_tenant_secret_ref,
    resolve_scoped_secret_ref,
    resolve_secret_ref,
    resolve_secret_ref_metadata,
)
from orchestrator.core.tenant_secret_service import tenant_secret_service


def test_resolve_secret_ref_platform_scope_falls_back_to_environment() -> None:
    session = MagicMock()
    session.get.return_value = None
    session.bind = None
    os.environ["DISCORD_INTERACTIONS_PUBLIC_KEY"] = "env-public-key"
    try:
        resolved = resolve_secret_ref(
            session,
            secret_ref="DISCORD_INTERACTIONS_PUBLIC_KEY",
            encryption_key="unused",
            scope="platform",
        )
    finally:
        os.environ.pop("DISCORD_INTERACTIONS_PUBLIC_KEY", None)
    assert resolved == "env-public-key"


def test_resolve_scoped_secret_ref_without_tenant_or_project_falls_back_to_environment() -> None:
    session = MagicMock()
    session.get.return_value = None
    session.bind = None
    os.environ["DISCORD_INTERACTIONS_PUBLIC_KEY"] = "env-public-key"
    os.environ["platform/DISCORD_INTERACTIONS_PUBLIC_KEY"] = "env-platform-public-key"
    try:
        resolved = resolve_scoped_secret_ref(
            session,
            secret_ref="DISCORD_INTERACTIONS_PUBLIC_KEY",
            encryption_key="unused",
        )
    finally:
        os.environ.pop("DISCORD_INTERACTIONS_PUBLIC_KEY", None)
        os.environ.pop("platform/DISCORD_INTERACTIONS_PUBLIC_KEY", None)
    assert resolved == "env-public-key"


def test_resolve_secret_ref_metadata_platform_scope_reports_environment_when_available() -> None:
    session = MagicMock()
    session.get.return_value = None
    session.bind = None
    os.environ["platform/DISCORD_INTERACTIONS_PUBLIC_KEY"] = "env-platform-public-key"
    try:
        metadata = resolve_secret_ref_metadata(
            session,
            secret_ref="platform/DISCORD_INTERACTIONS_PUBLIC_KEY",
            scope="platform",
        )
    finally:
        os.environ.pop("platform/DISCORD_INTERACTIONS_PUBLIC_KEY", None)
    assert metadata.source == "environment"


def test_resolve_scoped_secret_ref_project_falls_back_to_tenant_only() -> None:
    session = MagicMock()
    session.bind = None

    def _session_get(_cls, key):  # noqa: ANN001
        if key == "project/t1/p1/SECRET_NAME":
            return None
        if key == "tenant/t1/SECRET_NAME":
            return MagicMock(value_encrypted="enc")
        if key == "platform/SECRET_NAME":
            raise AssertionError("platform fallback should not be queried")
        return None

    session.get.side_effect = _session_get

    from orchestrator.core import secret_manager as module

    original_decrypt = module.decrypt_value
    module.decrypt_value = lambda ciphertext, encryption_key: "tenant-secret"  # type: ignore[assignment]
    try:
        resolved = resolve_scoped_secret_ref(
            session,
            secret_ref="SECRET_NAME",
            encryption_key="unused",
            tenant_id="t1",
            project_id="p1",
        )
    finally:
        module.decrypt_value = original_decrypt  # type: ignore[assignment]

    assert resolved == "tenant-secret"


def test_resolve_scoped_secret_ref_tenant_does_not_fallback_to_platform_or_env() -> None:
    session = MagicMock()
    session.bind = None
    session.get.return_value = None
    os.environ["SECRET_NAME"] = "global-env-secret"
    os.environ["tenant/t1/SECRET_NAME"] = "tenant-env-secret"
    os.environ["platform/SECRET_NAME"] = "platform-env-secret"
    try:
        resolved = resolve_scoped_secret_ref(
            session,
            secret_ref="SECRET_NAME",
            encryption_key="unused",
            tenant_id="t1",
        )
    finally:
        os.environ.pop("SECRET_NAME", None)
        os.environ.pop("tenant/t1/SECRET_NAME", None)
        os.environ.pop("platform/SECRET_NAME", None)
    assert resolved is None


def test_resolve_platform_secret_ref_rejects_unscoped_ref() -> None:
    session = MagicMock()
    def _session_get(_cls, key):  # noqa: ANN001
        if key == "platform/SECRET_NAME":
            return MagicMock(value_encrypted="enc")
        return None

    session.get.side_effect = _session_get

    import orchestrator.core.secret_manager as module

    original_decrypt = module.decrypt_value
    module.decrypt_value = lambda ciphertext, encryption_key, **_: "managed"  # type: ignore[assignment]
    try:
        with pytest.raises(ValueError, match="Platform secret refs must use platform"):
            resolve_platform_secret_ref(
                session,
                secret_ref="SECRET_NAME",
                encryption_key="unused",
            )
        assert (
            resolve_platform_secret_ref(
                session,
                secret_ref="platform/SECRET_NAME",
                encryption_key="unused",
            )
            == "managed"
        )
    finally:
        module.decrypt_value = original_decrypt  # type: ignore[assignment]


def test_resolve_platform_secret_ref_rejects_invalid_scopes() -> None:
    session = MagicMock()
    session.bind = None
    with (
        pytest.raises(ValueError, match="Platform secret refs must use platform"),
        patch.object(session, "get", side_effect=lambda _cls, key: None),
    ):
        resolve_platform_secret_ref(session, secret_ref="tenant/tenant-a/foo", encryption_key="unused")

    with (
        pytest.raises(ValueError, match="Platform secret refs must use platform"),
        patch.object(session, "get", side_effect=lambda _cls, key: None),
    ):
        resolve_platform_secret_ref(session, secret_ref="project/t1/p1/foo", encryption_key="unused")


def test_platform_secret_service_get_supports_platform_refs() -> None:
    with patch("orchestrator.core.platform_secret_service._resolve_platform_secret_ref") as resolve_mock:
        resolve_mock.side_effect = [None, "platform-value"]
        service = platform_secret_service
        assert service.get(session=MagicMock(), secret_ref="DISCORD_BOT_TOKEN", encryption_key="unused") is None
        assert resolve_mock.call_args_list[0][1]["secret_ref"] == "platform/DISCORD_BOT_TOKEN"

        assert (
            service.get(session=MagicMock(), secret_ref="platform/DISCORD_BOT_TOKEN", encryption_key="unused")
            == "platform-value"
        )
        assert resolve_mock.call_args_list[1][1]["secret_ref"] == "platform/DISCORD_BOT_TOKEN"


def test_platform_secret_service_falls_back_to_platform_env_names() -> None:
    os.environ["platform/JIRA_OAUTH_CLIENT_SECRET"] = "env-platform-secret"
    try:
        with patch("orchestrator.core.platform_secret_service._resolve_platform_secret_ref", return_value=None):
            assert (
                platform_secret_service.get(
                    session=MagicMock(),
                    secret_ref="JIRA_OAUTH_CLIENT_SECRET",
                    encryption_key="unused",
                )
                == "env-platform-secret"
            )
    finally:
        os.environ.pop("platform/JIRA_OAUTH_CLIENT_SECRET", None)


def test_platform_secret_service_does_not_fall_back_to_unscoped_env_names() -> None:
    os.environ["JIRA_OAUTH_CLIENT_SECRET"] = "env-plain-secret"
    try:
        with patch("orchestrator.core.platform_secret_service._resolve_platform_secret_ref", return_value=None):
            assert (
                platform_secret_service.get(
                    session=MagicMock(),
                    secret_ref="JIRA_OAUTH_CLIENT_SECRET",
                    encryption_key="unused",
                )
                is None
            )
    finally:
        os.environ.pop("JIRA_OAUTH_CLIENT_SECRET", None)


def test_platform_secret_service_rejects_scoped_secret_refs() -> None:
    service = platform_secret_service
    with pytest.raises(ValueError, match="Platform secrets must use platform"):
        service.get(session=MagicMock(), secret_ref="tenant/DISCORD_BOT_TOKEN", encryption_key="unused")
    with pytest.raises(ValueError, match="Platform secrets must use platform"):
        service.get(session=MagicMock(), secret_ref="project/example/DISCORD_BOT_TOKEN", encryption_key="unused")


def test_platform_secret_service_rejects_empty_ref() -> None:
    with pytest.raises(ValueError, match="Platform secret reference is required"):
        platform_secret_service.get(session=MagicMock(), secret_ref="platform/", encryption_key="unused")


def test_tenant_secret_service_rejects_platform_refs() -> None:
    with pytest.raises(ValueError, match="must not include platform"):
        tenant_secret_service.resolve_secret_ref(
            session=MagicMock(),
            secret_ref="platform/DISCORD_BOT_TOKEN",
            encryption_key="unused",
            tenant_id="tenant-a",
        )


def test_resolve_tenant_secret_ref_builds_tenant_ref() -> None:
    session = MagicMock()

    def _session_get(_cls, key):  # noqa: ANN001
        if key in {"tenant/tenant-a/FOO", "tenant/tenant-a/BAR"}:
            return MagicMock(value_encrypted="enc")
        return None

    session.get.side_effect = _session_get
    import orchestrator.core.secret_manager as module

    original_decrypt = module.decrypt_value
    module.decrypt_value = lambda ciphertext, encryption_key, **_: "tenant-value"  # type: ignore[assignment]
    try:
        assert (
            resolve_tenant_secret_ref(
                session,
                secret_ref="FOO",
                encryption_key="unused",
                tenant_id="tenant-a",
            )
            == "tenant-value"
        )
        assert (
            resolve_tenant_secret_ref(
                session,
                secret_ref="tenant/tenant-a/BAR",
                encryption_key="unused",
                tenant_id="tenant-a",
            )
            == "tenant-value"
        )
    finally:
        module.decrypt_value = original_decrypt  # type: ignore[assignment]


def test_resolve_tenant_secret_ref_rejects_invalid_refs() -> None:
    session = MagicMock()
    with patch.object(session, "get", side_effect=lambda _cls, key: None):
        with pytest.raises(ValueError, match="Tenant secret refs"):
            resolve_tenant_secret_ref(session, secret_ref="platform/FOO", encryption_key="unused", tenant_id="tenant-a")
        with pytest.raises(ValueError, match="Tenant secret refs"):
            resolve_tenant_secret_ref(session, secret_ref="project/t1/FOO", encryption_key="unused", tenant_id="tenant-a")
