from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _fernet_for_key(encryption_key: str) -> Fernet:
    key = encryption_key.strip()
    if not key:
        raise ValueError("Secrets encryption key is not configured")
    try:
        return Fernet(key.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        # Backward-compatible local/dev behavior: treat non-empty strings as passphrases.
        derived_key = base64.urlsafe_b64encode(hashlib.sha256(key.encode("utf-8")).digest())
        try:
            return Fernet(derived_key)
        except (TypeError, ValueError) as derived_exc:  # pragma: no cover
            raise ValueError("Secrets encryption key must be a valid Fernet key") from derived_exc


def encrypt_value(*, plaintext: str, encryption_key: str) -> str:
    fernet = _fernet_for_key(encryption_key)
    token = fernet.encrypt(plaintext.encode("utf-8"))
    return token.decode("utf-8")


def decrypt_value(*, ciphertext: str, encryption_key: str) -> str:
    fernet = _fernet_for_key(encryption_key)
    try:
        plaintext = fernet.decrypt(ciphertext.encode("utf-8"))
    except InvalidToken as exc:
        raise ValueError("Unable to decrypt secret value") from exc
    return plaintext.decode("utf-8")
