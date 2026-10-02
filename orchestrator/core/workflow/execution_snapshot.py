from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from orchestrator.core.workflow.trigger_context import (
    GithubPrRemediationTriggerContext,
    TriggerContext,
    TriggerContextDecodeError,
    decode_trigger_context,
    require_github_pr_remediation_context,
)
from orchestrator.core.worker.stage_events import WorkerStageUpdate
from orchestrator.core.workflow.checkpoint_codec import (
    decode_dev_result_payload,
    decode_pm_plan_payload,
    decode_qa_result_payload,
    decode_review_result_payload,
    decode_test_result_payload,
    encode_stage_checkpoint_artifact,
)
from orchestrator.core.workflow.runner import DevResult
from orchestrator.core.workflow.runner import PmPlan
from orchestrator.core.workflow.runner import QaResult
from orchestrator.core.workflow.runner import ReviewResult
from orchestrator.core.workflow.runner import TestResult
from orchestrator.core.workflow.runner import WorkflowResult
from orchestrator.core.workflow.runner import WorkflowStageCheckpoint

SNAPSHOT_VERSION = 1
_VALID_WORKFLOW_OUTCOMES: set[str] = {
    "success",
    "requeue",
    "waiting_for_input",
    "blocked",
    "failed",
}


class StageName(str, Enum):
    PM = "pm"
    DEV = "dev"
    TEST = "test"
    REVIEW = "review"
    QA = "qa"


@dataclass
class SnapshotContext:
    trigger_context: dict[str, Any] = field(default_factory=dict)
    execution_context: dict[str, Any] = field(default_factory=dict)


@dataclass
class SnapshotWorkflow:
    outcome: str | None = None
    attempts: int = 0
    summary: list[str] = field(default_factory=list)
    blocker_message: str | None = None
    requeue_target: str | None = None
    requeue_reason: str | None = None


@dataclass
class SnapshotEvents:
    stage_updates: list[dict[str, Any]] = field(default_factory=list)
    live_stage_updates: list[dict[str, Any]] = field(default_factory=list)
    stage_trace: list[dict[str, Any]] = field(default_factory=list)
    workstream_trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ExecutionStageRecord:
    attempt: int
    status: str
    summary: str
    completed_at: str
    artifact: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "attempt": self.attempt,
            "status": self.status,
            "summary": self.summary,
            "completed_at": self.completed_at,
        }
        if self.artifact is not None:
            payload["artifact"] = dict(self.artifact)
        return payload


@dataclass
class ExecutionSnapshot:
    version: int = SNAPSHOT_VERSION
    context: SnapshotContext = field(default_factory=SnapshotContext)
    workflow: SnapshotWorkflow = field(default_factory=SnapshotWorkflow)
    events: SnapshotEvents = field(default_factory=SnapshotEvents)
    stages: dict[str, ExecutionStageRecord] = field(default_factory=dict)

    @classmethod
    def empty(
        cls, *, trigger_context: dict[str, Any] | None = None
    ) -> ExecutionSnapshot:
        return cls(
            version=SNAPSHOT_VERSION,
            context=SnapshotContext(
                trigger_context=dict(trigger_context or {}), execution_context={}
            ),
            workflow=SnapshotWorkflow(),
            events=SnapshotEvents(),
            stages={},
        )

    @classmethod
    def load(cls, payload: object) -> ExecutionSnapshot | None:
        if not isinstance(payload, dict):
            return None
        if payload.get("version") != SNAPSHOT_VERSION:
            return None
        context = _decode_context(payload.get("context"))
        workflow = _decode_workflow(payload.get("workflow"))
        events = _decode_events(payload.get("events"))
        stages = _decode_stages(payload.get("stages"))
        if context is None or workflow is None or events is None or stages is None:
            return None
        return cls(
            version=SNAPSHOT_VERSION,
            context=context,
            workflow=workflow,
            events=events,
            stages=stages,
        )

    @classmethod
    def require(
        cls, payload: object | None, *, allow_empty: bool = True
    ) -> ExecutionSnapshot:
        if payload is None:
            if allow_empty:
                return cls.empty()
            raise ValueError("Execution snapshot is required")
        if allow_empty and isinstance(payload, dict) and not payload:
            return cls.empty()
        snapshot = cls.load(payload)
        if snapshot is None:
            raise ValueError("Unsupported execution snapshot version/shape")
        return snapshot

    def dump(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "context": {
                "trigger_context": dict(self.context.trigger_context),
                "execution_context": dict(self.context.execution_context),
            },
            "workflow": {
                "outcome": self.workflow.outcome,
                "attempts": self.workflow.attempts,
                "summary": list(self.workflow.summary),
                "blocker_message": self.workflow.blocker_message,
                "requeue_target": self.workflow.requeue_target,
                "requeue_reason": self.workflow.requeue_reason,
            },
            "events": {
                "stage_updates": _normalize_stage_update_payloads(
                    self.events.stage_updates
                ),
                "live_stage_updates": [
                    dict(item)
                    for item in self.events.live_stage_updates
                    if isinstance(item, dict)
                ],
                "stage_trace": [
                    dict(item)
                    for item in self.events.stage_trace
                    if isinstance(item, dict)
                ],
                "workstream_trace": [
                    dict(item)
                    for item in self.events.workstream_trace
                    if isinstance(item, dict)
                ],
            },
            "stages": {
                stage: record.to_payload() for stage, record in self.stages.items()
            },
        }

    def trigger_context(self) -> dict[str, Any]:
        return dict(self.context.trigger_context)

    def apply_execution_context(self, execution_context: dict[str, str] | None) -> None:
        if execution_context:
            self.context.execution_context = dict(execution_context)

    def append_live_stage_update(
        self, *, stage: str, recorded_at: str | None = None
    ) -> None:
        updates = list(self.events.live_stage_updates or [])
        updates.append(
            {
                "stage": str(stage).strip(),
                "recorded_at": recorded_at or datetime.now(timezone.utc).isoformat(),
            }
        )
        self.events.live_stage_updates = updates[-40:]

    def apply_stage_checkpoint(self, checkpoint: WorkflowStageCheckpoint) -> None:
        artifact = encode_stage_checkpoint_artifact(checkpoint)
        self.stages[checkpoint.stage] = ExecutionStageRecord(
            attempt=checkpoint.attempt,
            status=checkpoint.status,
            summary=checkpoint.summary,
            completed_at=datetime.now(timezone.utc).isoformat(),
            artifact=artifact,
        )

    def apply_workflow_result(
        self,
        *,
        workflow_result: WorkflowResult,
        stage_updates: list[WorkerStageUpdate | dict[str, str]],
    ) -> None:
        self.workflow = SnapshotWorkflow(
            outcome=workflow_result.outcome,
            attempts=workflow_result.attempts,
            summary=list(workflow_result.summary or []),
            blocker_message=workflow_result.blocker_message,
            requeue_target=(
                workflow_result.requeue_target.value
                if workflow_result.requeue_target is not None
                else None
            ),
            requeue_reason=workflow_result.requeue_reason,
        )
        self.events.stage_updates = _normalize_stage_update_payloads(stage_updates)
        self.events.stage_trace = [
            dict(item)
            for item in workflow_result.orchestration_stage_trace
            if isinstance(item, dict)
        ]
        self.events.workstream_trace = [
            dict(item)
            for item in workflow_result.orchestration_workstream_trace
            if isinstance(item, dict)
        ]

    def plan(self) -> PmPlan | None:
        record = self.stages.get("pm")
        if record is None:
            return None
        return decode_pm_plan_payload(record.artifact)

    def dev_result(self) -> DevResult | None:
        record = self.stages.get("dev")
        if record is None or not isinstance(record.artifact, dict):
            return None
        return decode_dev_result_payload(record.artifact)

    def test_result(self) -> TestResult | None:
        record = self.stages.get("test")
        if record is None or not isinstance(record.artifact, dict):
            return None
        return decode_test_result_payload(record.artifact)

    def review_result(self) -> ReviewResult | None:
        record = self.stages.get("review")
        if record is None or not isinstance(record.artifact, dict):
            return None
        return decode_review_result_payload(record.artifact)

    def qa_result(self) -> QaResult | None:
        record = self.stages.get("qa")
        if record is None or not isinstance(record.artifact, dict):
            return None
        return decode_qa_result_payload(record.artifact)


def _normalize_stage_update_payloads(
    stage_updates: list[WorkerStageUpdate | dict[str, str]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for item in stage_updates:
        normalized = (
            item
            if isinstance(item, WorkerStageUpdate)
            else WorkerStageUpdate.load(item)
        )
        if normalized is None:
            continue
        payloads.append(normalized.to_payload())
    return payloads


def load_trigger_context_from_plan(plan: object | None) -> dict[str, Any] | None:
    if plan is None or (isinstance(plan, dict) and not plan):
        return None
    snapshot = ExecutionSnapshot.load(plan)
    if snapshot is None:
        return None
    trigger_context = snapshot.trigger_context()
    return trigger_context or None


def load_parsed_trigger_context_from_plan(plan: object | None) -> TriggerContext | None:
    trigger_context = load_trigger_context_from_plan(plan)
    if trigger_context is None:
        return None
    return decode_trigger_context(trigger_context)


def load_github_pr_remediation_context_from_plan(
    plan: object | None,
) -> GithubPrRemediationTriggerContext | None:
    parsed = load_parsed_trigger_context_from_plan(plan)
    if isinstance(parsed, GithubPrRemediationTriggerContext):
        return parsed
    return None


def require_github_pr_remediation_context_from_plan(
    plan: object | None,
) -> GithubPrRemediationTriggerContext | None:
    trigger_context = require_trigger_context_from_plan(plan)
    if trigger_context is None:
        return None
    try:
        return require_github_pr_remediation_context(trigger_context)
    except TriggerContextDecodeError as exc:
        raise ValueError(str(exc)) from exc


def require_trigger_context_from_plan(plan: object | None) -> dict[str, Any] | None:
    if plan is None or (isinstance(plan, dict) and not plan):
        return None
    snapshot = ExecutionSnapshot.load(plan)
    if snapshot is None:
        raise ValueError("Unsupported execution snapshot version/shape")
    trigger_context = snapshot.trigger_context()
    return trigger_context or None


def _decode_context(raw: object) -> SnapshotContext | None:
    if not isinstance(raw, dict):
        return None
    trigger_context = raw.get("trigger_context")
    execution_context = raw.get("execution_context")
    if not isinstance(trigger_context, dict) or not isinstance(execution_context, dict):
        return None
    return SnapshotContext(
        trigger_context=dict(trigger_context),
        execution_context=dict(execution_context),
    )


def _decode_workflow(raw: object) -> SnapshotWorkflow | None:
    if not isinstance(raw, dict):
        return None
    attempts = raw.get("attempts")
    summary = raw.get("summary")
    outcome = raw.get("outcome")
    if outcome is not None and (
        not isinstance(outcome, str) or outcome not in _VALID_WORKFLOW_OUTCOMES
    ):
        return None
    parsed_summary = _parse_string_list(summary, require_non_empty=False)
    if not isinstance(attempts, int) or attempts < 0 or parsed_summary is None:
        return None
    blocker_message = _parse_optional_string(raw.get("blocker_message"))
    requeue_target = _parse_optional_string(raw.get("requeue_target"))
    requeue_reason = _parse_optional_string(raw.get("requeue_reason"))
    if outcome == "requeue" and requeue_reason is None:
        return None
    if outcome != "requeue" and (
        requeue_target is not None or requeue_reason is not None
    ):
        return None
    return SnapshotWorkflow(
        outcome=outcome,
        attempts=attempts,
        summary=parsed_summary,
        blocker_message=blocker_message,
        requeue_target=requeue_target,
        requeue_reason=requeue_reason,
    )


def _decode_events(raw: object) -> SnapshotEvents | None:
    if not isinstance(raw, dict):
        return None
    stage_updates = _parse_dict_list(raw.get("stage_updates"))
    live_stage_updates = _parse_dict_list(raw.get("live_stage_updates"))
    stage_trace = _parse_dict_list(raw.get("stage_trace"))
    workstream_trace = _parse_dict_list(raw.get("workstream_trace"))
    if (
        stage_updates is None
        or live_stage_updates is None
        or stage_trace is None
        or workstream_trace is None
    ):
        return None
    return SnapshotEvents(
        stage_updates=stage_updates,
        live_stage_updates=live_stage_updates,
        stage_trace=stage_trace,
        workstream_trace=workstream_trace,
    )


def _decode_stages(raw: object) -> dict[str, ExecutionStageRecord] | None:
    if not isinstance(raw, dict):
        return None
    normalized: dict[str, ExecutionStageRecord] = {}
    for stage_name, stage_payload in raw.items():
        try:
            stage = StageName(stage_name)
        except ValueError:
            return None
        record = _decode_stage_record(stage=stage, raw=stage_payload)
        if record is None:
            return None
        normalized[stage.value] = record
    return normalized


def _decode_stage_record(
    *, stage: StageName, raw: object
) -> ExecutionStageRecord | None:
    if not isinstance(raw, dict):
        return None
    attempt = raw.get("attempt")
    status = _parse_optional_string(raw.get("status"))
    summary = _parse_optional_string(raw.get("summary"))
    completed_at = _parse_optional_string(raw.get("completed_at"))
    if (
        not isinstance(attempt, int)
        or attempt < 1
        or status is None
        or summary is None
        or completed_at is None
    ):
        return None
    artifact = raw.get("artifact")
    normalized_artifact: dict[str, Any] | None = None
    if artifact is not None:
        if not isinstance(artifact, dict):
            return None
        if stage == StageName.PM and decode_pm_plan_payload(artifact) is None:
            return None
        if stage == StageName.DEV and decode_dev_result_payload(artifact) is None:
            return None
        if stage == StageName.TEST and decode_test_result_payload(artifact) is None:
            return None
        if stage == StageName.REVIEW and decode_review_result_payload(artifact) is None:
            return None
        if stage == StageName.QA and decode_qa_result_payload(artifact) is None:
            return None
        normalized_artifact = dict(artifact)
    return ExecutionStageRecord(
        attempt=attempt,
        status=status,
        summary=summary,
        completed_at=completed_at,
        artifact=normalized_artifact,
    )


def _parse_dict_list(value: object) -> list[dict[str, Any]] | None:
    if not isinstance(value, list):
        return None
    parsed: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        parsed.append(dict(item))
    return parsed


def _parse_optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    if not value.strip():
        return None
    return value


def _parse_string_list(value: object, *, require_non_empty: bool) -> list[str] | None:
    if not isinstance(value, list):
        return None
    parsed: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        if not item.strip():
            return None
        parsed.append(item)
    if require_non_empty and not parsed:
        return None
    return parsed
