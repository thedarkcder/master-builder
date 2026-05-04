from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.parent_feature_workflow.operations import PARENT_WU_PM_DECISION_MODEL
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime.invocation import AgentInvocationContext, invoke_runtime_json
from orchestrator.core.runtime.payload_models import (
    PMDecisionRequestPayload,
    PMDecisionResolutionSetPayload,
    TechnicalDecisionPayload,
)
from orchestrator.core.runtime.runtime import CodexRuntime, CodexRuntimeError
from orchestrator.core.workflow.work_units import run_work_unit, workflow_work_unit_input_fingerprint
from orchestrator.storage.models import WorkflowOperation, WorkflowOperationAttempt


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@dataclass(frozen=True)
class PMDecisionResolutionRequest:
    tenant_id: str
    project_id: str | None
    parent_issue_key: str
    parent_summary: str
    parent_description: str
    product_brief: dict[str, Any]
    planning_package: dict[str, Any]
    technical_decisions: tuple[TechnicalDecisionPayload, ...]
    pm_decision_requests: tuple[PMDecisionRequestPayload, ...]
    conversation_history: tuple[dict[str, Any], ...]
    workflow_id: str | None
    operation_id: str | None
    attempt_id: str | None
    attempt: int | None


class PMDecisionResolutionService:
    def resolve(
        self,
        *,
        session: Session | None,
        settings: Any | None,
        runtime: CodexRuntime,
        request: PMDecisionResolutionRequest,
    ) -> PMDecisionResolutionSetPayload:
        _ = (session, settings)
        if not request.pm_decision_requests:
            return PMDecisionResolutionSetPayload(
                resolved_decisions=(),
                stakeholder_escalations=(),
                updated_planning_context={},
            )
        if session is None:
            raise RuntimeError("PM decision resolution requires a database session")
        if not request.operation_id or not request.attempt_id:
            raise RuntimeError("PM decision resolution requires operation_id and attempt_id")
        operation = session.get(WorkflowOperation, request.operation_id)
        attempt = session.get(WorkflowOperationAttempt, request.attempt_id)
        if operation is None:
            raise RuntimeError(f"Workflow operation {request.operation_id} is missing for PM decision resolution")
        if attempt is None:
            raise RuntimeError(f"Workflow operation attempt {request.attempt_id} is missing for PM decision resolution")

        def _execute() -> PMDecisionResolutionSetPayload:
            return self._invoke_runtime(runtime=runtime, session=session, request=request)

        input_payload = {
            "parent_issue_key": request.parent_issue_key,
            "parent_summary": request.parent_summary,
            "parent_description": request.parent_description,
            "product_brief": request.product_brief,
            "planning_package": request.planning_package,
            "technical_decisions": [decision.to_payload() for decision in request.technical_decisions],
            "pm_decision_requests": [item.to_payload() for item in request.pm_decision_requests],
            "conversation_history": list(request.conversation_history),
        }
        input_hash = workflow_work_unit_input_fingerprint(input_payload)
        return run_work_unit(
            session,
            operation=operation,
            operation_attempt=attempt,
            unit_key=PARENT_WU_PM_DECISION_MODEL,
            idempotency_key=f"{request.parent_issue_key}:pm_decision_resolution:{input_hash}",
            input_payload=input_payload,
            execute=lambda _context: _execute(),
            serialize=lambda result: result.to_payload(),
            deserialize=lambda payload: PMDecisionResolutionSetPayload.from_payload(
                payload,
                expected_request_ids=tuple(pm_request.request_id for pm_request in request.pm_decision_requests),
                context="PM decision resolution stored payload",
            ),
        )

    def _invoke_runtime(
        self,
        *,
        runtime: CodexRuntime,
        session: Session | None,
        request: PMDecisionResolutionRequest,
    ) -> PMDecisionResolutionSetPayload:
        invocation_context = AgentInvocationContext(
            channel="system",
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            command="pm",
            stage="pm_decision_resolution",
            working_dir=".",
            run_id=None,
            invocation_id=f"pm-decision-resolution:{request.parent_issue_key}",
            workflow_id=request.workflow_id,
            operation_id=request.operation_id,
            attempt=request.attempt,
            attempt_id=request.attempt_id,
            issue_key=request.parent_issue_key,
            db_session=session,
        )
        payload = invoke_runtime_json(
            runtime=runtime,
            context=invocation_context,
            system_prompt=render_prompt("workflow/pm_decision_resolution_system.j2"),
            user_prompt=render_prompt(
                "workflow/pm_decision_resolution_user.j2",
                parent_issue_key=request.parent_issue_key,
                parent_summary=request.parent_summary,
                parent_description=request.parent_description,
                product_brief_json=_json_dump(request.product_brief),
                planning_package_json=_json_dump(request.planning_package),
                technical_decisions_json=_json_dump(
                    [decision.to_payload() for decision in request.technical_decisions]
                ),
                pm_decision_requests_json=_json_dump(
                    [pm_request.to_payload() for pm_request in request.pm_decision_requests]
                ),
                conversation_history_json=_json_dump(list(request.conversation_history)),
            ),
        )
        try:
            return PMDecisionResolutionSetPayload.from_payload(
                payload,
                expected_request_ids=tuple(pm_request.request_id for pm_request in request.pm_decision_requests),
                context="PM decision resolution payload",
            )
        except RuntimeError as exc:
            raise CodexRuntimeError(str(exc)) from exc
