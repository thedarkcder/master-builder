from __future__ import annotations

import logging

from orchestrator.core.workflow.definition import (
    WorkflowDefinition,
    WorkflowDefinitionRegistry,
    WorkflowRetryPolicyDefinition,
    WorkflowStepKind,
    WorkflowWorkUnitIdempotencyPolicy,
    WorkflowWorkUnitKind,
    WorkflowWorkUnitRetryPolicy,
    infer_workflow_steps,
    infer_workflow_work_units,
    workflow_step,
    workflow_work_unit,
)

ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION = "run_attempt_execution"
ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME = "human_input_resume"
ISSUE_EXECUTION_STEP_JIRA_COMMENT_PROJECTION = "jira_comment_projection"
ISSUE_EXECUTION_STEP_DISCORD_FOLLOWUP_PROJECTION = "discord_followup_projection"
ISSUE_EXECUTION_STEP_NOTIFICATION_EMIT = "notification_emit"
PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION = "run_attempt_execution"
PR_REMEDIATION_STEP_HUMAN_INPUT_RESUME = "human_input_resume"
PR_REMEDIATION_STEP_NOTIFICATION_EMIT = "notification_emit"
DEMO_PROOF_STEP_PREVIEW_LEASE = "preview_lease"
DEMO_PROOF_STEP_RELEASE = "release"
DEMO_PROOF_STEP_RECORDING = "recording"
DEMO_PROOF_STEP_EVIDENCE_UPLOAD = "evidence_upload"
DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE = "pr_evidence_update"
DEMO_PROOF_STEP_PREVIEW_CLEANUP = "preview_cleanup"

_DEMO_PROOF_WORK_UNIT_RETRY = WorkflowWorkUnitRetryPolicy(
    max_attempts=3,
    initial_interval_seconds=30,
    max_interval_seconds=300,
    backoff_coefficient=2.0,
)


class IssueExecutionWorkflow:
    @workflow_work_unit(
        key="run_attempt_execution.prepare",
        step_key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Prepare run",
        kind=WorkflowWorkUnitKind.PURE_COMPUTE,
        idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=True),
    )
    @workflow_work_unit(
        key="run_attempt_execution.repo_setup",
        step_key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Prepare repository",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_work_unit(
        key="run_attempt_execution.runtime_invocation",
        step_key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Runtime invocation",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_work_unit(
        key="run_attempt_execution.finalize",
        step_key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Finalize run",
        kind=WorkflowWorkUnitKind.ASSEMBLY,
        idempotency_policy=WorkflowWorkUnitIdempotencyPolicy(required=True),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Run attempt execution",
        kind=WorkflowStepKind.BUSINESS,
        description="Execute the claimed development run.",
    )
    def run_attempt_execution(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="human_input_resume.resume",
        step_key=ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME,
        label="Resume human input",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_HUMAN_INPUT_RESUME,
        label="Human input resume",
        kind=WorkflowStepKind.HUMAN_GATE,
        after=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Resume a run after required human input is supplied.",
    )
    def human_input_resume(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="jira_comment_projection.api",
        step_key=ISSUE_EXECUTION_STEP_JIRA_COMMENT_PROJECTION,
        label="Publish Jira comment",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_JIRA_COMMENT_PROJECTION,
        label="Jira comment projection",
        kind=WorkflowStepKind.SIDE_EFFECT,
        after=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Publish run state back to Jira when needed.",
    )
    def jira_comment_projection(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="discord_followup_projection.api",
        step_key=ISSUE_EXECUTION_STEP_DISCORD_FOLLOWUP_PROJECTION,
        label="Publish Discord follow-up",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_DISCORD_FOLLOWUP_PROJECTION,
        label="Discord follow-up projection",
        kind=WorkflowStepKind.NOTIFICATION,
        after=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Publish run follow-up state to Discord when needed.",
    )
    def discord_followup_projection(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="notification_emit.emit",
        step_key=ISSUE_EXECUTION_STEP_NOTIFICATION_EMIT,
        label="Emit notification",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_step(
        key=ISSUE_EXECUTION_STEP_NOTIFICATION_EMIT,
        label="Notification emit",
        kind=WorkflowStepKind.NOTIFICATION,
        after=ISSUE_EXECUTION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Emit user/admin notifications for run state.",
    )
    def notification_emit(self) -> None:
        raise NotImplementedError


class PrRemediationWorkflow:
    @workflow_work_unit(
        key="run_attempt_execution.pr_context_load",
        step_key=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Load PR context",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_work_unit(
        key="run_attempt_execution.runtime_invocation",
        step_key=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Runtime invocation",
        kind=WorkflowWorkUnitKind.MODEL_CALL,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_work_unit(
        key="run_attempt_execution.github_update",
        step_key=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Publish GitHub update",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_step(
        key=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        label="Run attempt execution",
        kind=WorkflowStepKind.BUSINESS,
        description="Execute the claimed PR remediation run.",
    )
    def run_attempt_execution(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="human_input_resume.resume",
        step_key=PR_REMEDIATION_STEP_HUMAN_INPUT_RESUME,
        label="Resume human input",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_step(
        key=PR_REMEDIATION_STEP_HUMAN_INPUT_RESUME,
        label="Human input resume",
        kind=WorkflowStepKind.HUMAN_GATE,
        after=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Resume PR remediation after required human input is supplied.",
    )
    def human_input_resume(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="notification_emit.emit",
        step_key=PR_REMEDIATION_STEP_NOTIFICATION_EMIT,
        label="Emit notification",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=WorkflowWorkUnitRetryPolicy(
            max_attempts=3, initial_interval_seconds=30, max_interval_seconds=300
        ),
    )
    @workflow_step(
        key=PR_REMEDIATION_STEP_NOTIFICATION_EMIT,
        label="Notification emit",
        kind=WorkflowStepKind.NOTIFICATION,
        after=PR_REMEDIATION_STEP_RUN_ATTEMPT_EXECUTION,
        required=False,
        description="Emit user/admin notifications for PR remediation state.",
    )
    def notification_emit(self) -> None:
        raise NotImplementedError


class DemoProofWorkflowDefinition:
    @workflow_work_unit(
        key="preview_lease.capacity_preflight",
        step_key=DEMO_PROOF_STEP_PREVIEW_LEASE,
        label="Verify proof capacity",
        kind=WorkflowWorkUnitKind.PURE_COMPUTE,
        description=(
            "Verify disk, provider, storage, and recorder capacity before release creation can be requested."
        ),
    )
    @workflow_work_unit(
        key="preview_lease.enforce_single_active",
        step_key=DEMO_PROOF_STEP_PREVIEW_LEASE,
        label="Enforce single active preview lease",
        kind=WorkflowWorkUnitKind.PURE_COMPUTE,
        description="Derive the proof scope and reject duplicate active preview leases before release work starts.",
    )
    @workflow_work_unit(
        key="preview_lease.acquire",
        step_key=DEMO_PROOF_STEP_PREVIEW_LEASE,
        label="Acquire preview lease",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=_DEMO_PROOF_WORK_UNIT_RETRY,
        description="Acquire or supersede the scoped preview lease for the proof scope.",
    )
    @workflow_step(
        key=DEMO_PROOF_STEP_PREVIEW_LEASE,
        label="Preview lease",
        kind=WorkflowStepKind.BUSINESS,
        retryable=True,
        description="Own the one-active-preview-lease invariant for the proof scope.",
    )
    def preview_lease(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="release.create_or_reuse",
        step_key=DEMO_PROOF_STEP_RELEASE,
        label="Create or reuse release",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_DEMO_PROOF_WORK_UNIT_RETRY,
        description="Create or reuse the real preview release attached to the acquired lease.",
    )
    @workflow_work_unit(
        key="release.wait_for_live",
        step_key=DEMO_PROOF_STEP_RELEASE,
        label="Wait for release live event",
        kind=WorkflowWorkUnitKind.SIDE_EFFECT,
        retry_policy=_DEMO_PROOF_WORK_UNIT_RETRY,
        description="Advance only from release events/signals for the leased preview release.",
    )
    @workflow_step(
        key=DEMO_PROOF_STEP_RELEASE,
        label="Release workflow",
        kind=WorkflowStepKind.INTEGRATION,
        after=DEMO_PROOF_STEP_PREVIEW_LEASE,
        retryable=True,
        description="Satisfy the preview lease with a real release and service readiness evidence.",
    )
    def release(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="recording.browser",
        step_key=DEMO_PROOF_STEP_RECORDING,
        label="Record browser walkthrough",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_DEMO_PROOF_WORK_UNIT_RETRY,
        description="Record browser proof against the real release context.",
    )
    @workflow_work_unit(
        key="recording.ios",
        step_key=DEMO_PROOF_STEP_RECORDING,
        label="Record iOS walkthrough",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_DEMO_PROOF_WORK_UNIT_RETRY,
        description="Record iOS proof against the real release context.",
    )
    @workflow_work_unit(
        key="recording.android",
        step_key=DEMO_PROOF_STEP_RECORDING,
        label="Record Android walkthrough",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_DEMO_PROOF_WORK_UNIT_RETRY,
        description="Record Android proof against the real release context.",
    )
    @workflow_step(
        key=DEMO_PROOF_STEP_RECORDING,
        label="Recording workflow",
        kind=WorkflowStepKind.INTEGRATION,
        after=DEMO_PROOF_STEP_RELEASE,
        retryable=True,
        description="Record required walkthrough variants for browser, iOS, and Android targets.",
    )
    def recording(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="evidence_upload.persist",
        step_key=DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        label="Upload evidence",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_DEMO_PROOF_WORK_UNIT_RETRY,
        description="Upload recorded videos to configured S3/MinIO-compatible artifact storage.",
    )
    @workflow_step(
        key=DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        label="Evidence upload",
        kind=WorkflowStepKind.INTEGRATION,
        after=DEMO_PROOF_STEP_RECORDING,
        retryable=True,
        description="Persist proof artifacts and verify their storage metadata.",
    )
    def evidence_upload(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="pr_evidence_update.attach_links",
        step_key=DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        label="Attach PR evidence links",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_DEMO_PROOF_WORK_UNIT_RETRY,
        description="Attach playable proof links to the pull request before ready-for-review.",
    )
    @workflow_step(
        key=DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        label="PR evidence update",
        kind=WorkflowStepKind.SIDE_EFFECT,
        after=DEMO_PROOF_STEP_EVIDENCE_UPLOAD,
        retryable=True,
        description="Publish verified evidence links to the pull request.",
    )
    def pr_evidence_update(self) -> None:
        raise NotImplementedError

    @workflow_work_unit(
        key="preview_cleanup.destroy_or_ttl",
        step_key=DEMO_PROOF_STEP_PREVIEW_CLEANUP,
        label="Destroy or TTL preview lease",
        kind=WorkflowWorkUnitKind.EXTERNAL_API,
        retry_policy=_DEMO_PROOF_WORK_UNIT_RETRY,
        description="Destroy or TTL-schedule every preview resource created by the proof lease.",
    )
    @workflow_step(
        key=DEMO_PROOF_STEP_PREVIEW_CLEANUP,
        label="Preview cleanup workflow",
        kind=WorkflowStepKind.SIDE_EFFECT,
        after=DEMO_PROOF_STEP_PR_EVIDENCE_UPDATE,
        retryable=True,
        description="Clean up or TTL every preview resource owned by the proof lease.",
    )
    def preview_cleanup(self) -> None:
        raise NotImplementedError


def normalize_workflow_retry_policy_config(raw: dict | None) -> dict[str, object]:
    config = raw if isinstance(raw, dict) else {}
    return {
        "manual_retry_enabled": bool(config.get("manual_retry_enabled", True)),
        "max_attempts": max(1, int(config.get("max_attempts") or 1)),
        "initial_interval_seconds": max(
            0, int(config.get("initial_interval_seconds") or 0)
        ),
        "max_interval_seconds": max(0, int(config.get("max_interval_seconds") or 0)),
        "backoff_coefficient": max(
            1.0, float(config.get("backoff_coefficient") or 1.0)
        ),
    }


workflow_definition_registry = WorkflowDefinitionRegistry()
_builtin_workflows_registered = False
_LEGACY_WORKFLOW_TYPES_TO_PURGE = frozenset()
_VALIDATED_WORKFLOW_STATUSES = ("queued", "running", "waiting_for_input", "failed")

logger = logging.getLogger(__name__)


def _register_builtin_workflows() -> None:
    global _builtin_workflows_registered
    if _builtin_workflows_registered:
        return
    from orchestrator.core.deployment_setup.workflow import (
        ProjectDeploymentSetupWorkflowDefinition,
    )
    from orchestrator.core.jira_project_reconciliation.workflow import (
        JiraProjectReconciliationWorkflow,
    )
    from orchestrator.core.parent_feature_workflow.planning import (
        ParentFeaturePlanningWorkflow,
    )

    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="issue_execution",
            system_key="issue_execution",
            handler_key="development_team_run",
            label="Issue execution",
            description="Durable execution for an engineering issue run.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=5,
                initial_interval_seconds=30,
                max_interval_seconds=900,
                backoff_coefficient=2.0,
            ),
            steps=infer_workflow_steps(IssueExecutionWorkflow),
            work_units=infer_workflow_work_units(IssueExecutionWorkflow),
        )
    )
    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="jira_project_reconciliation",
            system_key="jira_project_reconciliation",
            handler_key="jira_project_reconciliation",
            label="Jira project reconciliation",
            description="Durable Jira project scan, label reconciliation, and parent workflow queuing.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=3,
                initial_interval_seconds=30,
                max_interval_seconds=300,
                backoff_coefficient=2.0,
            ),
            steps=infer_workflow_steps(JiraProjectReconciliationWorkflow),
            work_units=infer_workflow_work_units(JiraProjectReconciliationWorkflow),
        )
    )
    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="project_deployment_setup",
            system_key="project_deployment_setup",
            handler_key="project_deployment_setup",
            label="Project deployment setup",
            description="Durable project deployment setup, deployable-unit discovery, and initial release.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=3,
                initial_interval_seconds=30,
                max_interval_seconds=300,
                backoff_coefficient=2.0,
            ),
            steps=infer_workflow_steps(ProjectDeploymentSetupWorkflowDefinition),
            work_units=infer_workflow_work_units(
                ProjectDeploymentSetupWorkflowDefinition
            ),
        )
    )
    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="demo_proof",
            system_key="demo_proof",
            handler_key="demo_proof",
            label="Demo proof",
            description="Durable QA demo proof workflow for leased release, recording, evidence, PR, and cleanup.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=3,
                initial_interval_seconds=30,
                max_interval_seconds=300,
                backoff_coefficient=2.0,
            ),
            capabilities={
                "independent_trigger": True,
                "preview_lease": True,
                "required_capture_targets": ("browser", "ios", "android"),
                "supported_trigger_modes": (
                    "from_run",
                    "from_pr",
                    "from_release",
                    "retry_recording",
                    "cleanup_only",
                ),
            },
            steps=infer_workflow_steps(DemoProofWorkflowDefinition),
            work_units=infer_workflow_work_units(DemoProofWorkflowDefinition),
        )
    )
    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="parent_planning",
            system_key="parent_planning",
            handler_key="jira_parent_feature",
            label="Parent planning",
            description="Durable parent feature planning and engineering child fanout.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=3,
                initial_interval_seconds=30,
                max_interval_seconds=300,
                backoff_coefficient=2.0,
            ),
            capabilities={"child_issue_links": True},
            steps=infer_workflow_steps(ParentFeaturePlanningWorkflow),
            work_units=infer_workflow_work_units(ParentFeaturePlanningWorkflow),
        )
    )
    workflow_definition_registry.register(
        WorkflowDefinition(
            workflow_type_key="pr_remediation",
            system_key="pr_remediation",
            handler_key="pr_remediation",
            label="PR remediation",
            description="Durable execution for PR remediation runs.",
            orchestration_backend="temporal",
            retry_policy=WorkflowRetryPolicyDefinition(
                manual_retry_enabled=True,
                max_attempts=5,
                initial_interval_seconds=30,
                max_interval_seconds=900,
                backoff_coefficient=2.0,
            ),
            steps=infer_workflow_steps(PrRemediationWorkflow),
            work_units=infer_workflow_work_units(PrRemediationWorkflow),
        )
    )
    _builtin_workflows_registered = True


def _ensure_builtin_workflows_registered() -> None:
    _register_builtin_workflows()


def get_workflow_type(_session=None, *, workflow_type_key: str) -> WorkflowDefinition:
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.get(workflow_type_key)


def get_workflow_type_by_system_key(
    _session=None, *, system_key: str
) -> WorkflowDefinition:
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.get_by_system_key(system_key)


def get_workflow_type_by_handler_key(
    _session=None, *, handler_key: str
) -> WorkflowDefinition:
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.get_by_handler(handler_key)


def list_workflow_types(_session=None) -> tuple[WorkflowDefinition, ...]:
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.list()


def validate_workflow_operation_type(*, workflow_type_key: str, operation_type: str):
    _ensure_builtin_workflows_registered()
    return workflow_definition_registry.validate_operation_type(
        workflow_type_key=workflow_type_key,
        operation_type=operation_type,
    )


def validate_persisted_workflow_definitions(*, session) -> None:  # noqa: ANN001
    from sqlalchemy import delete, select

    from orchestrator.storage.models import WorkflowExecution, WorkflowOperation

    if _LEGACY_WORKFLOW_TYPES_TO_PURGE:
        legacy_workflows = (
            session.execute(
                select(WorkflowExecution).where(
                    WorkflowExecution.workflow_type_key.in_(
                        tuple(_LEGACY_WORKFLOW_TYPES_TO_PURGE)
                    ),
                    WorkflowExecution.status.in_(_VALIDATED_WORKFLOW_STATUSES),
                )
            )
            .scalars()
            .all()
        )
        if legacy_workflows:
            legacy_ids = [workflow.workflow_id for workflow in legacy_workflows]
            session.execute(
                delete(WorkflowExecution).where(
                    WorkflowExecution.workflow_id.in_(legacy_ids)
                )
            )
            session.commit()
            logger.warning(
                "purged_legacy_workflow_executions workflow_type_keys=%s workflow_ids=%s",
                sorted(_LEGACY_WORKFLOW_TYPES_TO_PURGE),
                legacy_ids,
                extra={
                    "event_type": "workflow_definition_repair",
                    "metadata": {
                        "workflow_type_keys": sorted(_LEGACY_WORKFLOW_TYPES_TO_PURGE),
                        "workflow_ids": legacy_ids,
                    },
                },
            )

    active_workflows = (
        session.execute(
            select(WorkflowExecution).where(
                WorkflowExecution.status.in_(_VALIDATED_WORKFLOW_STATUSES)
            )
        )
        .scalars()
        .all()
    )
    for workflow in active_workflows:
        definition = get_workflow_type(
            session, workflow_type_key=workflow.workflow_type_key
        )
        valid_step_keys = {step.key for step in definition.steps}
        operation_types = (
            session.execute(
                select(WorkflowOperation.operation_type).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id
                )
            )
            .scalars()
            .all()
        )
        unknown = sorted(
            {
                str(operation_type or "").strip()
                for operation_type in operation_types
                if str(operation_type or "").strip()
                and str(operation_type or "").strip() not in valid_step_keys
            }
        )
        if unknown:
            raise RuntimeError(
                f"Workflow {workflow.workflow_id} references operation types not registered in code: {', '.join(unknown)}"
            )
