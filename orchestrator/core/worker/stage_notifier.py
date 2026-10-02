from __future__ import annotations

import logging
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.worker.stage_event_types import WorkerStageEvent
from orchestrator.core.worker.stage_events import WorkerStageUpdate
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
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
        self.stage_updates: list[WorkerStageUpdate] = []

    def append(self, stage_update: WorkerStageUpdate | dict[str, str]) -> None:
        if isinstance(stage_update, WorkerStageUpdate):
            normalized = stage_update
        else:
            normalized = WorkerStageUpdate.load(stage_update)
            if normalized is None and isinstance(stage_update, dict):
                stage_raw = str(stage_update.get("stage") or "").strip()
                if stage_raw:
                    try:
                        normalized = WorkerStageUpdate(
                            stage=WorkerStageEvent(stage_raw),
                            tenant_id=str(
                                getattr(self._run, "tenant_id", "") or ""
                            ).strip(),
                            issue_key=str(
                                getattr(self._run, "issue_key", "") or ""
                            ).strip(),
                            run_id=str(getattr(self._run, "run_id", "") or "").strip(),
                            jira_message=str(
                                stage_update.get("jira_message") or ""
                            ).strip(),
                            discord_message=str(
                                stage_update.get("discord_message") or ""
                            ).strip(),
                        )
                    except ValueError:
                        normalized = None
        if normalized is None:
            raise ValueError("stage_update must be a valid WorkerStageUpdate payload")
        self.stage_updates.append(normalized)
        self._session.refresh(self._run, attribute_names=["plan"])
        snapshot = ExecutionSnapshot.require(self._run.plan, allow_empty=True)
        snapshot.append_live_stage_update(
            stage=normalized.event_name,
            recorded_at=datetime.now(timezone.utc).isoformat(),
        )
        self._run.plan = snapshot.dump()
        self._session.commit()
        self._session.refresh(self._run)
        send_result = self._send_discord_message(
            session=self._session,
            tenant=self._tenant,
            project=self._project,
            message=normalized.discord_message,
            settings=self._settings,
            event=normalized.event_name,
        )
        if not send_result.sent:
            logger.info(
                "worker_discord_stage_update_not_sent tenant_id=%s run_id=%s stage=%s reason=%s",
                self._run.tenant_id,
                self._run.run_id,
                normalized.event_name,
                send_result.reason,
            )
        self._send_jira_message(
            session=self._session,
            tenant=self._tenant,
            issue_key=self._run.issue_key,
            stage=normalized.event_name,
            message=normalized.jira_message,
            settings=self._settings,
        )
