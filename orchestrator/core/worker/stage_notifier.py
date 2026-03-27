from __future__ import annotations

import logging
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.storage.models import Project, Run, Tenant

logger = logging.getLogger(__name__)


class RunStageNotifier:
    def __init__(
        self,
        *,
        session: Session,
        tenant: Tenant,
        run: Run,
        settings: Any,
        project: Project | None,
        send_discord_message: Callable[..., Any],
        send_jira_message: Callable[..., None],
    ) -> None:
        self._session = session
        self._tenant = tenant
        self._run = run
        self._settings = settings
        self._project = project
        self._send_discord_message = send_discord_message
        self._send_jira_message = send_jira_message
        self.stage_updates: list[dict[str, str]] = []

    def append(self, stage_update: dict[str, str]) -> None:
        self.stage_updates.append(stage_update)
        self._session.refresh(self._run, attribute_names=["plan"])
        existing_plan = dict(self._run.plan or {})
        live_updates_raw = existing_plan.get("live_stage_updates")
        live_updates: list[dict[str, str]] = []
        if isinstance(live_updates_raw, list):
            for item in live_updates_raw:
                if isinstance(item, dict):
                    live_updates.append({str(k): str(v) for k, v in item.items()})
        live_updates.append(
            {
                "stage": stage_update["stage"],
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        existing_plan["live_stage_updates"] = live_updates[-40:]
        self._run.plan = existing_plan
        self._session.commit()
        self._session.refresh(self._run)
        send_result = self._send_discord_message(
            session=self._session,
            tenant=self._tenant,
            project=self._project,
            message=stage_update["discord_message"],
            settings=self._settings,
            event=stage_update["stage"],
        )
        if not send_result.sent:
            logger.info(
                "worker_discord_stage_update_not_sent tenant_id=%s run_id=%s stage=%s reason=%s",
                self._run.tenant_id,
                self._run.run_id,
                stage_update["stage"],
                send_result.reason,
            )
        self._send_jira_message(
            session=self._session,
            tenant=self._tenant,
            issue_key=self._run.issue_key,
            stage=stage_update["stage"],
            message=stage_update["jira_message"],
            settings=self._settings,
        )
