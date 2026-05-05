from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from orchestrator.core.planning.decision_records import PlanningDecisionRecordStore
from orchestrator.core.runtime.payload_models import TechnicalDecision, TechnicalDecisionOption
from orchestrator.storage.models import PlanningDecisionRecord


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE planning_decision_records (
                    record_id VARCHAR(64) PRIMARY KEY,
                    tenant_id VARCHAR(128) NOT NULL,
                    project_id VARCHAR(128),
                    workflow_id VARCHAR(128) NOT NULL,
                    source_operation_id VARCHAR(64),
                    source_attempt_id VARCHAR(64),
                    parent_issue_key VARCHAR(64) NOT NULL,
                    lane VARCHAR(32) NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    source_stage VARCHAR(64) NOT NULL,
                    external_key VARCHAR(128) NOT NULL,
                    payload_json JSON NOT NULL,
                    created_at DATETIME NOT NULL,
                    updated_at DATETIME NOT NULL
                )
                """
            )
        )
    return sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)


def _decision(*, decision_id: str, question: str) -> TechnicalDecision:
    option = TechnicalDecisionOption(
        option_id=f"{decision_id}-option",
        title="Recommended",
        description="Use the explicit implementation option.",
        benefits=("Clear implementation boundary",),
        risks=("Requires review",),
    )
    return TechnicalDecision(
        decision_id=decision_id,
        area="architecture",
        question=question,
        options=(option,),
        selected_option_id=option.option_id,
        rationale="This is the selected implementation decision.",
        evidence=("Planning context",),
        confidence="high",
        product_impact="none",
    )


def test_decision_records_are_stage_scoped_not_workflow_global() -> None:
    session_factory = _session_factory()
    with session_factory() as session:
        store = PlanningDecisionRecordStore(session=session)

        store.record_technical_decisions(
            tenant_id="example",
            project_id="project-1",
            workflow_id="parent_planning:MAB-243",
            source_operation_id="operation-1",
            source_attempt_id="attempt-1",
            parent_issue_key="MAB-243",
            source_stage="engineering_planning",
            decisions=(_decision(decision_id="TD-001", question="Engineering decision?"),),
        )
        store.record_technical_decisions(
            tenant_id="example",
            project_id="project-1",
            workflow_id="parent_planning:MAB-243",
            source_operation_id="operation-1",
            source_attempt_id="attempt-1",
            parent_issue_key="MAB-243",
            source_stage="test_planning",
            decisions=(_decision(decision_id="TD-001", question="Testing decision?"),),
        )

        rows = (
            session.query(PlanningDecisionRecord)
            .filter(PlanningDecisionRecord.external_key == "TD-001")
            .order_by(PlanningDecisionRecord.source_stage)
            .all()
        )

    assert [(row.source_stage, row.payload_json["question"]) for row in rows] == [
        ("engineering_planning", "Engineering decision?"),
        ("test_planning", "Testing decision?"),
    ]


def test_decision_records_update_same_stage_identity() -> None:
    session_factory = _session_factory()
    with session_factory() as session:
        store = PlanningDecisionRecordStore(session=session)
        common_kwargs = {
            "tenant_id": "example",
            "project_id": "project-1",
            "workflow_id": "parent_planning:MAB-243",
            "source_operation_id": "operation-1",
            "source_attempt_id": "attempt-1",
            "parent_issue_key": "MAB-243",
            "source_stage": "engineering_planning",
        }
        store.record_technical_decisions(
            **common_kwargs,
            decisions=(_decision(decision_id="TD-001", question="Original decision?"),),
        )
        store.record_technical_decisions(
            **common_kwargs,
            decisions=(_decision(decision_id="TD-001", question="Updated decision?"),),
        )

        rows = session.query(PlanningDecisionRecord).all()

    assert len(rows) == 1
    assert rows[0].payload_json["question"] == "Updated decision?"


def test_decision_records_require_source_stage() -> None:
    session_factory = _session_factory()
    with session_factory() as session:
        store = PlanningDecisionRecordStore(session=session)

        with pytest.raises(ValueError, match="source_stage"):
            store.record_technical_decisions(
                tenant_id="example",
                project_id="project-1",
                workflow_id="parent_planning:MAB-243",
                source_operation_id="operation-1",
                source_attempt_id="attempt-1",
                parent_issue_key="MAB-243",
                source_stage=None,
                decisions=(_decision(decision_id="TD-001", question="Engineering decision?"),),
            )

