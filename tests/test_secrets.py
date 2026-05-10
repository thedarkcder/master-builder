import unittest

from cryptography.fernet import Fernet

from orchestrator.core.platform.secrets import decrypt_value, encrypt_value


class SecretsTests(unittest.TestCase):
    def test_encrypt_decrypt_with_fernet_key(self) -> None:
        key = Fernet.generate_key().decode("utf-8")
        encrypted = encrypt_value(plaintext="hello", encryption_key=key)
        decrypted = decrypt_value(ciphertext=encrypted, encryption_key=key)
        self.assertEqual(decrypted, "hello")

    def test_encrypt_with_invalid_key_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "valid Fernet key"):
            encrypt_value(plaintext="hello", encryption_key="not-a-fernet-key")

    def test_encrypt_with_blank_key_fails(self) -> None:
        with self.assertRaisesRegex(ValueError, "Secrets encryption key is not configured"):
            encrypt_value(plaintext="hello", encryption_key="   ")


if __name__ == "__main__":
    unittest.main()
