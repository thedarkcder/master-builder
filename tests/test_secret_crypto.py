from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet

from orchestrator.core.config import Settings
from orchestrator.core.secret_crypto import (
    SecretCryptoError,
    SecretCryptoService,
    SecretCipherEnvelope,
    decrypt_secret_value,
    encrypt_secret_value,
    jira_oauth_token_crypto_context,
    managed_secret_crypto_context,
)
from orchestrator.core.secrets import encrypt_value


class _FakeWrappedKeyProvider:
    provider_id = "fake"
    key_ref = "fake-key"

    def wrap_key(self, data_key: bytes) -> str:
        return data_key.hex()

    def unwrap_key(self, wrapped_key: str, *, key_ref: str) -> bytes:
        if key_ref != self.key_ref:
            raise SecretCryptoError("wrong key")
        return bytes.fromhex(wrapped_key)


class SecretCryptoTests(unittest.TestCase):
    def test_settings_default_vault_transit_key_name_is_stable(self) -> None:
        settings = Settings()
        self.assertEqual(settings.vault_transit_key, "master-builder")

    def test_provider_backed_round_trip(self) -> None:
        service = SecretCryptoService(provider=_FakeWrappedKeyProvider())
        payload = service.encrypt(plaintext="hello")
        envelope = SecretCipherEnvelope.loads(payload)
        self.assertEqual(envelope.provider, "fake")
        self.assertEqual(service.decrypt(payload=payload), "hello")

    def test_provider_backed_context_binding_rejects_wrong_owner(self) -> None:
        service = SecretCryptoService(provider=_FakeWrappedKeyProvider())
        payload = service.encrypt(plaintext="hello", context=managed_secret_crypto_context("platform/API_KEY"))
        with self.assertRaisesRegex(SecretCryptoError, "context does not match"):
            service.decrypt(payload=payload, context=managed_secret_crypto_context("platform/OTHER_KEY"))
        self.assertEqual(
            service.decrypt(payload=payload, context=managed_secret_crypto_context("platform/API_KEY")),
            "hello",
        )

    def test_provider_mismatch_fails(self) -> None:
        service = SecretCryptoService(provider=_FakeWrappedKeyProvider())
        payload = service.encrypt(plaintext="hello")
        wrong_provider = type("WrongProvider", (), {"provider_id": "wrong", "key_ref": "wrong", "wrap_key": lambda self, data_key: "", "unwrap_key": lambda self, wrapped_key, key_ref: b""})()
        with self.assertRaisesRegex(SecretCryptoError, "does not match configured provider"):
            SecretCryptoService(provider=wrong_provider).decrypt(payload=payload)

    def test_legacy_fallback_uses_explicit_encryption_key(self) -> None:
        key = Fernet.generate_key().decode("utf-8")
        settings = SimpleNamespace(secret_crypto_provider="", secrets_encryption_key="")
        ciphertext = encrypt_secret_value(plaintext="hello", settings=settings, encryption_key=key)
        self.assertEqual(decrypt_secret_value(ciphertext=ciphertext, settings=settings, encryption_key=key), "hello")

    def test_provider_mode_rejects_legacy_fernet_payloads_after_cutover(self) -> None:
        key = Fernet.generate_key().decode("utf-8")
        settings = SimpleNamespace(
            secret_crypto_provider="vault_transit",
            secrets_encryption_key=key,
            vault_addr="https://vault.example.com",
            vault_token="vault-token",
            vault_namespace="",
            vault_transit_key="master-builder",
            aws_kms_region="",
            aws_kms_key_id="",
            gcp_kms_key_name="",
        )
        ciphertext = encrypt_value(plaintext="hello", encryption_key=key)
        with self.assertRaisesRegex(SecretCryptoError, "not valid JSON"):
            decrypt_secret_value(ciphertext=ciphertext, settings=settings)

    def test_provider_backed_helpers_use_context(self) -> None:
        settings = SimpleNamespace(
            secret_crypto_provider="fake",
            secrets_encryption_key="",
            vault_addr="",
            vault_token="",
            vault_namespace="",
            vault_transit_key="",
            aws_kms_region="",
            aws_kms_key_id="",
            gcp_kms_key_name="",
        )
        service = SecretCryptoService(provider=_FakeWrappedKeyProvider())
        with patch("orchestrator.core.secret_crypto.secret_crypto_service", return_value=service):
            ciphertext = encrypt_secret_value(
                plaintext="hello",
                settings=settings,
                context=jira_oauth_token_crypto_context(connection_id="conn-1", token_field="access_token"),
            )
            self.assertEqual(
                decrypt_secret_value(
                    ciphertext=ciphertext,
                    settings=settings,
                    context=jira_oauth_token_crypto_context(connection_id="conn-1", token_field="access_token"),
                ),
                "hello",
            )
            with self.assertRaisesRegex(SecretCryptoError, "context does not match"):
                decrypt_secret_value(
                    ciphertext=ciphertext,
                    settings=settings,
                    context=jira_oauth_token_crypto_context(connection_id="conn-2", token_field="access_token"),
                )


if __name__ == "__main__":
    unittest.main()
