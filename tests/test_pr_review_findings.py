from __future__ import annotations

from unittest.mock import patch

from orchestrator.core.pr_review_findings import evaluate_pr_review_findings
from orchestrator.tools.github_app import PullRequestFileChange, WorkflowCheckSuite


def test_evaluate_pr_review_findings_parses_payload() -> None:
    payload = {
        "state": "blocked",
        "summary": "Found issues",
        "findings": [
            {
                "severity": "high",
                "message": "Use explicit timeout",
                "path": "orchestrator/core/x.py",
                "line": 42,
                "suggestion": "Set timeout=30",
            }
        ],
    }
    with (
        patch("orchestrator.core.pr_review_findings.get_settings", return_value=object()),
        patch("orchestrator.core.pr_review_findings.build_codex_runtime", return_value=object()),
        patch("orchestrator.core.pr_review_findings.invoke_runtime_json", return_value=payload),
        patch("orchestrator.core.pr_review_findings.render_prompt", return_value="prompt"),
    ):
        result = evaluate_pr_review_findings(
            repo_full_name="org/repo",
            pr_number=10,
            pr_title="MAB-1",
            pr_body="desc",
            workflow_checks=[WorkflowCheckSuite(name="CI", status="completed", conclusion="success")],
            changed_files=[PullRequestFileChange(filename="orchestrator/core/x.py", patch="+ bad()")],
            tenant_id="t1",
            project_id="p1",
        )
    assert result.state == "blocked"
    assert result.summary == "Found issues"
    assert len(result.findings) == 1
    assert result.findings[0].path == "orchestrator/core/x.py"
    assert result.findings[0].line == 42
