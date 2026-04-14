from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from orchestrator.core.deployment_host_agent_runtime import (
    DeploymentHostAgent,
    DeploymentHostAgentConfig,
    load_deployment_host_agent_access_token,
)


def _agent_config(*, access_token_path: Path, bootstrap_token: str | None = None) -> DeploymentHostAgentConfig:
    return DeploymentHostAgentConfig(
        api_base_url="http://localhost:4000",
        bootstrap_token=bootstrap_token,
        bootstrap_token_path=None,
        access_token=None,
        access_token_path=access_token_path,
        capabilities=("restore_database", "postgres"),
        heartbeat_interval_seconds=30,
        poll_interval_seconds=1,
        command_timeout_seconds=30,
        agent_version="test-agent",
        container_runtime_command="docker",
    )


def _restore_command() -> dict[str, object]:
    return {
        "command_id": "command-1",
        "claim_id": "claim-1",
        "kind": "restore_database",
        "restore_run_id": "restore-run-1",
        "resource_key": "db",
        "execution_context": {
            "database_type": "postgres",
            "container_name": "coolify-db-container",
            "database_name": "app",
            "username": "app",
            "password": "secret",
            "host": "127.0.0.1",
            "port": 5432,
            "artifact_path": "/var/lib/coolify/backups/restore.dump",
        },
        "container_candidates": ["coolify-db-container", "db-uuid-1"],
    }


def test_agent_bootstraps_and_persists_access_token() -> None:
    with TemporaryDirectory() as tmp_dir:
        token_path = Path(tmp_dir) / "agent-token"
        events: list[tuple[str, object]] = []

        class _FakeClient:
            def __init__(self, *, api_base_url: str, access_token: str | None = None) -> None:
                self.api_base_url = api_base_url
                self.access_token = access_token

            def register(self, *, bootstrap_token: str, agent_version: str, advertised_capabilities: tuple[str, ...]) -> dict[str, object]:
                events.append(("register", (bootstrap_token, agent_version, advertised_capabilities)))
                return {"access_token": "access-token-1", "host": {"host_id": "host-1"}}

            def heartbeat(self, *, agent_version: str, advertised_capabilities: tuple[str, ...], state: str) -> dict[str, object]:
                events.append(("heartbeat", (self.access_token, agent_version, tuple(advertised_capabilities), state)))
                return {"host_id": "host-1"}

            def claim_command(self) -> dict[str, object] | None:
                events.append(("claim", self.access_token))
                return None

        agent = DeploymentHostAgent(
            config=_agent_config(access_token_path=token_path, bootstrap_token="bootstrap-token-1"),
            client_factory=_FakeClient,
        )

        result = agent.process_once()

        assert result.processed is False
        assert load_deployment_host_agent_access_token(token_path) == "access-token-1"
        assert events[0][0] == "register"
        assert events[1][0] == "heartbeat"
        assert events[2] == ("claim", "access-token-1")


def test_agent_bootstraps_from_token_file_and_removes_it_after_registration() -> None:
    with TemporaryDirectory() as tmp_dir:
        bootstrap_path = Path(tmp_dir) / "bootstrap-token"
        access_token_path = Path(tmp_dir) / "agent-token"
        bootstrap_path.write_text("bootstrap-token-1", encoding="utf-8")
        events: list[tuple[str, object]] = []

        class _FakeClient:
            def __init__(self, *, api_base_url: str, access_token: str | None = None) -> None:
                self.api_base_url = api_base_url
                self.access_token = access_token

            def register(self, *, bootstrap_token: str, agent_version: str, advertised_capabilities: tuple[str, ...]) -> dict[str, object]:
                events.append(("register", (bootstrap_token, agent_version, advertised_capabilities)))
                return {"access_token": "access-token-1", "host": {"host_id": "host-1"}}

            def heartbeat(self, *, agent_version: str, advertised_capabilities: tuple[str, ...], state: str) -> dict[str, object]:
                events.append(("heartbeat", (self.access_token, agent_version, tuple(advertised_capabilities), state)))
                return {"host_id": "host-1"}

            def claim_command(self) -> dict[str, object] | None:
                events.append(("claim", self.access_token))
                return None

        config = replace(_agent_config(access_token_path=access_token_path), bootstrap_token_path=bootstrap_path)
        agent = DeploymentHostAgent(config=config, client_factory=_FakeClient)

        result = agent.process_once()

        assert result.processed is False
        assert access_token_path.read_text(encoding="utf-8") == "access-token-1"
        assert not bootstrap_path.exists()
        assert events[0][0] == "register"


def test_agent_process_once_executes_restore_and_reports_success() -> None:
    with TemporaryDirectory() as tmp_dir:
        token_path = Path(tmp_dir) / "agent-token"
        token_path.write_text("access-token-1", encoding="utf-8")
        completions: list[tuple[str, dict[str, object], str | None]] = []
        starts: list[tuple[str, str]] = []

        class _FakeClient:
            def __init__(self, *, api_base_url: str, access_token: str | None = None) -> None:
                self.access_token = access_token

            def heartbeat(self, *, agent_version: str, advertised_capabilities: tuple[str, ...], state: str) -> dict[str, object]:
                return {"host_id": "host-1"}

            def claim_command(self) -> dict[str, object] | None:
                return _restore_command()

            def start_command(self, *, command_id: str, claim_id: str) -> dict[str, object]:
                starts.append((command_id, claim_id))
                return {"command_id": command_id}

            def complete_command(
                self,
                *,
                command_id: str,
                claim_id: str,
                status: str,
                result: dict[str, object],
                last_error: str | None,
            ) -> dict[str, object]:
                completions.append((status, result, last_error))
                return {"command_id": command_id, "status": status}

        def _fake_run(command: list[str], **_: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=0, stdout="restore complete", stderr="", command=command)

        agent = DeploymentHostAgent(
            config=_agent_config(access_token_path=token_path),
            client_factory=_FakeClient,
            subprocess_run_fn=_fake_run,
        )

        result = agent.process_once()

        assert result.processed is True
        assert result.command_id == "command-1"
        assert starts == [("command-1", "claim-1")]
        assert completions[0][0] == "succeeded"
        assert completions[0][1]["selected_container"] == "coolify-db-container"
        assert completions[0][2] is None


def test_agent_process_once_reports_failed_restore_when_all_candidates_fail() -> None:
    with TemporaryDirectory() as tmp_dir:
        token_path = Path(tmp_dir) / "agent-token"
        token_path.write_text("access-token-1", encoding="utf-8")
        completions: list[tuple[str, dict[str, object], str | None]] = []

        class _FakeClient:
            def __init__(self, *, api_base_url: str, access_token: str | None = None) -> None:
                self.access_token = access_token

            def heartbeat(self, *, agent_version: str, advertised_capabilities: tuple[str, ...], state: str) -> dict[str, object]:
                return {"host_id": "host-1"}

            def claim_command(self) -> dict[str, object] | None:
                return _restore_command()

            def start_command(self, *, command_id: str, claim_id: str) -> dict[str, object]:
                return {"command_id": command_id}

            def complete_command(
                self,
                *,
                command_id: str,
                claim_id: str,
                status: str,
                result: dict[str, object],
                last_error: str | None,
            ) -> dict[str, object]:
                completions.append((status, result, last_error))
                return {"command_id": command_id, "status": status}

        def _fake_run(command: list[str], **_: object) -> SimpleNamespace:
            return SimpleNamespace(returncode=1, stdout="", stderr=f"failed: {' '.join(command)}")

        agent = DeploymentHostAgent(
            config=_agent_config(access_token_path=token_path),
            client_factory=_FakeClient,
            subprocess_run_fn=_fake_run,
        )

        result = agent.process_once()

        assert result.processed is True
        assert completions[0][0] == "failed"
        assert completions[0][2] == "Restore command failed for every container candidate"
        assert len(completions[0][1]["attempts"]) == 2
