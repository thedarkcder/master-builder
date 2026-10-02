"""Create private local configuration with unique keys; never overwrite a deployment."""

import argparse
import os
import json
from pathlib import Path
import secrets

from cryptography.fernet import Fernet
from orchestrator.core.qa.storage_setup import identity_config


def initialize(directory: Path) -> None:
    outputs = (
        directory / ".env",
        directory / "admin-ui/.env.local",
        directory / ".runtime-home/seaweedfs/s3.json",
    )
    for output in outputs:
        if output.exists():
            raise FileExistsError(
                f"Configuration already exists: {output.name}; refusing to replace it"
            )

    password_names = (
        "POSTGRES_PASSWORD",
        "POSTGRES_RUNTIME_PASSWORD",
        "ORCHESTRATOR_QA_DEMO_ARTIFACT_SECRET_KEY",
        "ORCHESTRATOR_CLICKHOUSE_PASSWORD",
        "ORCHESTRATOR_ADMIN_PASSWORD",
        "ORCHESTRATOR_ADMIN_TOKEN_SECRET",
        "ORCHESTRATOR_AUTH_TOKEN_SECRET",
        "ORCHESTRATOR_GITHUB_INSTALL_STATE_SECRET",
        "ORCHESTRATOR_ATLASSIAN_OAUTH_STATE_SECRET",
        "ORCHESTRATOR_DISCORD_INSTALL_STATE_SECRET",
        "ORCHESTRATOR_JIRA_ACTION_TOKEN_SECRET",
        "SEAWEEDFS_ADMIN_SECRET_KEY",
        "SEAWEEDFS_ADMIN_PASSWORD",
        "GRAFANA_ADMIN_PASSWORD",
        "AUTH_SECRET",
    )
    values = {name: secrets.token_urlsafe(32) for name in password_names}
    values["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode(
        "ascii"
    )
    values["ORCHESTRATOR_DATABASE_URL"] = (
        f"postgresql+psycopg://orchestrator_runtime:{values['POSTGRES_RUNTIME_PASSWORD']}@127.0.0.1:60003/orchestrator"
    )

    values["ORCHESTRATOR_CONTAINER_DATABASE_URL"] = values[
        "ORCHESTRATOR_DATABASE_URL"
    ].replace("@127.0.0.1:60003/", "@postgres:5432/")

    values["ORCHESTRATOR_MIGRATION_DATABASE_URL"] = (
        f"postgresql+psycopg://orchestrator_migrator:{values['POSTGRES_PASSWORD']}@127.0.0.1:60003/orchestrator"
    )
    values["ORCHESTRATOR_CONTAINER_MIGRATION_DATABASE_URL"] = values[
        "ORCHESTRATOR_MIGRATION_DATABASE_URL"
    ].replace("@127.0.0.1:60003/", "@postgres:5432/")

    # Read both templates before writing either output. Exclusive opens prevent
    # overwrites even if another initializer runs after the existence check.
    templates = (directory / ".env.example", directory / "admin-ui/.env.example")
    rendered = []
    for template in templates:
        lines = []
        for line in template.read_text(encoding="utf-8").splitlines():
            key, separator, _ = line.partition("=")
            if separator and key in values:
                line = f"{key}={values[key]}"
            lines.append(line)
        rendered.append("\n".join(lines) + "\n")
    root_values = dict(
        line.split("=", 1)
        for line in rendered[0].splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    rendered.append(
        json.dumps(
            identity_config(bucket=root_values["ORCHESTRATOR_QA_DEMO_ARTIFACT_BUCKET"]),
            indent=2,
        )
        + "\n"
    )
    outputs[2].parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for output, content in zip(outputs, rendered, strict=True):
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o644 if output == outputs[2] else 0o600,
        )
        os.fchmod(descriptor, 0o644 if output == outputs[2] else 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory", type=Path, default=Path(__file__).resolve().parents[1]
    )
    args = parser.parse_args()
    try:
        initialize(args.directory)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Local configuration initialization failed: {exc}\n")
    print(
        "Created private environment files and SeaweedFS identity policy with credential references. Existing outputs are never overwritten."
    )


if __name__ == "__main__":
    main()
