from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
import json

from orchestrator.core.github_install_state import create_install_state_token, parse_install_state_token
from orchestrator.core.atlassian_oauth_state import create_atlassian_oauth_state_token, parse_atlassian_oauth_state_token


class GitHubInstallStateTests(unittest.TestCase):
    def test_install_state_roundtrip_and_validation_errors(self) -> None:
        now = datetime.now(timezone.utc)
        token = create_install_state_token(
            tenant_id=" route25 ",
            exp=now + timedelta(minutes=5),
            secret="secret",
            return_to="edit",
        )
        parsed = parse_install_state_token(token=token, secret="secret", now=now)
        self.assertEqual(parsed.tenant_id, "route25")
        self.assertEqual(parsed.return_to, "edit")

        with self.assertRaisesRegex(ValueError, "tenant_id"):
            create_install_state_token(tenant_id=" ", exp=now, secret="secret")
        with self.assertRaisesRegex(ValueError, "secret"):
            create_install_state_token(tenant_id="route25", exp=now, secret="")
        with self.assertRaisesRegex(ValueError, "return_to"):
            create_install_state_token(tenant_id="route25", exp=now, secret="secret", return_to="bad")
        with self.assertRaisesRegex(ValueError, "format"):
            parse_install_state_token(token="bad", secret="secret", now=now)
        with self.assertRaisesRegex(ValueError, "state token is required"):
            parse_install_state_token(token="", secret="secret", now=now)
        with self.assertRaisesRegex(ValueError, "secret is required"):
            parse_install_state_token(token=token, secret="", now=now)
        with self.assertRaisesRegex(ValueError, "signature"):
            parse_install_state_token(token=f"{token}.extra", secret="secret", now=now)
        with self.assertRaisesRegex(ValueError, "expired"):
            parse_install_state_token(token=token, secret="secret", now=now + timedelta(days=1))

    def test_install_state_payload_validation(self) -> None:
        now = datetime.now(timezone.utc)
        payload = {"tenant_id": "route25", "exp": "not-an-int", "return_to": "edit"}
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_token = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        signature = hmac.new(b"secret", payload_token.encode("ascii"), hashlib.sha256).digest()
        signature_token = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        with self.assertRaisesRegex(ValueError, "exp"):
            parse_install_state_token(token=f"{payload_token}.{signature_token}", secret="secret", now=now)

        payload_bad_tenant = {"tenant_id": " ", "exp": int((now + timedelta(minutes=5)).timestamp()), "return_to": "edit"}
        payload_bytes = json.dumps(payload_bad_tenant, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_token = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        signature = hmac.new(b"secret", payload_token.encode("ascii"), hashlib.sha256).digest()
        signature_token = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        with self.assertRaisesRegex(ValueError, "tenant_id"):
            parse_install_state_token(token=f"{payload_token}.{signature_token}", secret="secret", now=now)

        payload_bad_return_to = {"tenant_id": "route25", "exp": int((now + timedelta(minutes=5)).timestamp()), "return_to": "bad"}
        payload_bytes = json.dumps(payload_bad_return_to, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_token = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        signature = hmac.new(b"secret", payload_token.encode("ascii"), hashlib.sha256).digest()
        signature_token = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        with self.assertRaisesRegex(ValueError, "return_to"):
            parse_install_state_token(token=f"{payload_token}.{signature_token}", secret="secret", now=now)


class AtlassianOAuthStateTests(unittest.TestCase):
    def test_jira_state_roundtrip_and_validation_errors(self) -> None:
        now = datetime.now(timezone.utc)
        token = create_atlassian_oauth_state_token(
            exp=now + timedelta(minutes=5),
            secret="secret",
            return_to="wizard",
            tenant_id=None,
        )
        parsed = parse_atlassian_oauth_state_token(token=token, secret="secret", now=now)
        self.assertEqual(parsed.return_to, "wizard")
        self.assertIsNone(parsed.tenant_id)

        edit_token = create_atlassian_oauth_state_token(
            exp=now + timedelta(minutes=5),
            secret="secret",
            return_to="edit",
            tenant_id="route25",
        )
        parsed_edit = parse_atlassian_oauth_state_token(token=edit_token, secret="secret", now=now)
        self.assertEqual(parsed_edit.tenant_id, "route25")

        with self.assertRaisesRegex(ValueError, "secret"):
            create_atlassian_oauth_state_token(exp=now, secret="")
        with self.assertRaisesRegex(ValueError, "return_to"):
            create_atlassian_oauth_state_token(exp=now, secret="secret", return_to="bad")
        with self.assertRaisesRegex(ValueError, "required"):
            parse_atlassian_oauth_state_token(token="", secret="secret")
        with self.assertRaisesRegex(ValueError, "format"):
            parse_atlassian_oauth_state_token(token="bad", secret="secret")
        with self.assertRaisesRegex(ValueError, "signature"):
            parse_atlassian_oauth_state_token(token=f"{token}.extra", secret="secret")
        with self.assertRaisesRegex(ValueError, "expired"):
            parse_atlassian_oauth_state_token(token=token, secret="secret", now=now + timedelta(days=1))

        payload = {"exp": int((now + timedelta(minutes=5)).timestamp()), "return_to": "edit", "tenant_id": None}
        payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_token = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        signature = hmac.new(b"secret", payload_token.encode("ascii"), hashlib.sha256).digest()
        signature_token = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        with self.assertRaisesRegex(ValueError, "tenant_id"):
            parse_atlassian_oauth_state_token(token=f"{payload_token}.{signature_token}", secret="secret", now=now)

        payload_bad_tenant_type = {"exp": int((now + timedelta(minutes=5)).timestamp()), "return_to": "wizard", "tenant_id": 123}
        payload_bytes = json.dumps(payload_bad_tenant_type, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_token = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        signature = hmac.new(b"secret", payload_token.encode("ascii"), hashlib.sha256).digest()
        signature_token = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        with self.assertRaisesRegex(ValueError, "tenant_id"):
            parse_atlassian_oauth_state_token(token=f"{payload_token}.{signature_token}", secret="secret", now=now)

        payload_bad_exp = {"exp": "bad", "return_to": "wizard", "tenant_id": None}
        payload_bytes = json.dumps(payload_bad_exp, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_token = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        signature = hmac.new(b"secret", payload_token.encode("ascii"), hashlib.sha256).digest()
        signature_token = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        with self.assertRaisesRegex(ValueError, "exp"):
            parse_atlassian_oauth_state_token(token=f"{payload_token}.{signature_token}", secret="secret", now=now)

        payload_bad_return_to = {"exp": int((now + timedelta(minutes=5)).timestamp()), "return_to": "bad", "tenant_id": None}
        payload_bytes = json.dumps(payload_bad_return_to, separators=(",", ":"), sort_keys=True).encode("utf-8")
        payload_token = base64.urlsafe_b64encode(payload_bytes).rstrip(b"=").decode("ascii")
        signature = hmac.new(b"secret", payload_token.encode("ascii"), hashlib.sha256).digest()
        signature_token = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        with self.assertRaisesRegex(ValueError, "return_to"):
            parse_atlassian_oauth_state_token(token=f"{payload_token}.{signature_token}", secret="secret", now=now)


if __name__ == "__main__":
    unittest.main()
