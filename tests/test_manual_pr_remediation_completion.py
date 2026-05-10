from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.core.communications import (
    GitHubManualFixIssueCommentReplyAction,
    GitHubManualFixReviewThreadReplyAction,
)
from orchestrator.core.worker.manual_pr_remediation_completion import (
    build_manual_pr_remediation_completion_actions,
    publish_manual_pr_remediation_completion,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import WorkflowResult


def _workflow_result(*, outcome: str = "success") -> WorkflowResult:
    return WorkflowResult(
        outcome=outcome,  # type: ignore[arg-type]
        plan=None,
        pr_url="https://github.com/org/repo/pull/10" if outcome == "success" else None,
        summary=["Implemented the requested change."],
        test_guidance=[],
        attempts=1,
        review_summary=["Moved profile sync off the auth path."],
        dev_rationale=["Added auth regression tests."],
    )


def _plan_with_trigger_context(trigger_context: dict[str, object]) -> dict[str, object]:
    return ExecutionSnapshot.empty(trigger_context=trigger_context).dump()


def test_build_manual_completion_actions_for_review_comment() -> None:
    run = SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="GP-10",
        run_id="run-10",
        status="succeeded",
        pr_url="https://github.com/org/repo/pull/10",
        last_error=None,
        plan=_plan_with_trigger_context(
            {
                "pr_number": 10,
                "head_sha": "abc123",
                "issue_created": True,
                "manual_fix_request": {
                    "requested_by": "alice",
                    "instruction_text": "make auth non-blocking",
                },
                "requested_comment": {
                    "type": "review_comment",
                    "id": 9001,
                    "url": "https://github.com/org/repo/pull/10#discussion_r9001",
                },
            }
        ),
    )
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/org/repo")

    actions = build_manual_pr_remediation_completion_actions(
        project=project,
        run=run,
        workflow_result=_workflow_result(),
        issue_url="https://jira.example.com/browse/GP-10",
    )

    assert len(actions) == 1
    assert isinstance(actions[0], GitHubManualFixReviewThreadReplyAction)
    assert actions[0].status_label == "SUCCEEDED"
    assert actions[0].change_summary == ("Moved profile sync off the auth path.",)


def test_build_manual_completion_actions_for_issue_comment() -> None:
    run = SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="GP-10",
        run_id="run-10",
        status="failed",
        pr_url=None,
        last_error="Tests failed",
        plan=_plan_with_trigger_context(
            {
                "pr_number": "10",
                "issue_created": False,
                "manual_fix_request": {
                    "requested_by": "alice",
                    "instruction_text": "fix this",
                },
                "requested_comment": {
                    "type": "issue_comment",
                    "id": 777,
                    "url": "https://github.com/org/repo/pull/10#issuecomment-777",
                },
            }
        ),
    )
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/org/repo")

    actions = build_manual_pr_remediation_completion_actions(
        project=project,
        run=run,
        workflow_result=_workflow_result(outcome="failed"),
        issue_url="https://jira.example.com/browse/GP-10",
    )

    assert len(actions) == 1
    assert isinstance(actions[0], GitHubManualFixIssueCommentReplyAction)
    assert actions[0].status_label == "FAILED"
    assert actions[0].reason == "Tests failed"


def test_build_manual_completion_actions_skips_legacy_plan_shape() -> None:
    run = SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="GP-10",
        run_id="run-10",
        status="succeeded",
        pr_url="https://github.com/org/repo/pull/10",
        last_error=None,
        plan={
            "trigger_context": {
                "pr_number": 10,
                "manual_fix_request": {"requested_by": "alice", "instruction_text": "fix this"},
                "requested_comment": {"type": "review_comment", "id": 9001, "url": "https://github.com/comment"},
            }
        },
    )
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/org/repo")

    actions = build_manual_pr_remediation_completion_actions(
        project=project,
        run=run,
        workflow_result=_workflow_result(),
        issue_url="https://jira.example.com/browse/GP-10",
    )

    assert actions == ()


def test_publish_manual_completion_noops_when_remediation_context_missing() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={"mode": "github_app"})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/org/repo")
    run = SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="GP-10",
        run_id="run-10",
        status="succeeded",
        pr_url="https://github.com/org/repo/pull/10",
        last_error=None,
        plan={"legacy": "shape"},
    )

    with patch(
        "orchestrator.core.worker.manual_pr_remediation_completion.github_client_from_tenant_config"
    ) as github_client_from_tenant_config:
        publish_manual_pr_remediation_completion(
            session=MagicMock(),
            tenant=tenant,
            project=project,
            run=run,
            workflow_result=_workflow_result(),
            settings=SimpleNamespace(secrets_encryption_key="secret"),
            issue_url="https://jira.example.com/browse/GP-10",
            terminal_status="succeeded",
        )

    github_client_from_tenant_config.assert_not_called()


def test_publish_manual_completion_raises_when_github_config_missing() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/org/repo")
    run = SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="GP-10",
        run_id="run-10",
        status="succeeded",
        pr_url="https://github.com/org/repo/pull/10",
        last_error=None,
        plan=_plan_with_trigger_context(
            {
                "pr_number": 10,
                "manual_fix_request": {"requested_by": "alice", "instruction_text": "fix this"},
                "requested_comment": {"type": "review_comment", "id": 9001, "url": "https://github.com/comment"},
            }
        ),
    )

    with patch(
        "orchestrator.core.worker.manual_pr_remediation_completion.github_client_from_tenant_config"
    ) as github_client_from_tenant_config:
        try:
            publish_manual_pr_remediation_completion(
                session=MagicMock(),
                tenant=tenant,
                project=project,
                run=run,
                workflow_result=_workflow_result(),
                settings=SimpleNamespace(secrets_encryption_key="secret"),
                issue_url="https://jira.example.com/browse/GP-10",
                terminal_status="succeeded",
            )
        except RuntimeError as exc:
            assert "requires tenant GitHub configuration" in str(exc)
        else:
            raise AssertionError("Expected RuntimeError")
    github_client_from_tenant_config.assert_not_called()


def test_publish_manual_completion_raises_when_transport_execution_fails() -> None:
    tenant = SimpleNamespace(tenant_id="tenant-1", github_config={"mode": "github_app"})
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/org/repo")
    run = SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="GP-10",
        run_id="run-10",
        status="succeeded",
        pr_url="https://github.com/org/repo/pull/10",
        last_error=None,
        plan=_plan_with_trigger_context(
            {
                "pr_number": 10,
                "manual_fix_request": {"requested_by": "alice", "instruction_text": "fix this"},
                "requested_comment": {"type": "review_comment", "id": 9001, "url": "https://github.com/comment"},
            }
        ),
    )

    with (
        patch(
            "orchestrator.core.worker.manual_pr_remediation_completion.github_client_from_tenant_config",
            return_value=MagicMock(),
        ),
        patch(
            "orchestrator.core.worker.manual_pr_remediation_completion.GitHubTransportExecutor.execute",
            side_effect=RuntimeError("github publish failed"),
        ),
    ):
        try:
            publish_manual_pr_remediation_completion(
                session=MagicMock(),
                tenant=tenant,
                project=project,
                run=run,
                workflow_result=_workflow_result(),
                settings=SimpleNamespace(secrets_encryption_key="secret"),
                issue_url="https://jira.example.com/browse/GP-10",
                terminal_status="succeeded",
            )
        except RuntimeError as exc:
            assert "github publish failed" in str(exc)
        else:
            raise AssertionError("Expected RuntimeError")
