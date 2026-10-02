#!/usr/bin/env python3
"""Fail closed unless pinned Gitleaks detects ephemeral real keys in fixture paths.

Requires an explicitly supplied Gitleaks 8.30.1 executable and the OpenSSL CLI.
The security workflow verifies the Gitleaks archive checksum before this gate.
No executable is downloaded or substituted by this script.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile

SCANNER_VERSION = "8.30.1"
COMMAND_TIMEOUT_SECONDS = 90


class CanaryError(RuntimeError):
    """A required scanner/tool contract was not established."""


def _command(arguments: list[str], *, tool: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Captured tool output can contain key material; never echo it on failure.
        raise CanaryError(
            f"The required {tool} CLI is unavailable or timed out"
        ) from exc


def verify_canary(*, gitleaks: str, openssl: str, repo_root: Path) -> dict[str, object]:
    version = _command([gitleaks, "version"], tool="Gitleaks")
    if version.returncode != 0 or version.stdout.strip() != SCANNER_VERSION:
        raise CanaryError(f"The required Gitleaks CLI version is {SCANNER_VERSION}")
    config = repo_root / ".gitleaks.toml"
    existing_fixture = repo_root / "tests/test_github_app.py"
    if not config.is_file() or not existing_fixture.is_file():
        raise CanaryError(
            "The repository scanner configuration and parser fixture are required"
        )
    with tempfile.TemporaryDirectory(
        prefix="master-builder-scanner-canary-"
    ) as directory:
        work = Path(directory)
        work.chmod(0o700)
        scan_root = work / "scan"
        (scan_root / "tests").mkdir(parents=True)
        (scan_root / "orchestrator").mkdir()
        pem = work / "ephemeral-private.pem"
        generated = _command(
            [
                openssl,
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(pem),
            ],
            tool="OpenSSL",
        )
        if generated.returncode != 0 or not pem.is_file():
            raise CanaryError(
                "The required OpenSSL CLI could not generate the ephemeral RSA canary"
            )
        pem.chmod(0o600)
        fixture = scan_root / "tests/test_github_app.py"
        fixture.write_bytes(existing_fixture.read_bytes() + b"\n" + pem.read_bytes())
        fixture.chmod(0o600)
        fernet = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii")
        configured_key = scan_root / "orchestrator/secret_canary.py"
        configured_key.write_text(f'ORCHESTRATOR_SECRETS_ENCRYPTION_KEY="{fernet}"\n')
        configured_key.chmod(0o600)
        report = work / "redacted-report.json"
        scanned = _command(
            [
                gitleaks,
                "dir",
                str(scan_root),
                "--config",
                str(config.resolve()),
                "--no-banner",
                "--redact=100",
                "--ignore-gitleaks-allow",
                "--report-format=json",
                "--report-path",
                str(report),
            ],
            tool="Gitleaks",
        )
        if scanned.returncode != 1 or not report.is_file():
            raise CanaryError(
                "Scanner canary did not establish the required key detections"
            )
        try:
            findings = json.loads(report.read_text())
        except (OSError, ValueError) as exc:
            raise CanaryError("Scanner canary report could not be verified") from exc
        if not isinstance(findings, list) or not all(
            isinstance(item, dict) for item in findings
        ):
            raise CanaryError("Scanner canary report did not contain valid findings")
        required = {
            "master-builder-pem-key": "tests/test_github_app.py",
            "master-builder-fernet-key": "orchestrator/secret_canary.py",
        }
        for rule, file in required.items():
            if not any(
                finding.get("RuleID") == rule
                and str(finding.get("File", "")).replace("\\", "/").endswith(file)
                for finding in findings
            ):
                raise CanaryError(
                    "Scanner canary did not establish the required key detections"
                )
        return {
            "scanner_version": SCANNER_VERSION,
            "finding_count": len(findings),
            "complete_pem_after_existing_fixture_detected": True,
            "configured_fernet_literal_detected": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gitleaks", required=True, help="Checksum-verified Gitleaks 8.30.1 executable"
    )
    parser.add_argument(
        "--openssl",
        default="openssl",
        help="Required OpenSSL executable (default: openssl)",
    )
    parser.add_argument(
        "--repo-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    arguments = parser.parse_args()
    try:
        evidence = verify_canary(
            gitleaks=arguments.gitleaks,
            openssl=arguments.openssl,
            repo_root=arguments.repo_root,
        )
    except CanaryError as exc:
        print(f"Secret-scanner canary failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(evidence, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
