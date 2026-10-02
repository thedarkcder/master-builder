from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


class PublicWorkflowContractsTests(unittest.TestCase):
    def test_security_audits_frozen_shipping_dependencies(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "security.yml").read_text())
        python_steps = workflow["jobs"]["python-dependency-audit"]["steps"]
        uv_setup = next(
            step
            for step in python_steps
            if step.get("uses", "").startswith("astral-sh/setup-uv@")
        )
        self.assertEqual(uv_setup["with"]["version"], "0.9.24")
        commands = "\n".join(step.get("run", "") for step in python_steps)
        for option in (
            "uv export --frozen",
            "--no-emit-project",
            "--no-dev",
            "--no-default-groups",
        ):
            self.assertIn(option, commands)
        self.assertIn("pip-audit==2.10.1", commands)
        for option in ("--no-deps", "--disable-pip", "--strict", "--requirement"):
            self.assertIn(option, commands)
        self.assertNotIn("pip install -e", commands)
        self.assertNotIn('requirements.txt" ]; then', commands)
        node_steps = workflow["jobs"]["node-dependency-audit"]["steps"]
        self.assertTrue(
            any(
                step.get("run") == "npm audit --package-lock-only"
                for step in node_steps
            )
        )

    def test_optional_release_workflows_use_frozen_runtime_and_existing_module(
        self,
    ) -> None:
        for filename in ("release-train-close.yml", "release-train-sync.yml"):
            workflow = yaml.safe_load((WORKFLOWS / filename).read_text())
            for job in workflow["jobs"].values():
                commands = "\n".join(step.get("run", "") for step in job["steps"])
                self.assertIn("uv sync --frozen --no-dev --no-default-groups", commands)
                self.assertNotIn("pip install", commands)
                self.assertNotIn("${ready_count:-0}", commands)
                self.assertNotIn("from orchestrator.core.release_train", commands)
        workflow = yaml.safe_load((WORKFLOWS / "release-train-close.yml").read_text())
        resolve = next(
            step
            for step in workflow["jobs"]["close-released-issues"]["steps"]
            if step.get("name") == "Resolve release version"
        )
        self.assertNotIn("${{", resolve["run"])
        self.assertEqual(
            resolve["env"]["GITHUB_RELEASE_TAG"], "${{ github.event.release.tag_name }}"
        )

    def test_release_close_guard_stops_before_writes_when_a_matching_pr_is_open(
        self,
    ) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "release-train-close.yml").read_text())
        guard = next(
            step
            for step in workflow["jobs"]["close-released-issues"]["steps"]
            if step.get("name") == "Verify release PRs were merged by maintainers"
        )
        with tempfile.TemporaryDirectory() as directory:
            test_directory = Path(directory)
            issue_keys = test_directory / "issue-keys.txt"
            issue_keys.write_text("EXAMPLE-123\n")
            gh = test_directory / "gh"
            gh.write_text('#!/bin/sh\nprintf "%s\n" "$TEST_OPEN_PR_COUNT"\n')
            gh.chmod(0o700)
            command = guard["run"].replace(
                "/tmp/release_issue_keys.txt", str(issue_keys)
            )
            for count, expected_exit in (("0", 0), ("1", 1)):
                with self.subTest(open_pr_count=count):
                    result = subprocess.run(
                        ["bash", "-c", command],
                        env={
                            **os.environ,
                            "PATH": f"{test_directory}:{os.environ['PATH']}",
                            "GITHUB_REPOSITORY": "example/project",
                            "TEST_OPEN_PR_COUNT": count,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, expected_exit)
                    if count == "1":
                        self.assertIn(
                            "A maintainer must review required checks and merge",
                            result.stdout,
                        )

    def test_release_close_requires_manual_merge_and_read_only_pr_access(self) -> None:
        workflow = yaml.safe_load((WORKFLOWS / "release-train-close.yml").read_text())
        self.assertEqual(workflow["permissions"], {"contents": "read"})
        job = workflow["jobs"]["close-released-issues"]
        self.assertEqual(job["if"], "vars.ENABLE_JIRA_RELEASE_AUTOMATION == 'true'")
        self.assertEqual(
            job["permissions"], {"contents": "write", "pull-requests": "read"}
        )
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        self.assertNotIn("gh pr merge", commands)
        guard = next(
            step
            for step in job["steps"]
            if step.get("name") == "Verify release PRs were merged by maintainers"
        )
        self.assertIn("gh pr list", guard["run"])
        self.assertIn("--state open", guard["run"])
        self.assertIn("exit 1", guard["run"])
        names = [step.get("name") for step in job["steps"]]
        self.assertLess(
            names.index(guard["name"]), names.index("Ensure GitHub release exists")
        )
        self.assertLess(
            names.index(guard["name"]),
            names.index("Transition release-train Jira issues to Done"),
        )


if __name__ == "__main__":
    unittest.main()
