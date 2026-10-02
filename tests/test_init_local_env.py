from pathlib import Path
import subprocess
import sys

from cryptography.fernet import Fernet


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "init_local_env.py"


def _templates(root: Path) -> None:
    root.joinpath("admin-ui").mkdir()
    repo = SCRIPT.parents[1]
    root.joinpath(".env.example").write_bytes(
        repo.joinpath(".env.example").read_bytes()
    )
    root.joinpath("admin-ui/.env.example").write_text(
        "AUTH_SECRET=\nNEXT_PUBLIC_API_BASE_URL=http://localhost:60001\n"
    )


def _values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if line and not line.startswith("#")
    )


def test_initialization_generates_independent_secrets_without_disclosing_them(tmp_path):
    _templates(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    values = _values(tmp_path / ".env")
    key = values["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"]
    cipher = Fernet(key.encode())
    assert cipher.decrypt(cipher.encrypt(b"test")) == b"test"
    required = [
        "ORCHESTRATOR_ADMIN_PASSWORD",
        "ORCHESTRATOR_ADMIN_TOKEN_SECRET",
        "ORCHESTRATOR_AUTH_TOKEN_SECRET",
        "POSTGRES_PASSWORD",
        "SEAWEEDFS_ADMIN_SECRET_KEY",
    ]
    secrets = [values[name] for name in required]
    assert len(set(secrets)) == len(secrets)
    for value in secrets + [key]:
        assert len(value) >= 32
        assert value not in result.stdout + result.stderr
    ui_values = _values(tmp_path / "admin-ui/.env.local")
    assert len(ui_values["AUTH_SECRET"]) >= 32
    assert ui_values["AUTH_SECRET"] not in secrets
    assert tmp_path.joinpath(".env").stat().st_mode & 0o777 == 0o600
    config_path = tmp_path / ".runtime-home/seaweedfs/s3.json"
    assert config_path.stat().st_mode & 0o777 == 0o644
    config = config_path.read_text()
    assert "${SEAWEEDFS_ADMIN_SECRET_KEY}" in config
    assert "${ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY}" in config
    assert all(value not in config for value in secrets + [key])


def test_initialization_refuses_to_overwrite_existing_configuration(tmp_path):
    _templates(tmp_path)
    existing = tmp_path / "admin-ui/.env.local"
    existing.write_text("preserve-this-configuration\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert existing.read_text() == "preserve-this-configuration\n"
    assert not tmp_path.joinpath(".env").exists()


def test_initialization_is_unique_between_installations(tmp_path):
    keys = []
    for name in ("one", "two"):
        root = tmp_path / name
        root.mkdir()
        _templates(root)
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--directory", str(root)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        keys.append(_values(root / ".env")["ORCHESTRATOR_AUTH_TOKEN_SECRET"])
    assert keys[0] != keys[1]


def test_initializer_separates_migration_and_runtime_credentials(tmp_path):
    _templates(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    values = _values(tmp_path / ".env")
    assert values["POSTGRES_RUNTIME_PASSWORD"] != values["POSTGRES_PASSWORD"]
    assert "://orchestrator_runtime:" in values["ORCHESTRATOR_DATABASE_URL"]
    assert "://orchestrator_migrator:" in values["ORCHESTRATOR_MIGRATION_DATABASE_URL"]
    assert (
        values["ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY"]
        != values["SEAWEEDFS_ADMIN_SECRET_KEY"]
    )


def test_realized_compose_forwards_explicit_local_identity_contract(tmp_path):
    import json
    import os
    import shutil
    import pytest

    if shutil.which("docker") is None:
        pytest.skip("Docker Compose CLI required for realized configuration proof")
    _templates(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--directory", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    repo = SCRIPT.parents[1]
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            str(tmp_path / ".env"),
            "-f",
            str(repo / "docker-compose.yml"),
            "config",
            "--format",
            "json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        env={
            key: value
            for key, value in os.environ.items()
            if key not in _values(tmp_path / ".env")
        },
    )
    assert result.returncode == 0, (
        "Generated Compose configuration could not be realized"
    )
    api = json.loads(result.stdout)["services"]["api"]["environment"]
    assert api["ORCHESTRATOR_PUBLIC_REGISTRATION_ENABLED"] == "true"
    assert "api" in api["ORCHESTRATOR_TRUSTED_HOSTS"].split(",")
    assert api["ORCHESTRATOR_AUTH_LOGIN_ACCOUNT_LIMIT"] == "10"
    assert api["ORCHESTRATOR_AUTH_REGISTRATION_GLOBAL_LIMIT"] == "20"
    assert api["ORCHESTRATOR_QA_DEMO_ARTIFACT_PUBLIC_BASE_URL"].endswith(
        "/api/bff/api/qa-artifacts"
    )
    assert api["ORCHESTRATOR_QA_DEMO_ARTIFACT_ACCESS_KEY"] == "masterbuilder-qa"
