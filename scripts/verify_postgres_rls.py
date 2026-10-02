"""Run PostgreSQL RLS proofs in one disposable, loopback-only container."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from urllib.parse import quote
from uuid import uuid4
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "pgvector/pgvector:0.8.6-pg16"


def _docker(
    host: str, *args: str, env: dict[str, str] | None = None, timeout: int = 60
) -> str:
    result = subprocess.run(
        ["docker", "--host", host, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError(
            "Disposable PostgreSQL Docker operation failed; check your local Docker daemon."
        )
    return result.stdout.strip()


def _local_docker_host() -> str:
    result = subprocess.run(
        ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode:
        raise RuntimeError("Cannot inspect the local Docker context.")
    host = json.loads(result.stdout)
    if not isinstance(host, str) or not host.startswith("unix:///"):
        raise RuntimeError(
            "PostgreSQL verification requires a local Unix Docker socket; remote contexts are rejected."
        )
    socket_path = Path(host.removeprefix("unix://"))
    if not stat.S_ISSOCK(socket_path.stat().st_mode):
        raise RuntimeError(
            "PostgreSQL verification requires an available local Unix Docker socket."
        )
    return host


def _wait_ready(host: str, container: str) -> None:
    deadline = time.monotonic() + 60
    # Probe the actual TCP readiness condition with bounded Docker IO; no sleeps.
    while time.monotonic() < deadline:
        if (
            _docker(
                host, "inspect", "--format", "{{.State.Running}}", container, timeout=5
            )
            != "true"
        ):
            raise RuntimeError("Disposable PostgreSQL exited before becoming ready.")
        result = subprocess.run(
            [
                "docker",
                "--host",
                host,
                "exec",
                container,
                "pg_isready",
                "-h",
                "127.0.0.1",
                "-d",
                "postgres",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return
    raise RuntimeError(
        "Disposable PostgreSQL TCP readiness did not succeed within 60 seconds."
    )


def main() -> int:
    host = _local_docker_host()
    name = f"master-builder-rls-{uuid4().hex}"
    password = secrets.token_urlsafe(48)
    owner = f"rls_owner_{uuid4().hex[:12]}"
    docker_env = dict(os.environ)
    docker_env["POSTGRES_PASSWORD"] = password
    container = _docker(
        host,
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        "master-builder.verification=postgres-rls",
        "--publish",
        "127.0.0.1::5432",
        "--env",
        "POSTGRES_PASSWORD",
        "--env",
        f"POSTGRES_USER={owner}",
        "--env",
        "POSTGRES_DB=postgres",
        IMAGE,
        env=docker_env,
        timeout=180,
    )
    if not re.fullmatch(r"[a-f0-9]{64}", container):
        raise RuntimeError(
            "Docker did not return a valid owned container identifier; no cleanup target was inferred."
        )
    try:
        _wait_ready(host, container)
        ports = json.loads(
            _docker(
                host,
                "inspect",
                "--format",
                "{{json .NetworkSettings.Ports}}",
                container,
            )
        )
        bindings = ports.get("5432/tcp")
        if not isinstance(bindings, list) or len(bindings) != 1:
            raise RuntimeError(
                "Disposable PostgreSQL requires one explicit loopback port binding."
            )
        binding = bindings[0]
        if (
            binding.get("HostIp") != "127.0.0.1"
            or not str(binding.get("HostPort", "")).isdigit()
        ):
            raise RuntimeError(
                "Disposable PostgreSQL has an invalid loopback port binding."
            )
        port = int(binding["HostPort"])
        # Never inherit a contributor's operational configuration or database.
        test_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ORCHESTRATOR_")
        }
        test_env["ORCHESTRATOR_RLS_TEST_DATABASE_URL"] = (
            f"postgresql+psycopg://{owner}:{quote(password, safe='')}@127.0.0.1:{port}/postgres"
        )
        with TemporaryDirectory(prefix="master-builder-rls-report-") as directory:
            report = Path(directory) / "results.xml"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "tests/test_tenant_rls_postgres.py",
                    "-q",
                    "--tb=line",
                    "--junitxml",
                    str(report),
                ],
                cwd=ROOT,
                env=test_env,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if not report.is_file():
                raise RuntimeError(
                    "PostgreSQL RLS tests produced no result report; verification failed."
                )
            suites = ET.parse(report).getroot()
            cases = list(suites.iter("testcase"))
            failures = [
                case
                for case in cases
                if case.find("failure") is not None or case.find("error") is not None
            ]
            skipped = [case for case in cases if case.find("skipped") is not None]
            print(
                f"Disposable PostgreSQL RLS: {len(cases) - len(failures) - len(skipped)} passed, {len(failures)} failed, {len(skipped)} skipped."
            )
            for case in failures + skipped:
                # Report names only: captured tracebacks may contain ephemeral URLs.
                print(
                    f"Unverified test: {case.get('classname', '')}.{case.get('name', '')}"
                )
            return (
                0
                if result.returncode == 0 and cases and not failures and not skipped
                else 1
            )
    finally:
        # Remove only the exact container ID returned by this invocation, including
        # its anonymous database volume. Shared containers/caches are untouched.
        _docker(host, "rm", "--force", "--volumes", container)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (
        RuntimeError,
        subprocess.TimeoutExpired,
        OSError,
        ValueError,
        ET.ParseError,
    ) as error:
        print(f"PostgreSQL RLS verification failed: {error}", file=sys.stderr)
        raise SystemExit(1) from None
