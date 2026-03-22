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

        context_patches = {
            "read_json_payload": AsyncMock(return_value=(payload, b"{}")),
            "resolve_global_github_webhook_secret": MagicMock(return_value=None),
            "extract_delivery_id": MagicMock(return_value="delivery-1"),
            "extract_installation_id": MagicMock(return_value=123),
            "find_tenant_by_installation_id": MagicMock(
                return_value=SimpleNamespace(
                    tenant_id="route25",
                    is_enabled=True,
                    jira_config={},
                    github_config={"installation_id": "123"},
                )
            ),
            "resolve_tenant_github_webhook_secret": MagicMock(return_value=None),
            "extract_repository_full_name": MagicMock(return_value="org/repo"),
            "extract_pull_request_targets": MagicMock(return_value=[(11, True)]),
            "resolve_active_project_for_repo": MagicMock(
                return_value=SimpleNamespace(
                    project_id="route25-default",
                    jira_project_key="GP",
                    github_repository="org/repo",
                )
            ),
            "github_client_from_tenant_config": MagicMock(return_value=MagicMock()),
            "ReviewAgentGate": MagicMock(),
            "resolve_scoped_secret_ref": MagicMock(return_value="secret"),
            "resolve_platform_secret_ref": MagicMock(return_value="platform-secret"),
            "validate_github_webhook_signature": MagicMock(),
        }
        application_patches = {
            "enqueue_pr_remediation_if_needed": MagicMock(return_value=None),
            "evaluate_pr_review_findings": MagicMock(
                return_value=SimpleNamespace(state="reviewed", summary="no findings", findings=())
            ),
            "tenant_jira_issue_url": MagicMock(return_value=None),
        }
        github_executor_patches = {
            "upsert_sticky_review_comment": MagicMock(return_value=SimpleNamespace(action="updated", comment_id=1)),
            "upsert_sticky_remediation_comment": MagicMock(
                return_value=SimpleNamespace(action="updated", comment_id=2)
            ),
            "publish_inline_review_batch": MagicMock(
                return_value=SimpleNamespace(submitted=False, review_id=None, inline_count=0)
            ),
            "upsert_manual_fix_followup_comment": MagicMock(
                return_value=SimpleNamespace(action="updated", comment_id=3)
            ),
        }
        discord_executor_patches = {
            "send_tenant_discord_message": MagicMock(),
        }
        classifier_patches = {
            "resolve_effective_policy": MagicMock(
                return_value={
                    "allow_auto_merge": False,
                    "allow_code_reviews": True,
                    "allow_pr_remediation": True,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        }
        for name, value in overrides.items():
            if name in context_patches:
                context_patches[name] = value
            elif name in classifier_patches:
                classifier_patches[name] = value
            elif name in application_patches:
                application_patches[name] = value
            elif name in github_executor_patches:
                github_executor_patches[name] = value
            elif name in discord_executor_patches:
                discord_executor_patches[name] = value
            else:
                raise AssertionError(f"Unhandled github ingress test override: {name}")

        with ExitStack() as stack:
            for name, value in context_patches.items():
                stack.enter_context(patch(f"orchestrator.api.webhooks.github_webhook_context.{name}", value))
            for name, value in classifier_patches.items():
                stack.enter_context(patch(f"orchestrator.api.webhooks.github_event_classifier.{name}", value))
            for name, value in application_patches.items():
                stack.enter_context(patch(f"orchestrator.api.webhooks.github_application.{name}", value))
            for name, value in github_executor_patches.items():
                stack.enter_context(patch(f"orchestrator.core.github.transport_executor.{name}", value))
            for name, value in discord_executor_patches.items():
                stack.enter_context(patch(f"orchestrator.core.discord.transport_executor.{name}", value))
            return await ingest_github_webhook_event(request=request, session=session, settings=settings, request_id="req-1")

    async def test_ping_event(self) -> None:
        response = await self._call(payload={}, headers={"X-GitHub-Event": "ping"})
        self.assertEqual(response.status_code, 200)

    async def test_missing_installation_id_raises_400(self) -> None:
        with self.assertRaises(HTTPException) as exc_ctx:
            await self._call(
                payload={},
                headers={"X-GitHub-Event": "pull_request"},
                extract_installation_id=MagicMock(return_value=None),
            )
        self.assertEqual(exc_ctx.exception.status_code, 400)

    async def test_unknown_installation(self) -> None:
        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "pull_request"},
            find_tenant_by_installation_id=MagicMock(return_value=None),
        )
        self.assertEqual(response.status_code, 202)
        self.assertIn("unknown_installation", response.body.decode())

    async def test_tenant_disabled(self) -> None:
        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "pull_request"},
            find_tenant_by_installation_id=MagicMock(
                return_value=SimpleNamespace(tenant_id="route25", is_enabled=False, jira_config={}, github_config={})
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
            extract_repository_full_name=MagicMock(return_value=None),
            extract_pull_request_targets=MagicMock(return_value=[]),
        )
        self.assertEqual(response.status_code, 202)
        self.assertIn("missing_pr_context", response.body.decode())

        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "pull_request"},
            resolve_active_project_for_repo=MagicMock(return_value=None),
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

    async def test_code_review_can_be_disabled(self) -> None:
        response = await self._call(
            payload={"action": "synchronize"},
            headers={"X-GitHub-Event": "pull_request"},
            github_client_from_tenant_config=MagicMock(side_effect=ValueError("should_not_be_called")),
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": True,
                    "allow_code_reviews": False,
                    "allow_pr_remediation": True,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )
        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"accepted":true', body)
        self.assertIn('"reason":"code_reviews_disabled"', body)
        self.assertIn('"pr_review":{"enabled":false', body)
        self.assertIn('"pr_remediation":{"enabled":false', body)
        self.assertIn('"manual_fix_requests_enabled":true', body)

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
            extract_pull_request_targets=MagicMock(return_value=[(10, True), (11, False)]),
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
            resolve_global_github_webhook_secret=MagicMock(return_value="global"),
            validate_github_webhook_signature=validate,
        )
        self.assertEqual(response.status_code, 202)
        validate.assert_called_once()

    async def test_remediation_posts_mapping_comment_when_triggered(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="example",
            body="desc",
            head_ref="feature/branch",
            base_ref="main",
            html_url="https://github.com/org/repo/pull/11",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=False, state="pending_checks", message="pending")
        remediation_result = SimpleNamespace(
            triggered=True,
            issue_key="GP-900",
            issue_created=True,
            enqueued=True,
            reason=None,
            run=SimpleNamespace(run_id="run-900"),
            head_sha="abc123",
        )
        upsert_remediation_comment = MagicMock(return_value=SimpleNamespace(action="updated", comment_id=77))

        response = await self._call(
            payload={"action": "created"},
            headers={"X-GitHub-Event": "pull_request_review_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            enqueue_pr_remediation_if_needed=MagicMock(return_value=remediation_result),
            upsert_sticky_remediation_comment=upsert_remediation_comment,
            tenant_jira_issue_url=MagicMock(return_value="https://jira.example.com/browse/GP-900"),
        )
        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"issue_key":"GP-900"', body)

    async def test_review_comment_event_routes_to_remediation_without_full_review_publication(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="example",
            body="desc",
            head_ref="feature/branch",
            base_ref="main",
            html_url="https://github.com/org/repo/pull/11",
        )
        enqueue = MagicMock(return_value=None)
        upsert_review_comment = MagicMock()
        publish_inline = MagicMock()

        response = await self._call(
            payload={
                "action": "created",
                "comment": {"id": 123, "body": "nit: can you rename this?", "user": {"login": "alice", "type": "User"}},
                "sender": {"login": "alice", "type": "User"},
                "issue": {"pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
            },
            headers={"X-GitHub-Event": "pull_request_review_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            enqueue_pr_remediation_if_needed=enqueue,
            upsert_sticky_review_comment=upsert_review_comment,
            publish_inline_review_batch=publish_inline,
        )

        self.assertEqual(response.status_code, 202)
        enqueue.assert_called_once()
        upsert_review_comment.assert_not_called()
        publish_inline.assert_not_called()

    async def test_bot_authored_review_comment_is_ignored(self) -> None:
        enqueue = MagicMock()
        upsert_review_comment = MagicMock()
        publish_inline = MagicMock()

        response = await self._call(
            payload={
                "action": "created",
                "comment": {
                    "id": 456,
                    "body": "Codex inline review findings.",
                    "user": {"login": "route25-master-builder[bot]", "type": "Bot"},
                },
                "sender": {"login": "route25-master-builder[bot]", "type": "Bot"},
                "issue": {"pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
            },
            headers={"X-GitHub-Event": "pull_request_review_comment"},
            enqueue_pr_remediation_if_needed=enqueue,
            upsert_sticky_review_comment=upsert_review_comment,
            publish_inline_review_batch=publish_inline,
        )

        self.assertEqual(response.status_code, 202)
        self.assertIn('"ignored_reason":"bot_authored"', response.body.decode())
        enqueue.assert_not_called()
        upsert_review_comment.assert_not_called()
        publish_inline.assert_not_called()

    async def test_remediation_trigger_with_missing_run_is_null_safe(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="example",
            body="desc",
            head_ref="feature/branch",
            base_ref="main",
            html_url="https://github.com/org/repo/pull/11",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=False, state="pending_checks", message="pending")
        remediation_result = SimpleNamespace(
            triggered=True,
            issue_key="GP-901",
            issue_created=False,
            enqueued=False,
            reason="jira_bug_create_failed:token",
            run=None,
            head_sha="abc123",
        )

        response = await self._call(
            payload={"action": "created"},
            headers={"X-GitHub-Event": "pull_request_review_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            enqueue_pr_remediation_if_needed=MagicMock(return_value=remediation_result),
            tenant_jira_issue_url=MagicMock(return_value="https://jira.example.com/browse/GP-901"),
        )
        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"issue_key":"GP-901"', body)
        self.assertIn('"run_id":null', body)

    async def test_pr_remediation_can_be_disabled(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="example",
            body="desc",
            head_ref="feature/branch",
            base_ref="main",
            html_url="https://github.com/org/repo/pull/11",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=False, state="pending_checks", message="pending")
        enqueue_remediation = MagicMock()
        upsert_remediation_comment = MagicMock()

        response = await self._call(
            payload={"action": "created"},
            headers={"X-GitHub-Event": "pull_request_review_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            enqueue_pr_remediation_if_needed=enqueue_remediation,
            upsert_sticky_remediation_comment=upsert_remediation_comment,
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": False,
                    "allow_code_reviews": True,
                    "allow_pr_remediation": False,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )

        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"reason":"pr_remediation_disabled"', body)
        self.assertIn('"enabled":false', body)
        enqueue_remediation.assert_not_called()
        upsert_remediation_comment.assert_not_called()

    async def test_fail_closed_when_findings_evaluation_errors(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="MAB-1: example",
            body="desc",
        )
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=True, state="ready", message="Need review")

        response = await self._call(
            payload={"action": "synchronize"},
            headers={"X-GitHub-Event": "pull_request"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            evaluate_pr_review_findings=MagicMock(side_effect=RuntimeError("codex failed")),
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": True,
                    "allow_code_reviews": True,
                    "allow_pr_remediation": True,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )

        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"findings_evaluated":false', body)
        self.assertIn('"green":false', body)
        github_client.merge_pull_request.assert_not_called()

    async def test_issue_comment_manual_fix_enqueues_even_when_pr_is_green(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="MAB-1: example",
            body="desc",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=True, state="ready", message="green")
        enqueue_remediation = MagicMock(
            return_value=SimpleNamespace(
                triggered=True,
                issue_key="GP-222",
                issue_created=False,
                enqueued=True,
                reason=None,
                run=SimpleNamespace(run_id="run-222"),
                head_sha="abc123",
            )
        )

        response = await self._call(
            payload={
                "action": "created",
                "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
                "comment": {"body": "@mb fix https://github.com/org/repo/pull/11#discussion_r222"},
            },
            headers={"X-GitHub-Event": "issue_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            enqueue_pr_remediation_if_needed=enqueue_remediation,
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": False,
                    "allow_code_reviews": True,
                    "allow_pr_remediation": True,
                    "allow_manual_pr_fix_requests": True,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )

        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"run_id":"run-222"', body)
        enqueue_remediation.assert_called_once()

    async def test_issue_comment_manual_fix_enqueues_when_code_reviews_and_auto_remediation_are_disabled(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="MAB-1: example",
            body="desc",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=True, state="ready", message="green")
        enqueue_remediation = MagicMock(
            return_value=SimpleNamespace(
                triggered=True,
                issue_key="GP-333",
                issue_created=False,
                enqueued=True,
                reason=None,
                run=SimpleNamespace(run_id="run-333"),
                head_sha="abc123",
            )
        )

        response = await self._call(
            payload={
                "action": "created",
                "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
                "comment": {
                    "id": 502,
                    "body": "@mb fix https://github.com/org/repo/pull/11#discussion_r333",
                    "html_url": "https://github.com/org/repo/pull/11#issuecomment-502",
                },
            },
            headers={"X-GitHub-Event": "issue_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            enqueue_pr_remediation_if_needed=enqueue_remediation,
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": False,
                    "allow_code_reviews": False,
                    "allow_pr_remediation": False,
                    "allow_manual_pr_fix_requests": True,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )

        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"run_id":"run-333"', body)
        self.assertNotIn('"reason":"code_reviews_disabled"', body)
        enqueue_remediation.assert_called_once()
        github_client.add_issue_comment_reaction.assert_called_once_with(
            repo_full_name="org/repo",
            comment_id=502,
            content="eyes",
        )

    async def test_issue_comment_manual_fix_adds_eyes_reaction(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="MAB-1: example",
            body="desc",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=True, state="ready", message="green")

        response = await self._call(
            payload={
                "action": "created",
                "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
                "comment": {"id": 501, "body": "@mb fix", "html_url": "https://github.com/org/repo/pull/11#issuecomment-501"},
            },
            headers={"X-GitHub-Event": "issue_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": False,
                    "allow_code_reviews": True,
                    "allow_pr_remediation": False,
                    "allow_manual_pr_fix_requests": True,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )

        self.assertEqual(response.status_code, 202)
        github_client.add_issue_comment_reaction.assert_called_once_with(
            repo_full_name="org/repo",
            comment_id=501,
            content="eyes",
        )

    async def test_issue_comment_without_manual_fix_skips_eyes_reaction(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="MAB-1: example",
            body="desc",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=True, state="ready", message="green")

        response = await self._call(
            payload={
                "action": "created",
                "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
                "comment": {"id": 501, "body": "@mb please handle this", "html_url": "https://github.com/org/repo/pull/11#issuecomment-501"},
            },
            headers={"X-GitHub-Event": "issue_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": False,
                    "allow_code_reviews": True,
                    "allow_pr_remediation": False,
                    "allow_manual_pr_fix_requests": True,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )

        self.assertEqual(response.status_code, 202)
        github_client.add_issue_comment_reaction.assert_not_called()

    async def test_review_comment_manual_fix_adds_eyes_reaction(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="MAB-1: example",
            body="desc",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=True, state="ready", message="green")

        response = await self._call(
            payload={
                "action": "created",
                "pull_request": {"number": 11},
                "comment": {"id": 901, "body": "@mb fix", "html_url": "https://github.com/org/repo/pull/11#discussion_r901"},
            },
            headers={"X-GitHub-Event": "pull_request_review_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": False,
                    "allow_code_reviews": True,
                    "allow_pr_remediation": False,
                    "allow_manual_pr_fix_requests": True,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )

        self.assertEqual(response.status_code, 202)
        github_client.add_pull_request_review_comment_reaction.assert_called_once_with(
            repo_full_name="org/repo",
            comment_id=901,
            content="eyes",
        )

    async def test_review_comment_without_manual_fix_skips_eyes_reaction(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="MAB-1: example",
            body="desc",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=True, state="ready", message="green")

        response = await self._call(
            payload={
                "action": "created",
                "pull_request": {"number": 11},
                "comment": {"id": 901, "body": "@mb please handle this", "html_url": "https://github.com/org/repo/pull/11#discussion_r901"},
            },
            headers={"X-GitHub-Event": "pull_request_review_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": False,
                    "allow_code_reviews": True,
                    "allow_pr_remediation": False,
                    "allow_manual_pr_fix_requests": True,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )

        self.assertEqual(response.status_code, 202)
        github_client.add_pull_request_review_comment_reaction.assert_not_called()

    async def test_issue_comment_manual_fix_disabled_by_policy(self) -> None:
        github_client = MagicMock()
        github_client.get_pull_request_details.return_value = SimpleNamespace(
            head_sha="abc123",
            title="MAB-1: example",
            body="desc",
        )
        github_client.list_check_suites.return_value = []
        github_client.list_pull_request_files.return_value = []
        gate = MagicMock()
        gate.evaluate_pr.return_value = SimpleNamespace(ready=True, state="ready", message="green")
        enqueue_remediation = MagicMock()

        response = await self._call(
            payload={
                "action": "created",
                "issue": {"number": 11, "pull_request": {"url": "https://api.github.com/repos/org/repo/pulls/11"}},
                "comment": {"body": "@mb fix https://github.com/org/repo/pull/11#discussion_r222"},
            },
            headers={"X-GitHub-Event": "issue_comment"},
            github_client_from_tenant_config=MagicMock(return_value=github_client),
            ReviewAgentGate=MagicMock(return_value=gate),
            enqueue_pr_remediation_if_needed=enqueue_remediation,
            resolve_effective_policy=MagicMock(
                return_value={
                    "allow_auto_merge": False,
                    "allow_code_reviews": True,
                    "allow_pr_remediation": True,
                    "allow_manual_pr_fix_requests": False,
                    "max_pr_auto_remediation_loops": 5,
                }
            ),
        )
        self.assertEqual(response.status_code, 202)
        body = response.body.decode()
        self.assertIn('"reason":"manual_pr_fix_requests_disabled"', body)
        enqueue_remediation.assert_not_called()

        validate = MagicMock()
        response = await self._call(
            payload={},
            headers={"X-GitHub-Event": "issues"},
            resolve_global_github_webhook_secret=MagicMock(return_value=None),
            resolve_tenant_github_webhook_secret=MagicMock(return_value="tenant"),
            validate_github_webhook_signature=validate,
        )
        self.assertEqual(response.status_code, 202)
        validate.assert_called_once()


if __name__ == "__main__":
    unittest.main()
