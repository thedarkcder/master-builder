from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from orchestrator.api.webhooks.github_webhook_context import (
    _latest_run_id_for_pr_url,
    _qa_demo_recordings_for_run,
    build_github_review_runtime,
)
from orchestrator.core.config import get_settings
from orchestrator.core.review.pr_review_findings import PrReviewFindingsResult, ReviewFinding
from orchestrator.core.worker.webhook_job_service import process_next_webhook_job
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import QaRecording, QaResult, QaScenario, QaStep, WorkflowStageCheckpoint
from orchestrator.storage.models import Project, Run, WorkflowExecution
from tests.production_path_support import (
    load_json_fixture,
    ProductionPathApiTestCase,
    seed_core_runtime_state,
    session_factory_for,
)

pytestmark = pytest.mark.production_path


class _FakeGitHubClient:
    def __init__(self) -> None:
        self.pull_request_reactions: list[dict[str, object]] = []
        self.review_comment_reactions: list[dict[str, object]] = []
        self.issue_comment_reactions: list[dict[str, object]] = []
        self.review_thread_replies: list[dict[str, object]] = []
        self.issue_comments: list[dict[str, object]] = []
        self.check_runs: list[dict[str, object]] = []

    def get_pull_request_details(self, *, repo_full_name: str, pr_number: int):
        return SimpleNamespace(
            head_sha="abc123",
            title="GP-123: example",
            body="desc",
            head_ref="feature/GP-123",
            base_ref="main",
            mergeable=True,
            mergeable_state="clean",
            html_url=f"https://github.com/{repo_full_name}/pull/{pr_number}",
        )

    def get_branch_head_sha(self, *, repo_full_name: str, branch: str):  # noqa: ARG002
        return "staging1234567"

    def list_check_suites(self, *, repo_full_name: str, ref: str):  # noqa: ARG002
        return []

    def list_pull_request_files(self, *, repo_full_name: str, pr_number: int):  # noqa: ARG002
        return []

    def add_pull_request_review_comment_reaction(self, *, repo_full_name: str, comment_id: int, content: str) -> None:
        self.review_comment_reactions.append(
            {"repo_full_name": repo_full_name, "comment_id": comment_id, "content": content}
        )

    def add_issue_comment_reaction(self, *, repo_full_name: str, comment_id: int, content: str) -> None:
        self.issue_comment_reactions.append(
            {"repo_full_name": repo_full_name, "comment_id": comment_id, "content": content}
        )

    def sync_pull_request_reaction(self, *, repo_full_name: str, pr_number: int, content: str) -> None:
        self.pull_request_reactions.append(
            {"repo_full_name": repo_full_name, "pr_number": pr_number, "content": content}
        )

    def list_pull_request_review_comments(self, *, repo_full_name: str, pr_number: int):  # noqa: ARG002
        return []

    def create_pull_request_review_comment_reply(
        self,
        *,
        repo_full_name: str,
        pull_request_number: int | None = None,
        pr_number: int | None = None,
        in_reply_to: int,
        body: str,
    ):
        self.review_thread_replies.append(
            {
                "repo_full_name": repo_full_name,
                "pull_request_number": pull_request_number if pull_request_number is not None else pr_number,
                "in_reply_to": in_reply_to,
                "body": body,
            }
        )
        return SimpleNamespace(comment_id=300 + len(self.review_thread_replies))

    def update_pull_request_review_comment(self, *, repo_full_name: str, comment_id: int, body: str):
        self.review_thread_replies.append(
            {
                "repo_full_name": repo_full_name,
                "comment_id": comment_id,
                "body": body,
                "updated": True,
            }
        )
        return SimpleNamespace(comment_id=comment_id)

    def list_pull_request_issue_comments(self, *, repo_full_name: str, pr_number: int):  # noqa: ARG002
        return []

    def create_pull_request_issue_comment(self, *, repo_full_name: str, pr_number: int, body: str):
        self.issue_comments.append(
            {"repo_full_name": repo_full_name, "pr_number": pr_number, "body": body}
        )
        return SimpleNamespace(comment_id=500 + len(self.issue_comments))

    def create_check_run(
        self,
        *,
        repo_full_name: str,
        head_sha: str,
        name: str,
        status: str,
        conclusion: str | None = None,
        title: str | None = None,
        summary: str | None = None,
    ):
        self.check_runs.append(
            {
                "repo_full_name": repo_full_name,
                "head_sha": head_sha,
                "name": name,
                "status": status,
                "conclusion": conclusion,
                "title": title,
                "summary": summary,
            }
        )
        return SimpleNamespace(check_run_id=100 + len(self.check_runs), html_url=None)

    def update_issue_comment(self, *, repo_full_name: str, comment_id: int, body: str):
        self.issue_comments.append(
            {"repo_full_name": repo_full_name, "comment_id": comment_id, "body": body, "updated": True}
        )
        return SimpleNamespace(comment_id=comment_id)


def _qa_demo_recording_snapshot() -> ExecutionSnapshot:
    snapshot = ExecutionSnapshot.empty()
    snapshot.apply_stage_checkpoint(
        WorkflowStageCheckpoint(
            stage="qa",
            attempt=1,
            status="completed",
            summary="recorded",
            qa_result=QaResult(
                summary=["recorded"],
                scenarios=[
                    QaScenario(
                        name="Browser happy path",
                        objective="Show browser feature",
                        capture_target="browser",
                        steps=[QaStep(action="assert_visible", selector="text=Feature")],
                    )
                ],
                recordings=[
                    QaRecording(
                        name="Browser happy path",
                        artifact_url="https://cdn.example/qa-demos/example/example-default/run-1/qa-demo-1.webm",
                        object_key="example/example-default/run-1/qa-demo-1.webm",
                        capture_target="browser",
                        capture_reference="https://preview.example",
                        content_sha256=f"{1:064x}",
                        release_commit_sha="b" * 40,
                        release_context_sha256="a" * 64,
                    )
                ],
            ),
        )
    )
    return snapshot


class GitHubWebhookProductionPathTests(ProductionPathApiTestCase):
    @classmethod
    def bootstrap_template_state(cls) -> None:
        seed_core_runtime_state(session_factory_for(cls._template_database_url))

    def setUp(self) -> None:
        self._start_test_runtime(name_prefix="github-webhook-production")

    def tearDown(self) -> None:
        self._stop_test_runtime()

    def _process_one_webhook_job(self):
        with self.session_factory() as session:
            return process_next_webhook_job(
                session=session,
                settings=get_settings(),
                owner_id="worker:test",
            )

    def test_qa_demo_review_runtime_passes_artifact_public_base_to_reviewer_gate(self) -> None:
        fake_client = _FakeGitHubClient()
        tenant = SimpleNamespace(
            tenant_id="example",
            github_config={},
            policy_config={"qa_demo_recording_enabled": True},
        )
        project = SimpleNamespace(
            project_id="example-default",
            policy_overrides={},
        )
        settings = SimpleNamespace(
            secrets_encryption_key="",
            codex_model=None,
            codex_reasoning_effort=None,
            qa_demo_artifact_public_base_url="https://cdn.example/qa-demos",
        )

        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch("orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate") as gate_cls,
        ):
            github_client, _reviewer_gate = build_github_review_runtime(
                session=SimpleNamespace(),
                settings=settings,
                tenant=tenant,
                project=project,
            )

        self.assertIs(github_client, fake_client)
        self.assertEqual(
            gate_cls.call_args.kwargs["demo_artifact_public_base_url"],
            "https://cdn.example/qa-demos",
        )
        self.assertIn("demo_evidence_recordings_resolver", gate_cls.call_args.kwargs)

    def test_qa_demo_review_runtime_resolves_persisted_qa_recordings_for_run(self) -> None:
        snapshot = _qa_demo_recording_snapshot()
        with self.session_factory() as session:
            session.add(
                WorkflowExecution(
                    workflow_id="workflow-qa-demo-proof",
                    workflow_type_key="issue_execution",
                    tenant_id="example",
                    project_id="example-default",
                    source_system="jira",
                    source_ref="GP-123",
                    repo_url="https://github.com/org/repo",
                    branch="feature/GP-123",
                    pr_url="https://github.com/org/repo/pull/17",
                    orchestration_backend="temporal",
                    dedupe_scope="issue_execution",
                    status="completed",
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                Run(
                    run_id="run-qa-demo-proof",
                    workflow_id="workflow-qa-demo-proof",
                    tenant_id="example",
                    project_id="example-default",
                    issue_key="GP-123",
                    repo_url="https://github.com/org/repo",
                    branch="feature/GP-123",
                    pr_url="https://github.com/org/repo/pull/17",
                    attempt_number=1,
                    entry_stage="qa",
                    dedupe_scope="issue_execution",
                    status="succeeded",
                    created_at=datetime.now(timezone.utc),
                    plan=snapshot.dump(),
                )
            )
            session.commit()

            recordings = _qa_demo_recordings_for_run(
                session=session,
                tenant_id="example",
                project_id="example-default",
                run_id="run-qa-demo-proof",
            )

        self.assertEqual(
            recordings,
            (
                {
                    "name": "Browser happy path",
                    "artifact_url": "https://cdn.example/qa-demos/example/example-default/run-1/qa-demo-1.webm",
                    "object_key": "example/example-default/run-1/qa-demo-1.webm",
                    "capture_target": "browser",
                    "capture_reference": "https://preview.example",
                    "content_sha256": f"{1:064x}",
                    "release_commit_sha": "b" * 40,
                    "release_context_sha256": "a" * 64,
                },
            ),
        )

    def test_qa_demo_review_runtime_ignores_persisted_qa_recordings_from_blocked_run(self) -> None:
        snapshot = _qa_demo_recording_snapshot()
        with self.session_factory() as session:
            session.add(
                WorkflowExecution(
                    workflow_id="workflow-qa-demo-blocked",
                    workflow_type_key="issue_execution",
                    tenant_id="example",
                    project_id="example-default",
                    source_system="jira",
                    source_ref="GP-123",
                    repo_url="https://github.com/org/repo",
                    branch="feature/GP-123",
                    pr_url="https://github.com/org/repo/pull/17",
                    orchestration_backend="temporal",
                    dedupe_scope="issue_execution",
                    status="failed",
                    created_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            )
            session.add(
                Run(
                    run_id="run-qa-demo-blocked",
                    workflow_id="workflow-qa-demo-blocked",
                    tenant_id="example",
                    project_id="example-default",
                    issue_key="GP-123",
                    repo_url="https://github.com/org/repo",
                    branch="feature/GP-123",
                    pr_url="https://github.com/org/repo/pull/17",
                    attempt_number=1,
                    entry_stage="qa",
                    dedupe_scope="issue_execution",
                    status="blocked",
                    created_at=datetime.now(timezone.utc),
                    plan=snapshot.dump(),
                )
            )
            session.commit()

            recordings = _qa_demo_recordings_for_run(
                session=session,
                tenant_id="example",
                project_id="example-default",
                run_id="run-qa-demo-blocked",
            )

        self.assertIsNone(recordings)

    def test_qa_demo_review_runtime_resolves_latest_run_for_pr_url(self) -> None:
        pr_url = "https://github.com/org/repo/pull/17"
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            old_workflow = WorkflowExecution(
                workflow_id="workflow-old-demo",
                workflow_type_key="issue_execution",
                tenant_id="example",
                project_id="example-default",
                source_system="jira",
                source_ref="GP-123",
                repo_url="https://github.com/org/repo",
                branch="feature/GP-123",
                pr_url=pr_url,
                orchestration_backend="temporal",
                dedupe_scope="issue_execution",
                status="completed",
                created_at=now - timedelta(minutes=10),
                updated_at=now - timedelta(minutes=10),
            )
            latest_workflow = WorkflowExecution(
                workflow_id="workflow-latest-demo",
                workflow_type_key="issue_execution",
                tenant_id="example",
                project_id="example-default",
                source_system="jira",
                source_ref="GP-123",
                repo_url="https://github.com/org/repo",
                branch="feature/GP-123",
                pr_url=pr_url,
                orchestration_backend="temporal",
                dedupe_scope="issue_execution",
                status="completed",
                created_at=now,
                updated_at=now,
            )
            session.add_all([old_workflow, latest_workflow])
            session.add_all(
                [
                    Run(
                        run_id="run-old-demo",
                        workflow_id="workflow-old-demo",
                        tenant_id="example",
                        project_id="example-default",
                        issue_key="GP-123",
                        repo_url="https://github.com/org/repo",
                        branch="feature/GP-123",
                        pr_url=pr_url,
                        attempt_number=1,
                        entry_stage="pm",
                        dedupe_scope="issue_execution",
                        status="succeeded",
                        created_at=now - timedelta(minutes=10),
                    ),
                    Run(
                        run_id="run-latest-demo",
                        workflow_id="workflow-latest-demo",
                        tenant_id="example",
                        project_id="example-default",
                        issue_key="GP-123",
                        repo_url="https://github.com/org/repo",
                        branch="feature/GP-123",
                        pr_url=pr_url,
                        attempt_number=2,
                        entry_stage="qa",
                        dedupe_scope="issue_execution",
                        status="running",
                        created_at=now,
                    ),
                ]
            )
            session.commit()

            resolved = _latest_run_id_for_pr_url(
                session=session,
                tenant_id="example",
                project_id="example-default",
                pr_url=pr_url,
            )

        self.assertEqual(resolved, "run-latest-demo")

    def test_ignored_event_runs_through_real_route(self) -> None:
        response = self.client.post(
            "/github/webhook",
            json=load_json_fixture("github", "webhooks", "issues_opened.json"),
            headers={"X-GitHub-Event": "issues", "X-GitHub-Delivery": "delivery-1"},
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["reason"], "ignored_event")
        self.assertTrue(response.json()["accepted"])

    def test_manual_fix_review_comment_executes_real_publication_path(self) -> None:
        fake_client = _FakeGitHubClient()
        remediation_result = SimpleNamespace(
            triggered=True,
            issue_key="GP-900",
            issue_created=False,
            enqueued=True,
            reason=None,
            run=SimpleNamespace(run_id="run-900"),
            head_sha="abc123",
        )
        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch(
                "orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate",
                return_value=SimpleNamespace(
                    evaluate_pr=lambda **kwargs: SimpleNamespace(ready=False, state="pending_checks", message="pending")
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.enqueue_pr_remediation_if_needed",
                return_value=remediation_result,
            ),
            patch(
                "orchestrator.api.webhooks.github_application.tenant_jira_issue_url",
                return_value="https://jira.example.com/browse/GP-900",
            ),
        ):
            response = self.client.post(
                "/github/webhook",
                json=load_json_fixture("github", "webhooks", "pull_request_review_comment_created.json"),
                headers={
                    "X-GitHub-Event": "pull_request_review_comment",
                    "X-GitHub-Delivery": "delivery-2",
                },
            )
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["accepted"])
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertEqual(fake_client.review_comment_reactions[0]["comment_id"], 901)
        self.assertEqual(fake_client.review_comment_reactions[0]["content"], "eyes")
        self.assertTrue(fake_client.review_thread_replies)
        reply_bodies = [str(reply["body"]) for reply in fake_client.review_thread_replies]
        self.assertTrue(any("Codex Manual Fix" in body for body in reply_bodies))
        self.assertFalse(any("Codex PR Remediation" in body for body in reply_bodies))

    def test_untagged_review_comment_is_ignored_by_real_route(self) -> None:
        fake_client = _FakeGitHubClient()
        payload = load_json_fixture("github", "webhooks", "pull_request_review_comment_created.json")
        payload["comment"]["body"] = "fix this"

        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch(
                "orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate",
                return_value=SimpleNamespace(
                    evaluate_pr=lambda **kwargs: SimpleNamespace(ready=False, state="pending_checks", message="pending")
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.enqueue_pr_remediation_if_needed",
            ) as enqueue_mock,
        ):
            response = self.client.post(
                "/github/webhook",
                json=payload,
                headers={
                    "X-GitHub-Event": "pull_request_review_comment",
                    "X-GitHub-Delivery": "delivery-untagged-review",
                },
            )
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        enqueue_mock.assert_not_called()
        self.assertFalse(fake_client.review_comment_reactions)
        self.assertFalse(fake_client.review_thread_replies)

    def test_pull_request_opened_syncs_pr_reaction_and_posts_review_comment(self) -> None:
        fake_client = _FakeGitHubClient()
        payload = {
            "action": "opened",
            "installation": {"id": 12345},
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 17,
                "title": "GP-123: example",
                "body": "desc",
                "html_url": "https://github.com/org/repo/pull/17",
                "state": "open",
                "head": {"sha": "abc123", "ref": "feature/GP-123"},
                "base": {"ref": "main"},
            },
        }

        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch(
                "orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate",
                return_value=SimpleNamespace(
                    evaluate_pr=lambda **kwargs: SimpleNamespace(ready=False, state="needs_changes", message="needs changes")
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.evaluate_pr_review_findings",
                return_value=PrReviewFindingsResult(
                    state="blocked",
                    summary="Found issues",
                    findings=(ReviewFinding(severity="high", message="Fix this", path=None, line=None),),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.enqueue_pr_remediation_if_needed",
                return_value=SimpleNamespace(
                    triggered=False,
                    issue_key=None,
                    issue_created=False,
                    enqueued=False,
                    reason="not_needed",
                    run=None,
                    head_sha="abc123",
                ),
            ),
        ):
            response = self.client.post(
                "/github/webhook",
                json=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-opened",
                },
            )
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertEqual(
            fake_client.pull_request_reactions,
            [{"repo_full_name": "org/repo", "pr_number": 17, "content": "confused"}],
        )
        self.assertTrue(fake_client.issue_comments)
        self.assertTrue(any("Codex PR Review" in str(comment["body"]) for comment in fake_client.issue_comments))

    def test_pull_request_reaction_404_does_not_fail_webhook_job(self) -> None:
        fake_client = _FakeGitHubClient()

        def raise_reaction_404(**_: object) -> None:
            raise RuntimeError("HTTP Error 404: Not Found")

        fake_client.sync_pull_request_reaction = raise_reaction_404
        payload = {
            "action": "synchronize",
            "installation": {"id": 12345},
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 17,
                "title": "GP-123: example",
                "body": "desc",
                "html_url": "https://github.com/org/repo/pull/17",
                "state": "open",
                "head": {"sha": "abc123", "ref": "feature/GP-123"},
                "base": {"ref": "stage"},
            },
        }

        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch(
                "orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate",
                return_value=SimpleNamespace(
                    evaluate_pr=lambda **kwargs: SimpleNamespace(ready=False, state="needs_changes", message="needs changes")
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.evaluate_pr_review_findings",
                return_value=PrReviewFindingsResult(
                    state="blocked",
                    summary="Found issues",
                    findings=(ReviewFinding(severity="high", message="Fix this", path=None, line=None),),
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.enqueue_pr_remediation_if_needed",
                return_value=SimpleNamespace(
                    triggered=False,
                    issue_key=None,
                    issue_created=False,
                    enqueued=False,
                    reason="not_needed",
                    run=None,
                    head_sha="abc123",
                ),
            ),
        ):
            response = self.client.post(
                "/github/webhook",
                json=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-reaction-404",
                },
            )
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertTrue(fake_client.issue_comments)

    def test_pull_request_closed_does_not_publish_review_side_effects(self) -> None:
        fake_client = _FakeGitHubClient()
        payload = {
            "action": "closed",
            "installation": {"id": 12345},
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 17,
                "title": "GP-123: example",
                "body": "desc",
                "html_url": "https://github.com/org/repo/pull/17",
                "state": "closed",
                "head": {"sha": "abc123", "ref": "feature/GP-123"},
                "base": {"ref": "stage"},
            },
        }

        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch(
                "orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate",
                return_value=SimpleNamespace(
                    evaluate_pr=lambda **kwargs: SimpleNamespace(ready=False, state="needs_changes", message="needs changes")
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.enqueue_pr_remediation_if_needed",
            ) as remediation_mock,
        ):
            response = self.client.post(
                "/github/webhook",
                json=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-closed",
                },
            )
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        remediation_mock.assert_not_called()
        self.assertFalse(fake_client.pull_request_reactions)
        self.assertFalse(fake_client.issue_comments)

    def test_pull_request_opened_with_no_findings_publishes_single_success_reaction(self) -> None:
        fake_client = _FakeGitHubClient()
        payload = {
            "action": "ready_for_review",
            "installation": {"id": 12345},
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 17,
                "title": "GP-123: example",
                "body": "desc",
                "html_url": "https://github.com/org/repo/pull/17",
                "state": "open",
                "head": {"sha": "abc123", "ref": "feature/GP-123"},
                "base": {"ref": "main"},
            },
        }
        missing_checks_signal = SimpleNamespace(
            ready=False,
            state="missing_checks",
            message="PR checks missing: CI, Security",
        )
        findings_result = PrReviewFindingsResult(
            state="ready",
            summary="No actionable findings identified in the provided patch set.",
            findings=(),
        )

        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch(
                "orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate",
                return_value=SimpleNamespace(evaluate_pr=lambda **kwargs: missing_checks_signal),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.evaluate_pr_review_findings",
                return_value=findings_result,
            ),
            patch(
                "orchestrator.api.webhooks.github_application.enqueue_pr_remediation_if_needed",
            ) as remediation_mock,
        ):
            response = self.client.post(
                "/github/webhook",
                json=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-no-findings",
                },
            )
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertEqual(
            fake_client.pull_request_reactions,
            [{"repo_full_name": "org/repo", "pr_number": 17, "content": "+1"}],
        )
        remediation_mock.assert_not_called()
        self.assertFalse(fake_client.issue_comments)

    def test_pull_request_opened_publishes_staging_admission_check(self) -> None:
        fake_client = _FakeGitHubClient()
        fake_client.get_pull_request_details = lambda **_: SimpleNamespace(
            head_sha="abc123",
            title="GP-124: stage-safe",
            body="desc",
            head_ref="feature/GP-124",
            base_ref="staging",
            mergeable=True,
            mergeable_state="clean",
            html_url="https://github.com/org/repo/pull/18",
            state="open",
        )
        payload = {
            "action": "opened",
            "installation": {"id": 12345},
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "number": 18,
                "title": "GP-124: stage-safe",
                "body": "desc",
                "html_url": "https://github.com/org/repo/pull/18",
                "state": "open",
                "head": {"sha": "abc123", "ref": "feature/GP-124"},
                "base": {"ref": "staging"},
            },
        }

        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch(
                "orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate",
                return_value=SimpleNamespace(
                    evaluate_pr=lambda **kwargs: SimpleNamespace(ready=False, state="needs_changes", message="needs changes")
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.enqueue_pr_remediation_if_needed",
                return_value=SimpleNamespace(
                    triggered=False,
                    issue_key=None,
                    issue_created=False,
                    enqueued=False,
                    reason="not_needed",
                    run=None,
                    head_sha="abc123",
                ),
            ),
        ):
            with self.session_factory() as session:
                project = session.get(Project, "example-default")
                assert project is not None
                project.policy_overrides = {
                    **dict(project.policy_overrides or {}),
                    "staging_admission_enabled": True,
                    "staging_branch": "staging",
                }
                session.commit()
            response = self.client.post(
                "/github/webhook",
                json=payload,
                headers={
                    "X-GitHub-Event": "pull_request",
                    "X-GitHub-Delivery": "delivery-pr-opened-check",
                },
            )
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertTrue(fake_client.check_runs)
        self.assertEqual(fake_client.check_runs[0]["name"], "MB Staging Merge Check")
        self.assertEqual(fake_client.check_runs[0]["conclusion"], "success")

    def test_check_run_completed_refreshes_pr_review_comment(self) -> None:
        fake_client = _FakeGitHubClient()
        payload = {
            "action": "completed",
            "installation": {"id": 12345},
            "repository": {"full_name": "org/repo"},
            "check_run": {
                "conclusion": "success",
                "pull_requests": [{"number": 17}],
            },
        }

        with (
            patch(
                "orchestrator.api.webhooks.github_webhook_context.github_client_from_tenant_config",
                return_value=fake_client,
            ),
            patch(
                "orchestrator.api.webhooks.github_webhook_context.ReviewAgentGate",
                return_value=SimpleNamespace(
                    evaluate_pr=lambda **kwargs: SimpleNamespace(ready=False, state="pending_checks", message="pending")
                ),
            ),
            patch(
                "orchestrator.api.webhooks.github_application.enqueue_pr_remediation_if_needed",
                return_value=SimpleNamespace(
                    triggered=False,
                    issue_key=None,
                    issue_created=False,
                    enqueued=False,
                    reason="not_needed",
                    run=None,
                    head_sha="abc123",
                ),
            ),
        ):
            response = self.client.post(
                "/github/webhook",
                json=payload,
                headers={
                    "X-GitHub-Event": "check_run",
                    "X-GitHub-Delivery": "delivery-check-run-completed",
                },
            )
            processed = self._process_one_webhook_job()

        self.assertEqual(response.status_code, 202)
        self.assertIsNotNone(processed)
        assert processed is not None
        self.assertEqual(processed.status, "done")
        self.assertEqual(
            fake_client.pull_request_reactions,
            [{"repo_full_name": "org/repo", "pr_number": 17, "content": "confused"}],
        )
        self.assertTrue(fake_client.issue_comments)
        self.assertTrue(any("Codex PR Review" in str(comment["body"]) for comment in fake_client.issue_comments))
