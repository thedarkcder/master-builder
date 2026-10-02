from __future__ import annotations

import shlex
from dataclasses import dataclass


@dataclass(frozen=True)
class DeploymentRestoreExecutionContext:
    database_type: str
    container_name: str
    database_name: str
    username: str
    password: str
    host: str
    port: int
    artifact_path: str


def _quoted(value: str) -> str:
    return shlex.quote(value)


def _decompress_prefix(artifact_path: str) -> str:
    return "gunzip -c" if str(artifact_path).strip().lower().endswith(".gz") else "cat"


def build_restore_command(
    context: DeploymentRestoreExecutionContext, *, artifact_via_stdin: bool = False
) -> list[str]:
    database_type = str(context.database_type or "").strip().lower()
    username = _quoted(context.username)
    database_name = _quoted(context.database_name)
    host = _quoted(context.host)
    port = str(int(context.port))
    reader = _decompress_prefix(context.artifact_path)
    artifact_path = _quoted(context.artifact_path)

    def _with_artifact_prefix(command_text: str) -> str:
        if artifact_via_stdin:
            return command_text
        return f"{reader} {artifact_path} | {command_text}"

    if database_type == "postgres":
        if str(context.artifact_path).strip().lower().endswith((".sql", ".sql.gz")):
            restore = _with_artifact_prefix(
                f"psql --host {host} --port {port} --username {username} --dbname {database_name}"
            )
        else:
            restore = _with_artifact_prefix(
                f"pg_restore --clean --if-exists --no-owner --no-privileges "
                f"--host {host} --port {port} --username {username} --dbname {database_name}"
            )
        return ["sh", "-lc", restore]

    if database_type == "mysql":
        restore = _with_artifact_prefix(
            f"mysql --host {host} --port {port} --user {username} {database_name}"
        )
        return ["sh", "-lc", restore]

    if database_type == "mariadb":
        restore = _with_artifact_prefix(
            f"mariadb --host {host} --port {port} --user {username} {database_name}"
        )
        return ["sh", "-lc", restore]

    raise ValueError(f"Unsupported restore database type: {context.database_type}")


def build_docker_exec_restore_command(
    context: DeploymentRestoreExecutionContext,
) -> str:
    return build_docker_exec_restore_command_text(
        context=context, container_reference=_quoted(context.container_name)
    )


def build_docker_exec_restore_command_text(
    *,
    context: DeploymentRestoreExecutionContext,
    container_reference: str,
    container_runtime_command: str = "docker",
) -> str:
    inner_command = build_restore_command(context, artifact_via_stdin=True)
    command_text = " ".join(_quoted(token) for token in inner_command)
    reader = _decompress_prefix(context.artifact_path)
    artifact_path = _quoted(context.artifact_path)
    if str(context.database_type or "").strip().lower() == "postgres":
        env_prefix = f"PGPASSWORD={_quoted(context.password)}"
    else:
        env_prefix = f"MYSQL_PWD={_quoted(context.password)}"
    runtime_command = _quoted(
        str(container_runtime_command or "docker").strip() or "docker"
    )
    docker_exec = f"{runtime_command} exec -i {container_reference} sh -lc {_quoted(f'{env_prefix} {command_text}')}"
    return f"{reader} {artifact_path} | {docker_exec}"
