from __future__ import annotations

import unittest
from types import SimpleNamespace

from orchestrator.api.webhooks.github_application import build_github_webhook_ingress_result
from orchestrator.api.webhooks.github_webhook_context import GitHubWebhookContext, GitHubWebhookPreparedRuntime
from orchestrator.core.communications import GitHubPullRequestCheckRunAction


class GitHubStagingAdmissionIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_ingress_result_plans_staging_admission_for_push_event(self) -> None:
        tenant = SimpleNamespace(tenant_id="tenant-1", policy_config={"allow_code_reviews": False})
        project = SimpleNamespace(
            project_id="project-1",
            policy_overrides={"staging_admission_enabled": True, "staging_branch": "staging"},
        )
        github_client = SimpleNamespace(
            list_open_pull_requests=lambda **_: [
                SimpleNamespace(number=44, head_ref="feature/a", base_ref="staging", title="A", state="open", html_url="")
            ],
            get_pull_request_details=lambda **_: SimpleNamespace(
                number=44,
                state="open",
                title="A",
                head_sha="sha44",
                base_ref="staging",
                mergeable=True,
                mergeable_state="clean",
            ),
            get_branch_head_sha=lambda **_: "stage123456",
        )
        prepared_runtime = GitHubWebhookPreparedRuntime(
            context=GitHubWebhookContext(
                request_id="req-1",
                delivery_id="delivery-1",
                github_event="push",
                payload={"ref": "refs/heads/staging"},
                normalized_action=None,
                installation_id=12345,
                tenant=tenant,
                project=project,
                repo_full_name="org/repo",
                pr_targets=[],
                ref_name="refs/heads/staging",
            ),
            github_client=github_client,
            reviewer_gate=None,
        )

        result = await build_github_webhook_ingress_result(
            prepared_runtime=prepared_runtime,
            request_id="req-1",
            session=SimpleNamespace(),
            settings=SimpleNamespace(),
        )

        self.assertTrue(any(isinstance(action, GitHubPullRequestCheckRunAction) for action in result.actions))
