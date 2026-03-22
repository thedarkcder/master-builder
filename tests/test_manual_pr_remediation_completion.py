from __future__ import annotations

from types import SimpleNamespace

from orchestrator.core.communications import (
    GitHubManualFixIssueCommentReplyAction,
    GitHubManualFixReviewThreadReplyAction,
    GitHubStickyRemediationReviewThreadReplyAction,
)
from orchestrator.core.worker.manual_pr_remediation_completion import (
    build_manual_pr_remediation_completion_actions,
)
from orchestrator.core.workflow.runner import WorkflowResult


def _workflow_result(*, succeeded: bool = True) -> WorkflowResult:
    return WorkflowResult(
        succeeded=succeeded,
        plan=None,
        pr_url="https://github.com/org/repo/pull/10" if succeeded else None,
        summary=["Implemented the requested change."],
        test_guidance=[],
        attempts=1,
        review_summary=["Moved profile sync off the auth path."],
        dev_rationale=["Added auth regression tests."],
    )


def test_build_manual_completion_actions_for_review_comment() -> None:
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
        },
    )
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/org/repo")

    actions = build_manual_pr_remediation_completion_actions(
        project=project,
        run=run,
        workflow_result=_workflow_result(),
        issue_url="https://jira.example.com/browse/GP-10",
    )

    assert len(actions) == 2
    assert isinstance(actions[0], GitHubManualFixReviewThreadReplyAction)
    assert isinstance(actions[1], GitHubStickyRemediationReviewThreadReplyAction)
    assert actions[0].status_label == "SUCCEEDED"
    assert actions[0].change_summary == ("Moved profile sync off the auth path.",)
    assert actions[1].status_label == "SUCCEEDED"


def test_build_manual_completion_actions_for_issue_comment() -> None:
    run = SimpleNamespace(
        tenant_id="tenant-1",
        issue_key="GP-10",
        run_id="run-10",
        status="failed",
        pr_url=None,
        last_error="Tests failed",
        plan={
            "trigger_context": {
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
        },
    )
    project = SimpleNamespace(project_id="project-1", github_repository="https://github.com/org/repo")

    actions = build_manual_pr_remediation_completion_actions(
        project=project,
        run=run,
        workflow_result=_workflow_result(succeeded=False),
        issue_url="https://jira.example.com/browse/GP-10",
    )

    assert len(actions) == 1
    assert isinstance(actions[0], GitHubManualFixIssueCommentReplyAction)
    assert actions[0].status_label == "FAILED"
    assert actions[0].reason == "Tests failed"
