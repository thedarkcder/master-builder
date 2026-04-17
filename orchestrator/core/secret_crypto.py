from __future__ import annotations

import base64
import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from functools import lru_cache
from typing import Mapping, Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_ENVELOPE_VERSION = "v2"
_LEGACY_ENVELOPE_VERSION = "v1"
_AES_NONCE_BYTES = 12
_AES_KEY_BYTES = 32
_VAULT_TIMEOUT_SECONDS = 10


class SecretCryptoError(ValueError):
    """Raised when stored secret encryption or decryption fails."""


@dataclass(frozen=True)
class SecretCipherEnvelope:
    version: str
    provider: str
    key_ref: str
    wrapped_data_key: str
    nonce_b64: str
    ciphertext_b64: str
    context_digest_b64: str | None = None

    def dumps(self) -> str:
        payload = {
            "version": self.version,
            "provider": self.provider,
            "key_ref": self.key_ref,
            "wrapped_data_key": self.wrapped_data_key,
            "nonce_b64": self.nonce_b64,
            "ciphertext_b64": self.ciphertext_b64,
        }
        if self.context_digest_b64 is not None:
            payload["context_digest_b64"] = self.context_digest_b64
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)

    @classmethod
    def loads(cls, payload: str) -> "SecretCipherEnvelope":
        try:
            loaded = json.loads(str(payload or "").strip())
        except json.JSONDecodeError as exc:
            raise SecretCryptoError("Stored secret payload is not valid JSON") from exc
        if not isinstance(loaded, dict):
            raise SecretCryptoError("Stored secret payload must be an object")
        version = str(loaded.get("version") or "").strip()
        provider = str(loaded.get("provider") or "").strip()
        key_ref = str(loaded.get("key_ref") or "").strip()
        wrapped_data_key = str(loaded.get("wrapped_data_key") or "").strip()
        nonce_b64 = str(loaded.get("nonce_b64") or "").strip()
        ciphertext_b64 = str(loaded.get("ciphertext_b64") or "").strip()
        if version not in {_ENVELOPE_VERSION, _LEGACY_ENVELOPE_VERSION}:
            raise SecretCryptoError(f"Unsupported stored secret payload version: {version or 'missing'}")
        if not provider or not key_ref or not wrapped_data_key or not nonce_b64 or not ciphertext_b64:
            raise SecretCryptoError("Stored secret payload is missing required fields")
        context_digest_b64 = None
        if version == _ENVELOPE_VERSION:
            context_digest_b64 = str(loaded.get("context_digest_b64") or "").strip()
            if not context_digest_b64:
                raise SecretCryptoError("Stored secret payload is missing required context binding")
        return cls(
            version=version,
            provider=provider,
            key_ref=key_ref,
            wrapped_data_key=wrapped_data_key,
            nonce_b64=nonce_b64,
            ciphertext_b64=ciphertext_b64,
            context_digest_b64=context_digest_b64,
        )


class WrappedKeyProvider(Protocol):
    provider_id: str

    @property
    def key_ref(self) -> str: ...

    def wrap_key(self, data_key: bytes) -> str: ...

    def unwrap_key(self, wrapped_key: str, *, key_ref: str) -> bytes: ...


def _b64encode(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64decode(value: str, *, field_name: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise SecretCryptoError(f"Stored secret payload field '{field_name}' is not valid base64") from exc


def _normalized_context_bytes(context: Mapping[str, str] | None) -> bytes:
    if not context:
        return b""
    normalized = {
        str(key).strip(): str(value).strip()
        for key, value in context.items()
        if str(key).strip() and str(value).strip()
    }
    if not normalized:
        return b""
    return json.dumps(normalized, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _context_digest_b64(context_bytes: bytes) -> str:
    return _b64encode(hashlib.sha256(context_bytes).digest())


def managed_secret_crypto_context(secret_ref: str) -> dict[str, str]:
    return {
        "kind": "managed_secret",
        "secret_ref": str(secret_ref or "").strip(),
    }


def jira_oauth_token_crypto_context(*, connection_id: str, token_field: str) -> dict[str, str]:
    return {
        "kind": "jira_oauth_token",
        "connection_id": str(connection_id or "").strip(),
        "token_field": str(token_field or "").strip(),
    }


@dataclass(frozen=True)
class SecretCryptoService:
    provider: WrappedKeyProvider

    def encrypt(self, *, plaintext: str, context: Mapping[str, str] | None = None) -> str:
        normalized_plaintext = str(plaintext or "")
        data_key = os.urandom(_AES_KEY_BYTES)
        nonce = os.urandom(_AES_NONCE_BYTES)
        aesgcm = AESGCM(data_key)
        context_bytes = _normalized_context_bytes(context)
        aad = context_bytes or None
        ciphertext = aesgcm.encrypt(nonce, normalized_plaintext.encode("utf-8"), aad)
        envelope = SecretCipherEnvelope(
            version=_ENVELOPE_VERSION if context_bytes else _LEGACY_ENVELOPE_VERSION,
            provider=self.provider.provider_id,
            key_ref=self.provider.key_ref,
            wrapped_data_key=self.provider.wrap_key(data_key),
            nonce_b64=_b64encode(nonce),
            ciphertext_b64=_b64encode(ciphertext),
            context_digest_b64=_context_digest_b64(context_bytes) if context_bytes else None,
        )
        return envelope.dumps()

    def decrypt(self, *, payload: str, context: Mapping[str, str] | None = None) -> str:
        envelope = SecretCipherEnvelope.loads(payload)
        if envelope.provider != self.provider.provider_id:
            raise SecretCryptoError(
                f"Stored secret payload provider '{envelope.provider}' does not match configured provider '{self.provider.provider_id}'"
            )
        data_key = self.provider.unwrap_key(envelope.wrapped_data_key, key_ref=envelope.key_ref)
        nonce = _b64decode(envelope.nonce_b64, field_name="nonce_b64")
        ciphertext = _b64decode(envelope.ciphertext_b64, field_name="ciphertext_b64")
        aad = None
        if envelope.version == _ENVELOPE_VERSION:
            context_bytes = _normalized_context_bytes(context)
            if not context_bytes:
                raise SecretCryptoError("Stored secret payload requires crypto context")
            if envelope.context_digest_b64 != _context_digest_b64(context_bytes):
                raise SecretCryptoError("Stored secret payload context does not match the owning record")
            aad = context_bytes
        try:
            plaintext = AESGCM(data_key).decrypt(nonce, ciphertext, aad)
        except Exception as exc:  # noqa: BLE001
            raise SecretCryptoError("Unable to decrypt stored secret payload") from exc
        return plaintext.decode("utf-8")


@dataclass(frozen=True)
class VaultTransitWrappedKeyProvider:
    vault_addr: str
    vault_token: str
    transit_key: str
    vault_namespace: str | None = None
    provider_id: str = "vault_transit"

    @property
    def key_ref(self) -> str:
        return self.transit_key

    def wrap_key(self, data_key: bytes) -> str:
        payload = self._vault_write(
            path=f"/v1/transit/encrypt/{self.transit_key}",
            body={"plaintext": _b64encode(data_key)},
        )
        ciphertext = str(((payload.get("data") or {}) if isinstance(payload, dict) else {}).get("ciphertext") or "").strip()
        if not ciphertext:
            raise SecretCryptoError("Vault Transit encrypt response did not include ciphertext")
        return ciphertext

    def unwrap_key(self, wrapped_key: str, *, key_ref: str) -> bytes:
        if key_ref != self.transit_key:
            raise SecretCryptoError(f"Stored secret key ref '{key_ref}' does not match configured Vault Transit key")
        payload = self._vault_write(
            path=f"/v1/transit/decrypt/{self.transit_key}",
            body={"ciphertext": wrapped_key},
        )
        plaintext_b64 = str(((payload.get("data") or {}) if isinstance(payload, dict) else {}).get("plaintext") or "").strip()
        if not plaintext_b64:
            raise SecretCryptoError("Vault Transit decrypt response did not include plaintext")
        return _b64decode(plaintext_b64, field_name="vault_plaintext")

    def _vault_write(self, *, path: str, body: dict[str, str]) -> dict:
        base = self.vault_addr.rstrip("/")
        if not base:
            raise SecretCryptoError("Vault address is not configured")
        if not self.vault_token.strip():
            raise SecretCryptoError("Vault token is not configured")
        request = urllib.request.Request(
            url=f"{base}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-Vault-Token": self.vault_token,
                **({"X-Vault-Namespace": self.vault_namespace} if self.vault_namespace else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=_VAULT_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise SecretCryptoError(f"Vault Transit request failed: {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise SecretCryptoError(f"Vault Transit request failed: {exc.reason}") from exc


@dataclass(frozen=True)
class AwsKmsWrappedKeyProvider:
    region_name: str
    key_id: str
    provider_id: str = "aws_kms"

    @property
    def key_ref(self) -> str:
        return self.key_id

    def wrap_key(self, data_key: bytes) -> str:
        client = self._client()
        response = client.encrypt(KeyId=self.key_id, Plaintext=data_key)
        wrapped = response.get("CiphertextBlob")
        if not isinstance(wrapped, (bytes, bytearray)):
            raise SecretCryptoError("AWS KMS encrypt response did not include CiphertextBlob")
        return _b64encode(bytes(wrapped))

    def unwrap_key(self, wrapped_key: str, *, key_ref: str) -> bytes:
        if key_ref != self.key_id:
            raise SecretCryptoError(f"Stored secret key ref '{key_ref}' does not match configured AWS KMS key")
        client = self._client()
        response = client.decrypt(CiphertextBlob=_b64decode(wrapped_key, field_name="wrapped_data_key"))
        plaintext = response.get("Plaintext")
        if not isinstance(plaintext, (bytes, bytearray)):
            raise SecretCryptoError("AWS KMS decrypt response did not include Plaintext")
        return bytes(plaintext)

    def _client(self):
        try:
            import boto3  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SecretCryptoError("boto3 is required for AWS KMS secret crypto") from exc
        return boto3.client("kms", region_name=self.region_name)


@dataclass(frozen=True)
class GcpKmsWrappedKeyProvider:
    key_name: str
    provider_id: str = "gcp_kms"

    @property
    def key_ref(self) -> str:
        return self.key_name

    def wrap_key(self, data_key: bytes) -> str:
        client = self._client()
        response = client.encrypt(request={"name": self.key_name, "plaintext": data_key})
        ciphertext = getattr(response, "ciphertext", None)
        if not isinstance(ciphertext, (bytes, bytearray)):
            raise SecretCryptoError("GCP KMS encrypt response did not include ciphertext")
        return _b64encode(bytes(ciphertext))

    def unwrap_key(self, wrapped_key: str, *, key_ref: str) -> bytes:
        if key_ref != self.key_name:
            raise SecretCryptoError(f"Stored secret key ref '{key_ref}' does not match configured GCP KMS key")
        client = self._client()
        response = client.decrypt(
            request={
                "name": self.key_name,
                "ciphertext": _b64decode(wrapped_key, field_name="wrapped_data_key"),
            }
        )
        plaintext = getattr(response, "plaintext", None)
        if not isinstance(plaintext, (bytes, bytearray)):
            raise SecretCryptoError("GCP KMS decrypt response did not include plaintext")
        return bytes(plaintext)

    def _client(self):
        try:
            from google.cloud import kms_v1  # type: ignore[import-not-found]
        except ImportError as exc:
            raise SecretCryptoError("google-cloud-kms is required for GCP KMS secret crypto") from exc
        return kms_v1.KeyManagementServiceClient()


def secret_crypto_provider_name(settings, *, encryption_key: str = "") -> str:
    provider_name = str(getattr(settings, "secret_crypto_provider", "") or "").strip().lower()
    if provider_name:
        return provider_name
    if str(encryption_key or "").strip() or str(getattr(settings, "secrets_encryption_key", "") or "").strip():
        return "fernet_legacy"
    raise SecretCryptoError("Secret crypto provider is not configured")


def has_provider_backed_secret_crypto(settings) -> bool:
    provider_name = str(getattr(settings, "secret_crypto_provider", "") or "").strip().lower()
    return provider_name in {"vault_transit", "aws_kms", "gcp_kms"}


@lru_cache(maxsize=16)
def _cached_service(
    provider_name: str,
    vault_addr: str,
    vault_token: str,
    vault_namespace: str,
    vault_transit_key: str,
    aws_region: str,
    aws_kms_key_id: str,
    gcp_kms_key_name: str,
) -> SecretCryptoService:
    if provider_name == "vault_transit":
        if not vault_addr.strip() or not vault_token.strip() or not vault_transit_key.strip():
            raise SecretCryptoError("Vault Transit secret crypto requires vault address, token, and transit key")
        provider = VaultTransitWrappedKeyProvider(
            vault_addr=vault_addr.strip(),
            vault_token=vault_token.strip(),
            vault_namespace=vault_namespace.strip() or None,
            transit_key=vault_transit_key.strip(),
        )
        return SecretCryptoService(provider=provider)
    if provider_name == "aws_kms":
        if not aws_region.strip() or not aws_kms_key_id.strip():
            raise SecretCryptoError("AWS KMS secret crypto requires region and key id")
        provider = AwsKmsWrappedKeyProvider(
            region_name=aws_region.strip(),
            key_id=aws_kms_key_id.strip(),
        )
        return SecretCryptoService(provider=provider)
    if provider_name == "gcp_kms":
        if not gcp_kms_key_name.strip():
            raise SecretCryptoError("GCP KMS secret crypto requires a KMS key name")
        provider = GcpKmsWrappedKeyProvider(key_name=gcp_kms_key_name.strip())
        return SecretCryptoService(provider=provider)
    raise SecretCryptoError(f"Unsupported secret crypto provider: {provider_name}")


def secret_crypto_service(settings) -> SecretCryptoService:
    provider_name = secret_crypto_provider_name(settings)
    if provider_name == "fernet_legacy":
        raise SecretCryptoError("Legacy Fernet secret crypto does not use the provider-backed secret crypto service")
    return _cached_service(
        provider_name,
        str(getattr(settings, "vault_addr", "") or ""),
        str(getattr(settings, "vault_token", "") or ""),
        str(getattr(settings, "vault_namespace", "") or ""),
        str(getattr(settings, "vault_transit_key", "") or ""),
        str(getattr(settings, "aws_kms_region", "") or ""),
        str(getattr(settings, "aws_kms_key_id", "") or ""),
        str(getattr(settings, "gcp_kms_key_name", "") or ""),
    )


def encrypt_secret_value(
    *,
    plaintext: str,
    settings,
    encryption_key: str = "",
    context: Mapping[str, str] | None = None,
) -> str:
    provider_name = secret_crypto_provider_name(settings, encryption_key=encryption_key)
    if provider_name == "fernet_legacy":
        from orchestrator.core.secrets import encrypt_value

        return encrypt_value(plaintext=plaintext, encryption_key=encryption_key or settings.secrets_encryption_key)
    return secret_crypto_service(settings).encrypt(plaintext=plaintext, context=context)


def decrypt_secret_value(
    *,
    ciphertext: str,
    settings,
    encryption_key: str = "",
    context: Mapping[str, str] | None = None,
) -> str:
    provider_name = secret_crypto_provider_name(settings, encryption_key=encryption_key)
    if provider_name == "fernet_legacy":
        from orchestrator.core.secrets import decrypt_value

        return decrypt_value(ciphertext=ciphertext, encryption_key=encryption_key or settings.secrets_encryption_key)
    normalized_ciphertext = str(ciphertext or "").strip()
    if not normalized_ciphertext.startswith("{"):
        from orchestrator.core.secrets import decrypt_value

        return decrypt_value(ciphertext=ciphertext, encryption_key=encryption_key or settings.secrets_encryption_key)
    return secret_crypto_service(settings).decrypt(payload=ciphertext, context=context)


def reencrypt_legacy_secret_value(
    *,
    ciphertext: str,
    settings,
    encryption_key: str = "",
    context: Mapping[str, str] | None = None,
) -> str | None:
    provider_name = secret_crypto_provider_name(settings, encryption_key=encryption_key)
    if provider_name == "fernet_legacy":
        return None
    normalized_ciphertext = str(ciphertext or "").strip()
    if not normalized_ciphertext or normalized_ciphertext.startswith("{"):
        return None
    from orchestrator.core.secrets import decrypt_value

    plaintext = decrypt_value(ciphertext=ciphertext, encryption_key=encryption_key or settings.secrets_encryption_key)
    return secret_crypto_service(settings).encrypt(plaintext=plaintext, context=context)
