from __future__ import annotations

import pytest

from orchestrator.core.deployment_restore_executor import (
    DeploymentRestoreExecutionContext,
    build_docker_exec_restore_command,
    build_docker_exec_restore_command_text,
    build_restore_command,
)


@pytest.mark.parametrize(
    ("database_type", "expected_tokens"),
    (
        ("postgres", ("pg_restore", "--clean", "--if-exists")),
        ("mysql", ("mysql",)),
        ("mariadb", ("mariadb",)),
    ),
)
def test_build_restore_command_emits_expected_provider_command(database_type: str, expected_tokens: tuple[str, ...]) -> None:
    command = build_restore_command(
        DeploymentRestoreExecutionContext(
            database_type=database_type,
            container_name="coolify-db-container",
            database_name="app",
            username="app",
            password="secret",
            host="127.0.0.1",
            port=5432 if database_type == "postgres" else 3306,
            artifact_path="/var/lib/coolify/backups/app.dump",
        )
    )

    command_text = " ".join(command)
    for token in expected_tokens:
        assert token in command_text


def test_build_restore_command_rejects_unsupported_database_type() -> None:
    with pytest.raises(ValueError, match="Unsupported restore database type"):
        build_restore_command(
            DeploymentRestoreExecutionContext(
                database_type="redis",
                container_name="coolify-db-container",
                database_name="app",
                username="app",
                password="secret",
                host="127.0.0.1",
                port=6379,
                artifact_path="/var/lib/coolify/backups/app.rdb",
            )
        )


def test_build_docker_exec_restore_command_wraps_postgres_restore() -> None:
    command = build_docker_exec_restore_command(
        DeploymentRestoreExecutionContext(
            database_type="postgres",
            container_name="coolify-db-container",
            database_name="app",
            username="app",
            password="secret",
            host="127.0.0.1",
            port=5432,
            artifact_path="/var/lib/coolify/backups/app.dump",
        )
    )

    assert command.startswith("cat /var/lib/coolify/backups/app.dump | docker exec -i coolify-db-container sh -lc ")
    assert "PGPASSWORD=secret" in command
    assert "pg_restore" in command
    assert "/var/lib/coolify/backups/app.dump" not in command.split("| docker exec -i ", 1)[1]


def test_build_docker_exec_restore_command_text_uses_custom_container_reference() -> None:
    command = build_docker_exec_restore_command_text(
        context=DeploymentRestoreExecutionContext(
            database_type="mysql",
            container_name="ignored-container",
            database_name="app",
            username="app",
            password="secret",
            host="127.0.0.1",
            port=3306,
            artifact_path="/var/lib/coolify/backups/app.sql.gz",
        ),
        container_reference="'custom-container'",
    )

    assert command.startswith("gunzip -c /var/lib/coolify/backups/app.sql.gz | docker exec -i 'custom-container' sh -lc ")
    assert "MYSQL_PWD=secret" in command
    assert "gunzip -c" in command
    assert "mysql" in command


def test_build_restore_command_can_read_from_stdin_for_container_exec() -> None:
    command = build_restore_command(
        DeploymentRestoreExecutionContext(
            database_type="postgres",
            container_name="coolify-db-container",
            database_name="app",
            username="app",
            password="secret",
            host="127.0.0.1",
            port=5432,
            artifact_path="/var/lib/coolify/backups/app.dump",
        ),
        artifact_via_stdin=True,
    )

    command_text = " ".join(command)
    assert "pg_restore" in command_text
    assert "/var/lib/coolify/backups/app.dump" not in command_text
