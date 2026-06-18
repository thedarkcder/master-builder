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
        capabilities=("restore_database", "postgres", "local_preview_routes"),
        heartbeat_interval_seconds=30,
        poll_interval_seconds=1,
        command_timeout_seconds=30,
        agent_version="test-agent",
        container_runtime_command="docker",
        local_preview_proxy_dynamic_dir=None,
    )


def _restore_command() -> dict[str, object]:
    return {
        "command_id": "command-1",
        "claim_id": "claim-1",
        "kind": "restore_database",
        "restore_run_id": "restore-run-1",
        "payload": {
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
        },
    }


def _local_preview_route_command() -> dict[str, object]:
    return {
        "command_id": "command-2",
        "claim_id": "claim-2",
        "kind": "sync_local_preview_routes",
        "payload": {
            "action": "upsert",
            "release_id": "release-1",
            "deployment_uuid": "deployment-1",
            "application_uuid": "app-uuid-1",
            "route_bindings": [
                {
                    "service_key": "admin-website",
                    "host": "admin.preview.192-168-0-118.sslip.io",
                    "port": "80",
                },
                {
                    "service_key": "management-api",
                    "host": "management.preview.192-168-0-118.sslip.io",
                    "port": "8080",
                },
            ],
        },
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
        assert bootstrap_path.read_text(encoding="utf-8") == "bootstrap-token-1"
        assert events[0][0] == "register"


def test_agent_reauthenticates_with_bootstrap_token_when_access_token_is_rejected() -> None:
    with TemporaryDirectory() as tmp_dir:
        bootstrap_path = Path(tmp_dir) / "bootstrap-token"
        access_token_path = Path(tmp_dir) / "agent-token"
        bootstrap_path.write_text("bootstrap-token-1", encoding="utf-8")
        access_token_path.write_text("stale-access-token", encoding="utf-8")
        events: list[tuple[str, object]] = []

        class _FakeClient:
            def __init__(self, *, api_base_url: str, access_token: str | None = None) -> None:
                self.api_base_url = api_base_url
                self.access_token = access_token

            def register(self, *, bootstrap_token: str, agent_version: str, advertised_capabilities: tuple[str, ...]) -> dict[str, object]:
                events.append(("register", (bootstrap_token, agent_version, advertised_capabilities)))
                return {"access_token": "fresh-access-token", "host": {"host_id": "host-1"}}

            def heartbeat(self, *, agent_version: str, advertised_capabilities: tuple[str, ...], state: str) -> dict[str, object]:
                events.append(("heartbeat", self.access_token))
                if self.access_token == "stale-access-token":
                    from orchestrator.core.deployment_host_agent_runtime import DeploymentHostControlPlaneError

                    raise DeploymentHostControlPlaneError("unauthorized", status_code=401)
                return {"host_id": "host-1"}

            def claim_command(self) -> dict[str, object] | None:
                events.append(("claim", self.access_token))
                return None

        config = replace(_agent_config(access_token_path=access_token_path), bootstrap_token_path=bootstrap_path)
        agent = DeploymentHostAgent(config=config, client_factory=_FakeClient)

        result = agent.process_once()

        assert result.processed is False
        assert access_token_path.read_text(encoding="utf-8") == "fresh-access-token"
        assert ("heartbeat", "stale-access-token") in events
        assert ("register", ("bootstrap-token-1", "test-agent", ("restore_database", "postgres", "local_preview_routes"))) in events
        assert ("claim", "fresh-access-token") in events


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


def test_agent_process_once_rejects_app_deployment_commands() -> None:
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
                return {
                    "command_id": "command-1",
                    "claim_id": "claim-1",
                    "kind": "deploy_docker_compose",
                    "payload": {"source_path": "api/docker"},
                }

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

        agent = DeploymentHostAgent(
            config=_agent_config(access_token_path=token_path),
            client_factory=_FakeClient,
        )

        result = agent.process_once()

        assert result.processed is True
        assert result.command_id == "command-1"
        assert starts == [("command-1", "claim-1")]
        assert completions[0][0] == "failed"
        assert completions[0][1] == {"kind": "deploy_docker_compose"}
        assert completions[0][2] == "Unsupported deployment host command kind 'deploy_docker_compose'"


def test_agent_process_once_syncs_local_preview_routes() -> None:
    with TemporaryDirectory() as tmp_dir:
        token_path = Path(tmp_dir) / "agent-token"
        token_path.write_text("access-token-1", encoding="utf-8")
        proxy_dir = Path(tmp_dir) / "proxy"
        completions: list[tuple[str, dict[str, object], str | None]] = []
        starts: list[tuple[str, str]] = []

        class _FakeClient:
            def __init__(self, *, api_base_url: str, access_token: str | None = None) -> None:
                self.access_token = access_token

            def heartbeat(self, *, agent_version: str, advertised_capabilities: tuple[str, ...], state: str) -> dict[str, object]:
                return {"host_id": "host-1"}

            def claim_command(self) -> dict[str, object] | None:
                return _local_preview_route_command()

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
            if command[:4] == ["docker", "ps", "-q", "--filter"]:
                return SimpleNamespace(returncode=0, stdout="container-1\ncontainer-2\n", stderr="")
            if command[:2] == ["docker", "inspect"]:
                payload = [
                    {
                        "Config": {"Labels": {"com.docker.compose.service": "admin-website"}},
                        "NetworkSettings": {"Networks": {"coolify": {"IPAddress": "172.20.0.18"}}},
                    },
                    {
                        "Config": {"Labels": {"com.docker.compose.service": "management-api"}},
                        "NetworkSettings": {"Networks": {"coolify": {"IPAddress": "172.20.0.7"}}},
                    },
                ]
                return SimpleNamespace(returncode=0, stdout=__import__("json").dumps(payload), stderr="")
            raise AssertionError(f"Unexpected docker command: {command}")

        agent = DeploymentHostAgent(
            config=replace(_agent_config(access_token_path=token_path), local_preview_proxy_dynamic_dir=proxy_dir),
            client_factory=_FakeClient,
            subprocess_run_fn=_fake_run,
        )

        result = agent.process_once()

        assert result.processed is True
        assert result.command_id == "command-2"
        assert starts == [("command-2", "claim-2")]
        assert completions[0][0] == "succeeded"
        assert completions[0][2] is None
        route_file = proxy_dir / "mb-preview-release-.yaml"
        assert route_file.exists()
        route_yaml = route_file.read_text(encoding="utf-8")
        assert "admin.preview.192-168-0-118.sslip.io" in route_yaml
        assert "http://172.20.0.18:80" in route_yaml
        assert "http://172.20.0.7:8080" in route_yaml


def test_agent_process_once_removes_stale_local_preview_routes_for_same_host() -> None:
    with TemporaryDirectory() as tmp_dir:
        token_path = Path(tmp_dir) / "agent-token"
        token_path.write_text("access-token-1", encoding="utf-8")
        proxy_dir = Path(tmp_dir) / "proxy"
        proxy_dir.mkdir()
        stale_file = proxy_dir / "mb-preview-stale123.yaml"
        stale_file.write_text(
            "http:\n"
            "  routers:\n"
            "    stale:\n"
            "      entryPoints: [http]\n"
            "      rule: Host(`app.preview.example.test`) && PathPrefix(`/`)\n"
            "      service: stale\n"
            "  services:\n"
            "    stale:\n"
            "      loadBalancer:\n"
            "        servers:\n"
            "        - url: http://172.20.0.2:19006\n",
            encoding="utf-8",
        )
        unrelated_file = proxy_dir / "mb-preview-keep123.yaml"
        unrelated_file.write_text(
            "http:\n"
            "  routers:\n"
            "    keep:\n"
            "      entryPoints: [http]\n"
            "      rule: Host(`other.preview.example.test`) && PathPrefix(`/`)\n"
            "      service: keep\n",
            encoding="utf-8",
        )
        completions: list[tuple[str, dict[str, object], str | None]] = []

        class _FakeClient:
            def __init__(self, *, api_base_url: str, access_token: str | None = None) -> None:
                self.access_token = access_token

            def heartbeat(self, *, agent_version: str, advertised_capabilities: tuple[str, ...], state: str) -> dict[str, object]:
                return {"host_id": "host-1"}

            def claim_command(self) -> dict[str, object] | None:
                command = _local_preview_route_command()
                command["payload"]["release_id"] = "release-current"
                command["payload"]["application_uuid"] = "app-uuid-current"
                command["payload"]["route_bindings"] = [
                    {
                        "service_key": "repo",
                        "host": "app.preview.example.test",
                        "port": "19006",
                    }
                ]
                return command

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
            if command[:4] == ["docker", "ps", "-q", "--filter"]:
                return SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            if command[:2] == ["docker", "inspect"]:
                payload = [
                    {
                        "Config": {"Labels": {"com.docker.compose.service": "repo"}},
                        "NetworkSettings": {"Networks": {"coolify": {"IPAddress": "172.20.0.21"}}},
                    }
                ]
                return SimpleNamespace(returncode=0, stdout=__import__("json").dumps(payload), stderr="")
            raise AssertionError(f"Unexpected docker command: {command}")

        agent = DeploymentHostAgent(
            config=replace(_agent_config(access_token_path=token_path), local_preview_proxy_dynamic_dir=proxy_dir),
            client_factory=_FakeClient,
            subprocess_run_fn=_fake_run,
        )

        result = agent.process_once()

        assert result.processed is True
        assert completions[0][0] == "succeeded"
        assert not stale_file.exists()
        assert unrelated_file.exists()
        route_file = proxy_dir / "mb-preview-release-.yaml"
        assert route_file.exists()
        route_yaml = route_file.read_text(encoding="utf-8")
        assert "app.preview.example.test" in route_yaml
        assert "http://172.20.0.21:19006" in route_yaml


def test_agent_process_once_syncs_single_app_local_preview_route_when_coolify_service_label_is_generated() -> None:
    with TemporaryDirectory() as tmp_dir:
        token_path = Path(tmp_dir) / "agent-token"
        token_path.write_text("access-token-1", encoding="utf-8")
        proxy_dir = Path(tmp_dir) / "proxy"
        completions: list[tuple[str, dict[str, object], str | None]] = []

        class _FakeClient:
            def __init__(self, *, api_base_url: str, access_token: str | None = None) -> None:
                self.access_token = access_token

            def heartbeat(self, *, agent_version: str, advertised_capabilities: tuple[str, ...], state: str) -> dict[str, object]:
                return {"host_id": "host-1"}

            def claim_command(self) -> dict[str, object] | None:
                command = _local_preview_route_command()
                command["payload"]["release_id"] = "release-single"
                command["payload"]["application_uuid"] = "app-uuid-single"
                command["payload"]["route_bindings"] = [
                    {
                        "service_key": "repo",
                        "host": "app.preview.192-168-0-118.sslip.io",
                        "port": "19006",
                    }
                ]
                return command

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
            if command[:4] == ["docker", "ps", "-q", "--filter"]:
                return SimpleNamespace(returncode=0, stdout="container-1\n", stderr="")
            if command[:2] == ["docker", "inspect"]:
                payload = [
                    {
                        "Config": {"Labels": {"com.docker.compose.service": "app-uuid-single-123456"}},
                        "NetworkSettings": {"Networks": {"coolify": {"IPAddress": "172.20.0.21"}}},
                    }
                ]
                return SimpleNamespace(returncode=0, stdout=__import__("json").dumps(payload), stderr="")
            raise AssertionError(f"Unexpected docker command: {command}")

        agent = DeploymentHostAgent(
            config=replace(_agent_config(access_token_path=token_path), local_preview_proxy_dynamic_dir=proxy_dir),
            client_factory=_FakeClient,
            subprocess_run_fn=_fake_run,
        )

        result = agent.process_once()

        assert result.processed is True
        assert completions[0][0] == "succeeded"
        assert completions[0][2] is None
        route_file = proxy_dir / "mb-preview-release-.yaml"
        route_yaml = route_file.read_text(encoding="utf-8")
        assert "app.preview.192-168-0-118.sslip.io" in route_yaml
        assert "http://172.20.0.21:19006" in route_yaml
