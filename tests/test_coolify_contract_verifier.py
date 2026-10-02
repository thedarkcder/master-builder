from __future__ import annotations

import importlib.util
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import patch


def _load_module():
    module_path = (
        Path(__file__).resolve().parents[1] / "scripts" / "verify_coolify_contract.py"
    )
    spec = importlib.util.spec_from_file_location(
        "verify_coolify_contract", module_path
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Failed to load verify_coolify_contract module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeCoolifyClient:
    def __init__(self, module, *, deployment_statuses: list[str] | None = None) -> None:
        self._module = module
        self.deployment_statuses = deployment_statuses or ["success"]
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.application_uuid = "app-uuid-1"
        self.deployment_uuid = "deployment-uuid-1"
        self.project_payload = {"uuid": "project-uuid-1"}
        self.environment_payload = {"name": "production"}
        self.server_payload = {"uuid": "server-uuid-1"}
        self.application_payload = {
            "uuid": self.application_uuid,
            "description": "initial description",
        }
        self.env_rows = [
            {"key": "MB_CONTRACT_RUN_ID"},
            {"key": "MB_CONTRACT_VERIFICATION"},
        ]
        self.deployment_history = [{"uuid": self.deployment_uuid}]

    def get_project(self, project_uuid: str) -> dict[str, Any]:
        self.calls.append(("get_project", (project_uuid,), {}))
        return dict(self.project_payload)

    def get_environment(
        self, project_uuid: str, environment_name: str
    ) -> dict[str, Any]:
        self.calls.append(("get_environment", (project_uuid, environment_name), {}))
        return dict(self.environment_payload)

    def get_server(self, server_uuid: str) -> dict[str, Any]:
        self.calls.append(("get_server", (server_uuid,), {}))
        return dict(self.server_payload)

    def create_public_application(self, *, payload: dict[str, Any]) -> str:
        self.calls.append(("create_public_application", (), payload))
        return self.application_uuid

    def get_application(self, application_uuid: str) -> dict[str, Any]:
        self.calls.append(("get_application", (application_uuid,), {}))
        return dict(self.application_payload)

    def update_application(
        self, *, application_uuid: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.calls.append(("update_application", (application_uuid,), payload))
        self.application_payload = {**self.application_payload, **payload}
        return dict(self.application_payload)

    def bulk_update_application_envs(
        self, *, application_uuid: str, envs: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.calls.append(
            ("bulk_update_application_envs", (application_uuid,), {"data": envs})
        )
        return {"message": "ok"}

    def list_application_envs(self, application_uuid: str) -> list[dict[str, Any]]:
        self.calls.append(("list_application_envs", (application_uuid,), {}))
        return list(self.env_rows)

    def start_application(
        self, application_uuid: str, *, force: bool, instant_deploy: bool
    ) -> str:
        self.calls.append(
            (
                "start_application",
                (application_uuid,),
                {"force": force, "instant_deploy": instant_deploy},
            )
        )
        return self.deployment_uuid

    def get_deployment(self, deployment_uuid: str) -> dict[str, Any]:
        self.calls.append(("get_deployment", (deployment_uuid,), {}))
        status = (
            self.deployment_statuses.pop(0) if self.deployment_statuses else "success"
        )
        return {"deployment_uuid": deployment_uuid, "status": status}

    def list_application_deployments(
        self, application_uuid: str
    ) -> list[dict[str, Any]]:
        self.calls.append(("list_application_deployments", (application_uuid,), {}))
        return list(self.deployment_history)

    def delete_application(self, application_uuid: str) -> dict[str, Any]:
        self.calls.append(("delete_application", (application_uuid,), {}))
        return {"message": "Application deleted."}


class CoolifyContractVerifierTests(unittest.TestCase):
    def test_parse_args_requires_execute_guardrail(self) -> None:
        module = _load_module()
        with patch.object(sys, "argv", ["verify_coolify_contract.py"]):
            args = module.parse_args()
        self.assertFalse(args.execute)

    def test_load_config_from_env_fails_closed_when_required_env_missing(self) -> None:
        module = _load_module()
        args = module.parse_args(["--execute"])
        with self.assertRaises(module.CoolifyVerificationError):
            module.load_config_from_env(args=args, environ={})

    def test_verify_contract_runs_success_path_and_cleans_up(self) -> None:
        module = _load_module()
        args = module.parse_args(["--execute"])
        config = module.load_config_from_env(
            args=args,
            environ={
                "COOLIFY_VERIFY_BASE_URL": "https://coolify.example.com/api/v1",
                "COOLIFY_VERIFY_API_TOKEN": "token",
                "COOLIFY_VERIFY_PROJECT_UUID": "project-uuid-1",
                "COOLIFY_VERIFY_SERVER_UUID": "server-uuid-1",
                "COOLIFY_VERIFY_DESTINATION_UUID": "destination-uuid-1",
                "COOLIFY_VERIFY_REPOSITORY": "https://github.com/example/repo",
                "COOLIFY_VERIFY_TIMEOUT_SECONDS": "2",
                "COOLIFY_VERIFY_POLL_INTERVAL_SECONDS": "1",
                "COOLIFY_VERIFY_APP_NAME": "temp-contract-app",
            },
        )
        fake_client = FakeCoolifyClient(module)

        exit_code, checks, summary = module.verify_contract(
            config=config,
            client=fake_client,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(all(check.ok for check in checks))
        self.assertEqual(summary["ok"], True)
        self.assertEqual(summary["application_uuid"], "app-uuid-1")
        self.assertEqual(summary["deployment_uuid"], "deployment-uuid-1")
        self.assertEqual(summary["checks"][-1]["name"], "cleanup")
        self.assertEqual(summary["checks"][-1]["ok"], True)
        self.assertEqual(
            [call[0] for call in fake_client.calls],
            [
                "get_project",
                "get_environment",
                "get_server",
                "create_public_application",
                "get_application",
                "update_application",
                "get_application",
                "bulk_update_application_envs",
                "list_application_envs",
                "start_application",
                "get_deployment",
                "list_application_deployments",
                "delete_application",
            ],
        )

    def test_verify_contract_marks_terminal_failure_as_failed(self) -> None:
        module = _load_module()
        args = module.parse_args(["--execute"])
        config = module.load_config_from_env(
            args=args,
            environ={
                "COOLIFY_VERIFY_BASE_URL": "https://coolify.example.com/api/v1",
                "COOLIFY_VERIFY_API_TOKEN": "token",
                "COOLIFY_VERIFY_PROJECT_UUID": "project-uuid-1",
                "COOLIFY_VERIFY_SERVER_UUID": "server-uuid-1",
                "COOLIFY_VERIFY_DESTINATION_UUID": "destination-uuid-1",
                "COOLIFY_VERIFY_REPOSITORY": "https://github.com/example/repo",
                "COOLIFY_VERIFY_TIMEOUT_SECONDS": "2",
                "COOLIFY_VERIFY_POLL_INTERVAL_SECONDS": "1",
            },
        )
        fake_client = FakeCoolifyClient(
            module, deployment_statuses=["running", "failed"]
        )

        exit_code, checks, summary = module.verify_contract(
            config=config,
            client=fake_client,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(exit_code, 1)
        self.assertFalse(summary["ok"])
        self.assertTrue(
            any(check.name == "deployment status" and not check.ok for check in checks)
        )

    def test_main_prints_refusal_without_execute(self) -> None:
        module = _load_module()
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = module.main([])
        self.assertEqual(exit_code, 2)
        self.assertIn("Refusing to execute without --execute.", output.getvalue())

    def test_main_prints_json_summary(self) -> None:
        module = _load_module()
        output = io.StringIO()
        fake_client = FakeCoolifyClient(module)
        with (
            patch.object(module, "CoolifyHttpClient", return_value=fake_client),
            patch.object(
                sys,
                "argv",
                [
                    "verify_coolify_contract.py",
                    "--execute",
                    "--keep-application",
                    "--timeout-seconds",
                    "2",
                    "--poll-interval-seconds",
                    "1",
                ],
            ),
            patch.dict(
                module.os.environ,
                {
                    "COOLIFY_VERIFY_BASE_URL": "https://coolify.example.com/api/v1",
                    "COOLIFY_VERIFY_API_TOKEN": "token",
                    "COOLIFY_VERIFY_PROJECT_UUID": "project-uuid-1",
                    "COOLIFY_VERIFY_SERVER_UUID": "server-uuid-1",
                    "COOLIFY_VERIFY_DESTINATION_UUID": "destination-uuid-1",
                    "COOLIFY_VERIFY_REPOSITORY": "https://github.com/example/repo",
                },
                clear=False,
            ),
            redirect_stdout(output),
        ):
            exit_code = module.main(
                None,
            )

        self.assertEqual(exit_code, 0)
        lines = [line for line in output.getvalue().splitlines() if line.strip()]
        self.assertTrue(lines[-1].startswith("{"))
        summary = json.loads(lines[-1])
        self.assertTrue(summary["ok"])
        self.assertEqual(summary["keep_application"], True)


if __name__ == "__main__":
    unittest.main()
