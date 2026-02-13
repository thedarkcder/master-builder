import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from orchestrator.core.worker.stage_notifier import RunStageNotifier
from orchestrator.storage.models import Run, Tenant


class WorkerStageNotifierTests(unittest.TestCase):
    def test_append_records_stage_and_fanouts_to_discord_and_jira(self) -> None:
        now = datetime.now(timezone.utc)
        tenant = Tenant(
            tenant_id="tenant-stage",
            name="Tenant Stage",
            is_enabled=True,
            jira_config={},
            github_config={},
            repos_config={"github_repository": "https://github.com/example/repo"},
            policy_config={},
            discord_config=None,
            created_at=now,
            updated_at=now,
        )
        run = Run(
            run_id="run-stage-1",
            tenant_id="tenant-stage",
            issue_key="MAB-910",
            issue_summary="stage update",
            issue_description="desc",
            repo_url="https://github.com/example/repo",
            branch=None,
            pr_url=None,
            status="running",
            last_error=None,
            plan=None,
            created_at=now,
            started_at=now,
            finished_at=None,
        )
        discord_calls: list[dict[str, object]] = []
        jira_calls: list[dict[str, object]] = []

        def fake_discord(**kwargs):  # noqa: ANN003
            discord_calls.append(kwargs)
            return SimpleNamespace(sent=True, reason=None)

        def fake_jira(**kwargs):  # noqa: ANN003
            jira_calls.append(kwargs)

        notifier = RunStageNotifier(
            session=object(),
            tenant=tenant,
            run=run,
            settings=SimpleNamespace(),
            project=None,
            send_discord_message=fake_discord,
            send_jira_message=fake_jira,
        )

        stage_update = {
            "stage": "lock_acquired",
            "tenant_id": "tenant-stage",
            "issue_key": "MAB-910",
            "run_id": "run-stage-1",
            "jira_message": "jira text",
            "discord_message": "discord text",
        }
        notifier.append(stage_update)

        self.assertEqual(notifier.stage_updates, [stage_update])
        self.assertEqual(len(discord_calls), 1)
        self.assertEqual(discord_calls[0]["event"], "lock_acquired")
        self.assertEqual(discord_calls[0]["message"], "discord text")
        self.assertEqual(len(jira_calls), 1)
        self.assertEqual(jira_calls[0]["stage"], "lock_acquired")
        self.assertEqual(jira_calls[0]["message"], "jira text")
