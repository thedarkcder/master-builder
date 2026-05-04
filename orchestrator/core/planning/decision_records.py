from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy.orm import Session

from orchestrator.core.runtime.payload_models import (
    PMDecisionRequestPayload,
    PMDecisionResolutionPayload,
    StakeholderEscalationPayload,
    TechnicalDecisionPayload,
)
from orchestrator.storage.models import PlanningDecisionRecord


PLANNING_DECISION_LANE_TECHNICAL = "technical"
PLANNING_DECISION_LANE_PM = "pm"
PLANNING_DECISION_LANE_STAKEHOLDER = "stakeholder"


class PlanningDecisionRecordStore:
    def __init__(self, *, session: Session) -> None:
        self._session = session

    def record_technical_decisions(
        self,
        *,
        tenant_id: str,
        project_id: str | None,
        workflow_id: str,
        source_operation_id: str | None,
        source_attempt_id: str | None,
        parent_issue_key: str,
        source_stage: str | None,
        decisions: tuple[TechnicalDecisionPayload, ...],
    ) -> None:
        for decision in decisions:
            self._upsert(
                tenant_id=tenant_id,
                project_id=project_id,
                workflow_id=workflow_id,
                source_operation_id=source_operation_id,
                source_attempt_id=source_attempt_id,
                parent_issue_key=parent_issue_key,
                lane=PLANNING_DECISION_LANE_TECHNICAL,
                status="selected",
                source_stage=source_stage,
                external_key=decision.decision_id,
                payload=decision.to_payload(),
            )

    def record_pm_requests(
        self,
        *,
        tenant_id: str,
        project_id: str | None,
        workflow_id: str,
        source_operation_id: str | None,
        source_attempt_id: str | None,
        parent_issue_key: str,
        source_stage: str | None,
        requests: tuple[PMDecisionRequestPayload, ...],
    ) -> None:
        for request in requests:
            self._upsert(
                tenant_id=tenant_id,
                project_id=project_id,
                workflow_id=workflow_id,
                source_operation_id=source_operation_id,
                source_attempt_id=source_attempt_id,
                parent_issue_key=parent_issue_key,
                lane=PLANNING_DECISION_LANE_PM,
                status="requested",
                source_stage=source_stage,
                external_key=request.request_id,
                payload=request.to_payload(),
            )

    def record_pm_resolutions(
        self,
        *,
        tenant_id: str,
        project_id: str | None,
        workflow_id: str,
        source_operation_id: str | None,
        source_attempt_id: str | None,
        parent_issue_key: str,
        resolutions: tuple[PMDecisionResolutionPayload, ...],
    ) -> None:
        for resolution in resolutions:
            self._upsert(
                tenant_id=tenant_id,
                project_id=project_id,
                workflow_id=workflow_id,
                source_operation_id=source_operation_id,
                source_attempt_id=source_attempt_id,
                parent_issue_key=parent_issue_key,
                lane=PLANNING_DECISION_LANE_PM,
                status="resolved",
                source_stage="pm_decision_resolution",
                external_key=resolution.request_id,
                payload=resolution.to_payload(),
            )

    def record_stakeholder_escalations(
        self,
        *,
        tenant_id: str,
        project_id: str | None,
        workflow_id: str,
        source_operation_id: str | None,
        source_attempt_id: str | None,
        parent_issue_key: str,
        escalations: tuple[StakeholderEscalationPayload, ...],
    ) -> None:
        for escalation in escalations:
            self._upsert(
                tenant_id=tenant_id,
                project_id=project_id,
                workflow_id=workflow_id,
                source_operation_id=source_operation_id,
                source_attempt_id=source_attempt_id,
                parent_issue_key=parent_issue_key,
                lane=PLANNING_DECISION_LANE_STAKEHOLDER,
                status="escalated",
                source_stage="pm_decision_resolution",
                external_key=escalation.escalation_id,
                payload=escalation.to_payload(),
            )

    def _upsert(
        self,
        *,
        tenant_id: str,
        project_id: str | None,
        workflow_id: str,
        source_operation_id: str | None,
        source_attempt_id: str | None,
        parent_issue_key: str,
        lane: str,
        status: str,
        source_stage: str | None,
        external_key: str,
        payload: dict[str, object],
    ) -> None:
        normalized_external_key = str(external_key or "").strip()
        if not normalized_external_key:
            raise ValueError("Planning decision record requires external_key")
        now = datetime.now(UTC)
        existing = (
            self._session.query(PlanningDecisionRecord)
            .filter(
                PlanningDecisionRecord.tenant_id == tenant_id,
                PlanningDecisionRecord.workflow_id == workflow_id,
                PlanningDecisionRecord.lane == lane,
                PlanningDecisionRecord.external_key == normalized_external_key,
            )
            .one_or_none()
        )
        if existing is None:
            existing = PlanningDecisionRecord(
                record_id=uuid4().hex,
                tenant_id=tenant_id,
                project_id=project_id,
                workflow_id=workflow_id,
                source_operation_id=source_operation_id,
                source_attempt_id=source_attempt_id,
                parent_issue_key=parent_issue_key,
                lane=lane,
                status=status,
                source_stage=source_stage,
                external_key=normalized_external_key,
                payload_json=payload,
                created_at=now,
                updated_at=now,
            )
            self._session.add(existing)
            return
        existing.project_id = project_id
        existing.source_operation_id = source_operation_id
        existing.source_attempt_id = source_attempt_id
        existing.parent_issue_key = parent_issue_key
        existing.status = status
        existing.source_stage = source_stage
        existing.payload_json = payload
        existing.updated_at = now
