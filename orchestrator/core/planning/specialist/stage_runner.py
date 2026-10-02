from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

from sqlalchemy.orm import Session

from orchestrator.core.planning.specialist.constants import (
    SPECIALIST_STAGE_CONTRACT_ATTEMPTS,
)
from orchestrator.core.planning.specialist.contract_parser import StageContractParser
from orchestrator.core.planning.specialist.models import (
    PlanningStageDefinition,
    RetryableSpecialistPlanningContractError,
    SpecialistPlanningRequest,
    SpecialistPlanningStageResult,
)
from orchestrator.core.prompt_domain_models import prompt_domain_model_for_template
from orchestrator.core.prompt_templates import render_prompt
from orchestrator.core.runtime.invocation import AgentInvocationContext
from orchestrator.core.runtime.stage_session import RuntimeStageSession
from orchestrator.core.workflow.work_units import (
    run_work_unit,
    workflow_work_unit_input_fingerprint,
)
from orchestrator.storage.models import WorkflowOperation, WorkflowOperationAttempt


def _json_dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _contract_repair_user_prompt(
    *,
    original_user_prompt: str,
    stage: PlanningStageDefinition,
    domain_model: dict[str, object],
    contract_error: str,
    invalid_payload: dict[str, Any],
) -> str:
    return "\n\n".join(
        (
            original_user_prompt,
            "CONTRACT REPAIR REQUIRED",
            f"The previous {stage.role_label} response violated the required JSON contract.",
            f"Schema error: {contract_error}",
            "Return the full corrected JSON object only. Do not omit any required fields. Do not include markdown.",
            f"Domain model JSON: {_json_dump(domain_model)}",
            f"Previous invalid JSON: {_json_dump(invalid_payload)}",
        )
    )


def _planning_stage_user_context(
    *,
    request: SpecialistPlanningRequest,
    stage: PlanningStageDefinition,
    stage_session: RuntimeStageSession,
    domain_model: dict[str, object],
) -> dict[str, object]:
    return {
        "parent_issue_key": request.parent_issue_key,
        "parent_summary": request.parent_summary,
        "parent_description": request.parent_description,
        "product_brief_json": _json_dump(request.product_brief),
        "project_keys_json": _json_dump(list(request.project_keys)),
        "related_issues_json": _json_dump(list(request.related_issues)),
        "status_counts_json": _json_dump(request.status_counts),
        "github_context_json": _json_dump(request.github_context),
        "conversation_history_json": _json_dump(list(request.conversation_history)),
        "stage_state": stage.planning_state,
        "persona_id": stage.persona_id,
        "role_label": stage.role_label,
        "domain_model": domain_model,
        **stage_session.tooling.governed_native_prompt_context(),
    }


class SpecialistPlanningStageRunner:
    def __init__(self, *, parser: StageContractParser | None = None) -> None:
        self._parser = parser or StageContractParser()

    def run_stage(
        self,
        *,
        session: Session | None,
        settings: Any | None,
        runtime: object,
        runtime_for_selector: Callable[[str], object] | None,
        request: SpecialistPlanningRequest,
        stage: PlanningStageDefinition,
    ) -> SpecialistPlanningStageResult:
        selected_runtime = runtime
        if callable(runtime_for_selector):
            selected_runtime = runtime_for_selector(stage.selector)
            if selected_runtime is None:
                raise RuntimeError(
                    f"Runtime selector returned no runtime for {stage.selector}"
                )
        invocation_context = AgentInvocationContext(
            channel="system",
            tenant_id=request.tenant_id,
            project_id=request.project_id,
            command="pm",
            stage=stage.planning_state,
            working_dir=request.working_dir,
            workflow_id=request.workflow_id,
            operation_id=request.operation_id,
            attempt_id=request.attempt_id,
            issue_key=request.parent_issue_key,
            attempt=request.attempt,
            reasoning_effort=stage.reasoning_effort,
            db_session=session,
        )
        stage_session = RuntimeStageSession.create(
            runtime=selected_runtime,
            context=invocation_context,
            policy_stage=stage.planning_state,
            session=session,
            settings=settings,
            issue_key=request.parent_issue_key,
        )
        domain_model = prompt_domain_model_for_template(stage.system_prompt_template)
        if domain_model is None:
            raise RuntimeError(
                f"No domain model registered for {stage.system_prompt_template}"
            )
        system_prompt = render_prompt(
            stage.system_prompt_template, domain_model=domain_model
        )
        user_prompt = render_prompt(
            stage.user_prompt_template,
            **_planning_stage_user_context(
                request=request,
                stage=stage,
                stage_session=stage_session,
                domain_model=domain_model,
            ),
        )

        def _invoke() -> SpecialistPlanningStageResult:
            return self._invoke_until_contract_valid(
                stage_session=stage_session,
                stage=stage,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                domain_model=domain_model,
            )

        if request.operation_id or request.attempt_id:
            if session is None:
                raise RuntimeError(
                    "Specialist planning workflow attempts require a database session"
                )
            if not request.operation_id or not request.attempt_id:
                raise RuntimeError(
                    "Specialist planning workflow attempts require operation_id and attempt_id"
                )
            operation = session.get(WorkflowOperation, request.operation_id)
            attempt = session.get(WorkflowOperationAttempt, request.attempt_id)
            if operation is None:
                raise RuntimeError(
                    f"Workflow operation {request.operation_id} is missing for specialist planning"
                )
            if attempt is None:
                raise RuntimeError(
                    f"Workflow operation attempt {request.attempt_id} is missing for specialist planning"
                )
            unit_key = str(
                request.work_unit_keys_by_stage.get(stage.planning_state) or ""
            ).strip()
            if not unit_key:
                raise RuntimeError(
                    f"No specialist planning work unit is declared for operation "
                    f"{operation.operation_type} stage {stage.planning_state}"
                )
            input_payload = {
                "parent_issue_key": request.parent_issue_key,
                "parent_summary": request.parent_summary,
                "parent_description": request.parent_description,
                "product_brief": request.product_brief,
                "project_keys": list(request.project_keys),
                "related_issues": list(request.related_issues),
                "status_counts": request.status_counts,
                "github_context": request.github_context,
                "conversation_history": list(request.conversation_history),
                "stage": stage.planning_state,
                "persona_id": stage.persona_id,
            }
            input_hash = workflow_work_unit_input_fingerprint(input_payload)
            return run_work_unit(
                session,
                operation=operation,
                operation_attempt=attempt,
                unit_key=unit_key,
                idempotency_key=f"{request.parent_issue_key}:{stage.planning_state}:{input_hash}",
                input_payload=input_payload,
                execute=lambda _context: _invoke(),
                serialize=lambda result: result.to_payload(),
                deserialize=lambda payload: self._parser.parse(
                    stage=stage, payload=payload
                ),
            )

        return _invoke()

    def _invoke_until_contract_valid(
        self,
        *,
        stage_session: RuntimeStageSession,
        stage: PlanningStageDefinition,
        system_prompt: str,
        user_prompt: str,
        domain_model: dict[str, object],
    ) -> SpecialistPlanningStageResult:
        contract_error: str | None = None
        last_payload: dict[str, Any] | None = None
        for attempt_number in range(1, SPECIALIST_STAGE_CONTRACT_ATTEMPTS + 1):
            stage_user_prompt = user_prompt
            if contract_error is not None and last_payload is not None:
                stage_user_prompt = _contract_repair_user_prompt(
                    original_user_prompt=user_prompt,
                    stage=stage,
                    domain_model=domain_model,
                    contract_error=contract_error,
                    invalid_payload=last_payload,
                )
            payload = stage_session.invoke_json(
                system_prompt=system_prompt,
                user_prompt=stage_user_prompt,
            )
            last_payload = payload
            try:
                return self._parser.parse(stage=stage, payload=payload)
            except RuntimeError as exc:
                contract_error = str(exc)
                if attempt_number >= SPECIALIST_STAGE_CONTRACT_ATTEMPTS:
                    raise RetryableSpecialistPlanningContractError(
                        contract_error
                    ) from exc
        raise RetryableSpecialistPlanningContractError(
            f"{stage.planning_state} payload contract retry exhausted without a parse result"
        )
