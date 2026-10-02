"""The actual scanner gate requires an explicitly supplied pinned CLI."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/verify_secret_scanner.py"


def _run(binary: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--gitleaks", binary, *arguments],
        capture_output=True,
        text=True,
        timeout=120,
    )


def _actual_binary() -> str:
    binary = os.environ.get("MASTER_BUILDER_GITLEAKS_BINARY")
    if binary is None:
        pytest.skip(
            "Actual scanner integration requires MASTER_BUILDER_GITLEAKS_BINARY; "
            "security CI runs the required canary directly with its verified CLI"
        )
    return binary


def test_canary_requires_available_scanner_without_install_fallback(tmp_path):
    result = _run(str(tmp_path / "missing-gitleaks"))
    assert result.returncode == 1
    assert "required Gitleaks CLI" in result.stderr
    assert "Traceback" not in result.stderr


def test_actual_scanner_detects_fresh_secrets_after_existing_parser_fixtures():
    result = _run(_actual_binary())
    assert result.returncode == 0, result.stderr
    evidence = json.loads(result.stdout)
    assert evidence["scanner_version"] == "8.30.1"
    assert evidence["complete_pem_after_existing_fixture_detected"] is True
    assert evidence["configured_fernet_literal_detected"] is True
    assert evidence["finding_count"] >= 2
    assert "BEGIN PRIVATE KEY" not in result.stdout + result.stderr


def test_actual_scanner_canary_rejects_config_without_required_key_rules(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_github_app.py").write_bytes(
        (ROOT / "tests/test_github_app.py").read_bytes()
    )
    (tmp_path / ".gitleaks.toml").write_text("[extend]\nuseDefault = true\n")
    result = _run(_actual_binary(), "--repo-root", str(tmp_path))
    assert result.returncode == 1
    assert "required key detections" in result.stderr
    assert "BEGIN PRIVATE KEY" not in result.stdout + result.stderr
