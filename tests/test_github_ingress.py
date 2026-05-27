from __future__ import annotations

import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from orchestrator.api.transport_runtime import decode_json_body
from orchestrator.api.webhooks.github_application import build_github_webhook_ingress_result
from orchestrator.api.webhooks.github_ingress import ingest_github_webhook_event
from orchestrator.api.webhooks.github_webhook_context import (
    GitHubWebhookContext,
    GitHubWebhookPreparedRuntime,
    resolve_github_webhook_context,
)

pytestmark = pytest.mark.contract


class GitHubIngressContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_resolve_context_returns_ping_response(self) -> None:
        request = SimpleNamespace(headers={"X-GitHub-Event": "ping"})
        with (
            patch("orchestrator.api.webhooks.github_webhook_context.read_json_payload", AsyncMock(return_value=({}, b"{}"))),
            patch("orchestrator.api.webhooks.github_webhook_context.resolve_global_github_webhook_secret", return_value=None),
        ):
            response = await resolve_github_webhook_context(
                request=request,
                session=MagicMock(),
                settings=SimpleNamespace(),
                request_id="req-1",
                logger=logging.getLogger("test"),
            )

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body.decode("utf-8"))["reason"], "ping")

    async def test_resolve_context_missing_installation_id_raises_400(self) -> None:
        request = SimpleNamespace(headers={"X-GitHub-Event": "pull_request"})
        with (
            patch("orchestrator.api.webhooks.github_webhook_context.read_json_payload", AsyncMock(return_value=({}, b"{}"))),
            patch("orchestrator.api.webhooks.github_webhook_context.resolve_global_github_webhook_secret", return_value=None),
            patch("orchestrator.api.webhooks.github_webhook_context.extract_installation_id", return_value=None),
        ):
            with self.assertRaises(HTTPException) as exc_ctx:
                await resolve_github_webhook_context(
                    request=request,
                    session=MagicMock(),
                    settings=SimpleNamespace(),
                    request_id="req-1",
                    logger=logging.getLogger("test"),
                )

        self.assertEqual(exc_ctx.exception.status_code, 400)

    async def test_resolve_context_returns_project_not_mapped_response(self) -> None:
        request = SimpleNamespace(headers={"X-GitHub-Event": "pull_request"})
        tenant = SimpleNamespace(tenant_id="example", is_enabled=True, jira_config={}, github_config={})
        payload = {"installation": {"id": 12345}, "repository": {"full_name": "org/repo"}}
        with (
            patch("orchestrator.api.webhooks.github_webhook_context.read_json_payload", AsyncMock(return_value=(payload, b"{}"))),
            patch("orchestrator.api.webhooks.github_webhook_context.resolve_global_github_webhook_secret", return_value=None),
            patch("orchestrator.api.webhooks.github_webhook_context.extract_delivery_id", return_value="delivery-1"),
            patch("orchestrator.api.webhooks.github_webhook_context.extract_installation_id", return_value=12345),
            patch("orchestrator.api.webhooks.github_webhook_context.find_tenant_by_installation_id", return_value=tenant),
            patch("orchestrator.api.webhooks.github_webhook_context.extract_repository_full_name", return_value="org/repo"),
            patch("orchestrator.api.webhooks.github_webhook_context.extract_pull_request_targets", return_value=[(11, True)]),
            patch("orchestrator.api.webhooks.github_webhook_context.resolve_active_project_for_repo", return_value=None),
        ):
            response = await resolve_github_webhook_context(
                request=request,
                session=MagicMock(),
                settings=SimpleNamespace(),
                request_id="req-1",
                logger=logging.getLogger("test"),
            )

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(response.status_code, 202)
        body = json.loads(response.body.decode("utf-8"))
        self.assertEqual(body["reason"], "project_not_mapped")
        self.assertEqual(body["repository"], "org/repo")

    async def test_build_ingress_result_returns_code_reviews_disabled_summary(self) -> None:
        tenant = SimpleNamespace(tenant_id="example", policy_config={"allow_code_reviews": False})
        project = SimpleNamespace(project_id="example-default", policy_overrides={})
        prepared_runtime = GitHubWebhookPreparedRuntime(
            context=GitHubWebhookContext(
                request_id="req-1",
                delivery_id="delivery-1",
                github_event="pull_request",
                payload={"action": "synchronize"},
                normalized_action="synchronize",
                installation_id=12345,
                tenant=tenant,
                project=project,
                repo_full_name="org/repo",
                pr_targets=[(11, True)],
            ),
            github_client=None,
            reviewer_gate=None,
        )

        result = await build_github_webhook_ingress_result(
            prepared_runtime=prepared_runtime,
            request_id="req-1",
            session=MagicMock(),
            settings=SimpleNamespace(),
        )

        body = decode_json_body(result.actions[-1])
        self.assertTrue(body["accepted"])
        self.assertEqual(body["pr_review"]["reason"], "code_reviews_disabled")
        self.assertFalse(body["pr_review"]["enabled"])

    async def test_build_ingress_result_returns_review_misconfigured_response(self) -> None:
        tenant = SimpleNamespace(
            tenant_id="example",
            policy_config={
                "allow_code_reviews": True,
                "allow_pr_remediation": True,
                "allow_auto_merge": False,
            },
        )
        project = SimpleNamespace(project_id="example-default", policy_overrides={})
        prepared_runtime = GitHubWebhookPreparedRuntime(
            context=GitHubWebhookContext(
                request_id="req-1",
                delivery_id="delivery-1",
                github_event="pull_request",
                payload={"action": "synchronize"},
                normalized_action="synchronize",
                installation_id=12345,
                tenant=tenant,
                project=project,
                repo_full_name="org/repo",
                pr_targets=[(11, True)],
            ),
            github_client=None,
            reviewer_gate=None,
            review_runtime_error="bad config",
        )

        result = await build_github_webhook_ingress_result(
            prepared_runtime=prepared_runtime,
            request_id="req-1",
            session=MagicMock(),
            settings=SimpleNamespace(),
        )

        body = decode_json_body(result.actions[-1])
        self.assertFalse(body["accepted"])
        self.assertEqual(body["reason"], "review_misconfigured")

    async def test_ingest_github_webhook_enqueues_one_job_per_pr_target(self) -> None:
        request = SimpleNamespace(headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "delivery-1"})
        session = MagicMock()
        context = GitHubWebhookContext(
            request_id="req-1",
            delivery_id="delivery-1",
            github_event="pull_request",
            payload={"action": "synchronize"},
            normalized_action="synchronize",
            installation_id=12345,
            tenant=SimpleNamespace(tenant_id="example"),
            project=SimpleNamespace(project_id="example-default"),
            repo_full_name="org/repo",
            pr_targets=[(11, True), (12, False)],
        )
        enqueue_mock = MagicMock(
            side_effect=[
                SimpleNamespace(created=True, job=SimpleNamespace(job_id="job-1", dedupe_key="delivery-1:11", subject_key="github_pr:example:org/repo:11", context_json={"pr_number": 11})),
                SimpleNamespace(created=True, job=SimpleNamespace(job_id="job-2", dedupe_key="delivery-1:12", subject_key="github_pr:example:org/repo:12", context_json={"pr_number": 12})),
            ]
        )

        with (
            patch("orchestrator.api.webhooks.github_ingress.resolve_github_webhook_context", AsyncMock(return_value=context)),
            patch("orchestrator.api.webhooks.github_ingress.enqueue_webhook_job", enqueue_mock),
            patch("orchestrator.api.webhooks.github_ingress.notify_webhook_job_enqueued"),
        ):
            response = await ingest_github_webhook_event(
                request=request,
                session=session,
                settings=SimpleNamespace(),
                request_id="req-1",
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue_mock.call_count, 2)
        session.commit.assert_called_once()

    async def test_ingest_github_ping_returns_immediate_response_without_queue(self) -> None:
        request = SimpleNamespace(headers={"X-GitHub-Event": "ping"})
        session = MagicMock()
        ping_response = JSONResponse(status_code=200, content={"accepted": True, "reason": "ping"})

        with (
            patch("orchestrator.api.webhooks.github_ingress.resolve_github_webhook_context", AsyncMock(return_value=ping_response)),
            patch("orchestrator.api.webhooks.github_ingress.enqueue_webhook_job") as enqueue_mock,
        ):
            response = await ingest_github_webhook_event(
                request=request,
                session=session,
                settings=SimpleNamespace(),
                request_id="req-1",
            )

        self.assertEqual(response.status_code, 200)
        enqueue_mock.assert_not_called()

    async def test_ingest_github_push_without_pr_targets_enqueues_repo_job(self) -> None:
        request = SimpleNamespace(headers={"X-GitHub-Event": "push", "X-GitHub-Delivery": "delivery-1"})
        session = MagicMock()
        context = GitHubWebhookContext(
            request_id="req-1",
            delivery_id="delivery-1",
            github_event="push",
            payload={"ref": "refs/heads/staging"},
            normalized_action=None,
            installation_id=12345,
            tenant=SimpleNamespace(tenant_id="example"),
            project=SimpleNamespace(project_id="example-default"),
            repo_full_name="org/repo",
            pr_targets=[],
            ref_name="refs/heads/staging",
        )
        enqueue_mock = MagicMock(
            return_value=SimpleNamespace(
                created=True,
                job=SimpleNamespace(
                    job_id="job-1",
                    dedupe_key="delivery-1:refs/heads/staging",
                    subject_key="github_ref:example:org/repo:refs/heads/staging",
                    context_json={"pr_number": None},
                ),
            )
        )

        with (
            patch("orchestrator.api.webhooks.github_ingress.resolve_github_webhook_context", AsyncMock(return_value=context)),
            patch("orchestrator.api.webhooks.github_ingress.enqueue_webhook_job", enqueue_mock),
            patch("orchestrator.api.webhooks.github_ingress.notify_webhook_job_enqueued"),
        ):
            response = await ingest_github_webhook_event(
                request=request,
                session=session,
                settings=SimpleNamespace(),
                request_id="req-1",
            )

        self.assertEqual(response.status_code, 202)
        enqueue_mock.assert_called_once()
        self.assertIn("github_ref:example:org/repo:refs/heads/staging", enqueue_mock.call_args.kwargs["request"].subject_key)
