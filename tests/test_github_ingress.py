from __future__ import annotations

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException

from orchestrator.api.webhooks.github_ingress import ingest_github_webhook_event


class GitHubIngressTests(unittest.IsolatedAsyncioTestCase):
    async def _call(self, payload: dict, headers: dict[str, str] | None = None, **overrides):
        request = SimpleNamespace(headers=headers or {})
        session = MagicMock()
        settings = SimpleNamespace(secrets_encryption_key="k")

        base_patches = {
            "_read_json_payload": AsyncMock(return_value=(payload, b"{}")),
            "_resolve_global_github_webhook_secret": MagicMock(return_value=None),
            "_extract_delivery_id": MagicMock(return_value="delivery-1"),
            "_extract_installation_id": MagicMock(return_value=123),
            "_find_tenant_by_installation_id": MagicMock(
                return_value=SimpleNamespace(
                    tenant_id="route25",
                    is_enabled=True,
                    github_config={"installation_id": "123"},
                )
            ),
            "_resolve_tenant_github_webhook_secret": MagicMock(return_value=None),
            "_extract_repository_full_name": MagicMock(return_value="org/repo"),
            "_extract_pull_request_targets": MagicMock(return_value=[(11, True)]),
            "_resolve_active_project_for_repo": MagicMock(return_value=SimpleNamespace(project_id="route25-default")),
            "github_client_from_tenant_config": MagicMock(return_value=MagicMock()),
            "ReviewAgentGate": MagicMock(),
            "send_tenant_discord_message": MagicMock(),
            "enqueue_pr_remediation_if_needed": MagicMock(return_value=None),
            "resolve_scoped_secret_ref": MagicMock(return_value="secret"),
            "_validate_github_webhook_signature": MagicMock(),
        }
        base_patches.update(overrides)

        with ExitStack() as stack:
            for name, value in base_patches.items():
                stack.enter_context(patch(f"orchestrator.api.webhooks.github_ingress.{name}", value))
            return await ingest_github_webhook_event(request=request, session=session, settings=settings, request_id="req-1")

    async def test_ping_event(self) -> None:
        response = await self._call(payload={}, headers={"X-GitHub-Event": "ping"})
        self.assertEqual(response.status_code, 200)

    async def test_missing_installation_id_raises_400(self) -> None:
        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(
                payload={},
                headers={"X-GitHub-Event": "pull_request"},
                _extract_installation_id=MagicMock(return_value=None),
            )
        self.assertEqual(exc_ctx.exception.status_code, 400)

    async def test_unknown_installation(self) -> None:
        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "pull_request"},
            _find_tenant_by_installation_id=MagicMock(return_value=None),
        )
        self.assertEqual(response.status_code, 202)
        self.assertIn("unknown_installation", response.body.decode())

    async def test_tenant_disabled(self) -> None:
        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "pull_request"},
            _find_tenant_by_installation_id=MagicMock(
                return_value=SimpleNamespace(tenant_id="route25", is_enabled=False, github_config={})
            ),
        )
        self.assertEqual(response.status_code, 202)
        self.assertIn("tenant_disabled", response.body.decode())

    async def test_ignored_event(self) -> None:
        response = await self._call(payload={}, headers={"X-GitHub-Event": "issues"})
        self.assertEqual(response.status_code, 202)
        self.assertIn("ignored_event", response.body.decode())

    async def test_missing_pr_context_and_project_not_mapped(self) -> None:
        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "pull_request"},
            _extract_repository_full_name=MagicMock(return_value=None),
            _extract_pull_request_targets=MagicMock(return_value=[]),
        )
        self.assertEqual(response.status_code, 202)
        self.assertIn("missing_pr_context", response.body.decode())

        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "pull_request"},
            _resolve_active_project_for_repo=MagicMock(return_value=None),
        )
        self.assertEqual(response.status_code, 202)
        self.assertIn("project_not_mapped", response.body.decode())

    async def test_review_misconfigured(self) -> None:
        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "pull_request"},
            github_client_from_tenant_config=MagicMock(side_effect=ValueError("bad config")),
        )
        self.assertEqual(response.status_code, 202)
        self.assertIn("review_misconfigured", response.body.decode())

    async def test_review_uses_platform_for_unscoped_github_refs(self) -> None:
        def _scoped_secret_lookup(
            session,
            secret_ref: str,
            encryption_key: str,
            tenant_id: str,
            project_id: str | None = None,
        ) -> str | None:
            if secret_ref == "tenant/route25/GITHUB_APP_ID":
                return "tenant-app-id"
            if secret_ref == "tenant/route25/GITHUB_APP_PRIVATE_KEY":
                return "tenant-private-key"
            return None

        def _platform_secret_lookup(
            session,
            *,
            secret_ref: str,
            encryption_key: str,
            allow_environment_fallback: bool = True,
        ) -> str | None:
            if secret_ref == "GITHUB_APP_ID":
                return "platform-app-id"
            if secret_ref == "GITHUB_APP_PRIVATE_KEY":
                return "platform-private-key"
            return None

        def _github_client_factory(
            config: dict,
            *,
            tenant_secret_lookup=None,
            platform_secret_lookup=None,
        ) -> MagicMock:
            self.assertIsNotNone(tenant_secret_lookup)
            self.assertIsNotNone(platform_secret_lookup)
            self.assertEqual(
                tenant_secret_lookup("tenant/route25/GITHUB_APP_ID"),
                "tenant-app-id",
            )
            self.assertEqual(
                tenant_secret_lookup("tenant/route25/GITHUB_APP_PRIVATE_KEY"),
                "tenant-private-key",
            )
            self.assertEqual(platform_secret_lookup("GITHUB_APP_ID"), "platform-app-id")
            self.assertEqual(platform_secret_lookup("GITHUB_APP_PRIVATE_KEY"), "platform-private-key")

            def _evaluate_pr(*, repo_full_name: str, pr_number: int):
                _ = repo_full_name, pr_number
                return SimpleNamespace(
                    ready=True,
                    state="ready",
                    message="ready",
                )

            return SimpleNamespace(
                evaluate_pr=_evaluate_pr
            )

        response = await self._call(
            payload={"action": "synchronize"},
            headers={"X-GitHub-Event": "pull_request"},
            resolve_scoped_secret_ref=MagicMock(side_effect=_scoped_secret_lookup),
            resolve_platform_secret_ref=MagicMock(side_effect=_platform_secret_lookup),
            github_client_from_tenant_config=_github_client_factory,
            ReviewAgentGate=MagicMock(
                return_value=MagicMock(
                    evaluate_pr=MagicMock(
                        return_value=SimpleNamespace(
                            ready=True,
                            state="ready",
                            message="ready",
                        )
                    )
                )
            ),
        )
        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"accepted":true', body)

    async def test_signal_generation_with_failures(self) -> None:
        gate = MagicMock()
        gate.evaluate_pr.side_effect = [
            ValueError("boom"),
            SimpleNamespace(ready=True, state="ready", message="Need review"),
        ]
        review_gate_cls = MagicMock(return_value=gate)
        send_discord = MagicMock()

        response = await self._call(
            payload={"action": "synchronize"},
            headers={"X-GitHub-Event": "pull_request"},
            _extract_pull_request_targets=MagicMock(return_value=[(10, True), (11, False)]),
            ReviewAgentGate=review_gate_cls,
            send_tenant_discord_message=send_discord,
        )

        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"accepted":true', body)
        self.assertIn('"signals"', body)
        send_discord.assert_called_once()

    async def test_signature_validation_paths(self) -> None:
        validate = MagicMock()
        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "issues"},
            _resolve_global_github_webhook_secret=MagicMock(return_value="global"),
            _validate_github_webhook_signature=validate,
        )
        self.assertEqual(response.status_code, 202)
        validate.assert_called_once()

        validate = MagicMock()
        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "issues"},
            _resolve_global_github_webhook_secret=MagicMock(return_value=None),
            _resolve_tenant_github_webhook_secret=MagicMock(return_value="tenant"),
            _validate_github_webhook_signature=validate,
        )
        self.assertEqual(response.status_code, 202)
        validate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
