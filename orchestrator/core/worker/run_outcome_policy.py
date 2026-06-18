from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

from orchestrator.core.deployment_previews import create_run_preview_deployment, destroy_project_deployment_preview_release
from orchestrator.core.qa.demo_proof_start import advance_demo_proof_workflow_event, start_demo_proof_workflow
from orchestrator.core.qa.demo_service import (
    execute_qa_demo_stage,
    mark_pull_request_ready_after_demo_proof,
    next_required_qa_demo_worker_capability,
    qa_demo_recording_enabled,
    remaining_capture_targets,
    required_capture_targets,
    required_recording_counts_by_target,
    update_pull_request_with_demo_failure_evidence,
    update_pull_request_with_demo_evidence,
)
from orchestrator.core.runs.service import RUN_STATUS_WAITING_FOR_INPUT
from orchestrator.core.worker.finalization import CompletionTailExecutor, WorkflowFinalizer
from orchestrator.core.workflow.execution_artifacts import latest_pushed_execution_artifact_for_run
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import QaResult, WorkflowStageCheckpoint
from orchestrator.core.worker.capabilities import worker_label_for_capability
from orchestrator.storage.models import ProjectDeploymentRelease

_QA_DEMO_PREVIEW_PENDING_STATUSES = frozenset({"queued", "provisioning", "deploying", "route_activating"})
_QA_DEMO_PREVIEW_TERMINAL_FAILURE_STATUSES = frozenset({"failed", "rolled_back", "destroyed"})
_QA_DEMO_SUCCESS_PROOF_EVENTS_BEFORE_PR = (
    "ProofLeaseAcquired",
    "ReleaseRequested",
    "ReleaseProvisioning",
    "ReleaseLive",
    "RouteReady",
    "ServiceVerificationPassed",
    "RecordingStarted",
    "RecordingCompleted",
    "EvidenceUploadStarted",
    "EvidenceUploaded",
)
_QA_DEMO_FAILURE_PROOF_EVENTS_BEFORE_PR = (
    "ProofLeaseAcquired",
    "ReleaseRequested",
    "ReleaseProvisioning",
    "ReleaseLive",
    "RouteReady",
    "ServiceVerificationPassed",
    "RecordingStarted",
    "RecordingFailureEvidenceCaptured",
    "FailureEvidenceUploadStarted",
    "FailureEvidenceUploaded",
)


class RunOutcomePolicy:
    def __init__(
        self,
        *,
        session,
        settings,
        deps,
        cleanup_run_workspaces_safe_fn,
    ) -> None:  # noqa: ANN001
        self._session = session
        self._settings = settings
        self._deps = deps
        self._cleanup_run_workspaces_safe_fn = cleanup_run_workspaces_safe_fn

    def complete(
        self,
        *,
        prepared,
        workflow_result,
        execution_context,
    ):
        run = prepared.run
        project = prepared.project
        self._session.refresh(run)
        if (
            str(run.worker_service_instance_id or "").strip()
            != str(prepared.worker_service_instance_id or "").strip()
            or str(getattr(run, "claim_id", "") or "").strip() != prepared.claim_id
            or run.status not in {self._deps.statuses.running, self._deps.statuses.cancelled}
        ):
            self._deps.identity.logger.warning(
                "worker_run_ownership_lost run_id=%s tenant_id=%s issue_key=%s status=%s current_owner=%s expected_owner=%s",
                run.run_id,
                run.tenant_id,
                run.issue_key,
                run.status,
                run.worker_service_instance_id,
                prepared.worker_service_instance_id,
            )
            return run
        if run.status == self._deps.statuses.cancelled:
            self._cleanup_run_workspaces_safe_fn(
                cleanup_run_workspaces_fn=self._deps.identity.cleanup_run_workspaces_fn,
                logger=self._deps.identity.logger,
                base_dir=self._settings.project_repo_checkout_base_dir,
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
            )
            return self._deps.execution.finalize_cancelled_run_fn(
                self._session,
                run=run,
                stage_updates=prepared.notifier.stage_updates,
                expected_worker_service_instance_id=prepared.worker_service_instance_id,
                expected_claim_id=prepared.claim_id,
            )
        if workflow_result.plan is not None and self._deps.stage_updates.plan_posted_update_fn is not None:
            prepared.notifier.append(
                self._deps.stage_updates.plan_posted_update_fn(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    jira_url=prepared.jira_issue_url,
                    run_url=prepared.run_dashboard_url,
                )
            )
            self._deps.identity.emit_agent_event_fn(
                event_type="PLAN_POSTED",
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
                issue_key=run.issue_key,
                agent_id=prepared.agent_id,
            )
        stale_snapshot_result = self._handle_stale_snapshot(
            prepared=prepared,
            workflow_result=workflow_result,
            execution_context=execution_context,
        )
        if stale_snapshot_result is not None:
            return stale_snapshot_result

        if workflow_result.pr_url and self._deps.stage_updates.pr_opened_update_fn is not None:
            prepared.notifier.append(
                self._deps.stage_updates.pr_opened_update_fn(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    jira_url=prepared.jira_issue_url,
                    run_url=prepared.run_dashboard_url,
                    pr_url=workflow_result.pr_url,
                )
            )
            self._deps.identity.emit_agent_event_fn(
                event_type="PR_OPENED",
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
                issue_key=run.issue_key,
                agent_id=prepared.agent_id,
        )
        demo_recording_required = qa_demo_recording_enabled(getattr(prepared, "effective_policy", None))
        qa_plan = _workflow_plan_for_qa(workflow_result=workflow_result, persisted_plan=getattr(run, "plan", None))
        if workflow_result.outcome == "success":
            preview_release = None
            if demo_recording_required and not str(workflow_result.pr_url or "").strip():
                workflow_result = _workflow_result_with_qa_blocker(
                    workflow_result=workflow_result,
                    attempt=max(1, int(workflow_result.attempts or 1)),
                    message="QA demo recording requires a PR URL before creating the run preview release.",
                )
            if demo_recording_required and workflow_result.outcome == "success" and qa_plan is None:
                workflow_result = _workflow_result_with_qa_blocker(
                    workflow_result=workflow_result,
                    attempt=max(1, int(workflow_result.attempts or 1)),
                    message=(
                        "QA demo recording requires persisted PM demo requirements before creating the run "
                        "preview release."
                    ),
                )
            if (
                demo_recording_required
                and workflow_result.outcome == "success"
                and not _qa_demo_project_capture_targets(getattr(prepared, "workflow_request", None))
            ):
                workflow_result = _workflow_result_with_qa_blocker(
                    workflow_result=workflow_result,
                    attempt=max(1, int(workflow_result.attempts or 1)),
                    message=(
                        "QA demo recording requires project demo capture targets derived from project app metadata "
                        "before creating the run preview release."
                    ),
                )
            missing_plan_targets = (
                _qa_demo_missing_project_capture_targets(
                    qa_plan=qa_plan,
                    workflow_request=getattr(prepared, "workflow_request", None),
                )
                if demo_recording_required and workflow_result.outcome == "success" and qa_plan is not None
                else ()
            )
            if missing_plan_targets:
                workflow_result = _workflow_result_with_qa_blocker(
                    workflow_result=workflow_result,
                    attempt=max(1, int(workflow_result.attempts or 1)),
                    message=(
                        "QA demo recording PM requirements are missing required project demo capture target(s) "
                        "before creating the run preview release: "
                        + ", ".join(missing_plan_targets)
                    ),
                )
            try:
                waiting_preview_release = None
                qa_demo_proof_started = False
                qa_demo_proof_start_context = None
                if demo_recording_required and workflow_result.outcome == "success" and qa_plan is not None:
                    qa_demo_proof_start_context = _qa_demo_pre_release_proof_context(
                        prepared=prepared,
                        plan=qa_plan,
                        workflow_result=workflow_result,
                    )

                    def _start_qa_demo_proof_before_release(*, proof_scope_id: str, commit_sha: str) -> None:
                        nonlocal qa_demo_proof_started, qa_demo_proof_start_context
                        proof_context_payload = dict(vars(qa_demo_proof_start_context))
                        proof_context_payload["proof_scope_id"] = proof_scope_id
                        proof_context_payload["commit_sha"] = commit_sha
                        qa_demo_proof_start_context = SimpleNamespace(**proof_context_payload)
                        if qa_demo_proof_started:
                            return
                        self._start_qa_demo_proof_workflow(
                            proof_context=qa_demo_proof_start_context,
                            trigger_event="run_success_before_preview_release",
                        )
                        qa_demo_proof_started = True

                if demo_recording_required and workflow_result.outcome == "success":
                    waiting_preview_release = _qa_demo_waiting_preview_release(
                        session=self._session,
                        tenant_id=run.tenant_id,
                        project_id=project.project_id,
                        run_id=run.run_id,
                        execution_context=execution_context,
                        persisted_plan=getattr(run, "plan", None),
                    )
                    if waiting_preview_release is not None:
                        preview_release = waiting_preview_release
                if (
                    workflow_result.outcome == "success"
                    and (not demo_recording_required or qa_plan is not None)
                    and preview_release is None
                ):
                    preview_result = create_run_preview_deployment(
                        session=self._session,
                        tenant=prepared.tenant,
                        project=project,
                        run=run,
                        settings=self._settings,
                        pr_url=workflow_result.pr_url,
                        demo_proof_lease_required=demo_recording_required,
                        demo_proof_lease_acquired_fn=(
                            _start_qa_demo_proof_before_release
                            if demo_recording_required and qa_demo_proof_start_context is not None
                            else None
                        ),
                        force=_qa_demo_preview_force_requested(
                            workflow_request=getattr(prepared, "workflow_request", None),
                            demo_recording_required=demo_recording_required,
                            execution_context=execution_context,
                            persisted_plan=getattr(run, "plan", None),
                        ),
                    )
                    preview_release = preview_result.release
                    if demo_recording_required and preview_release is None:
                        workflow_result = _workflow_result_with_qa_blocker(
                            workflow_result=workflow_result,
                            attempt=max(1, int(workflow_result.attempts or 1)),
                            message=(
                                "QA demo recording requires a run preview deployment/release before recording; "
                                f"preview deployment was not created: {preview_result.reason}."
                            ),
                        )
                    if preview_result.created and preview_result.release is not None:
                        self._deps.identity.logger.info(
                            "worker_run_preview_deployment_created tenant_id=%s project_id=%s run_id=%s release_id=%s",
                            run.tenant_id,
                            project.project_id,
                            run.run_id,
                            preview_result.release.release_id,
                        )
            except Exception as exc:  # noqa: BLE001
                preview_error = f"Run preview deployment failed: {type(exc).__name__}: {exc}"
                workflow_result = (
                    _workflow_result_with_qa_blocker(
                        workflow_result=workflow_result,
                        attempt=max(1, int(workflow_result.attempts or 1)),
                        message=preview_error,
                    )
                    if demo_recording_required
                    else replace(
                        workflow_result,
                        outcome="failed",
                        blocker_message=preview_error,
                    )
                )
            else:
                if demo_recording_required and workflow_result.outcome == "success":
                    workflow_result = self._complete_required_qa_demo_stage(
                        prepared=prepared,
                        workflow_result=workflow_result,
                        preview_release=preview_release,
                        execution_context=execution_context,
                        proof_workflow_started=qa_demo_proof_started,
                    )
        capability_result = self._handle_capability_requeue(
            prepared=prepared,
            workflow_result=workflow_result,
            execution_context=execution_context,
        )
        if capability_result is not None:
            return capability_result

        generic_requeue_result = self._handle_generic_requeue(
            prepared=prepared,
            workflow_result=workflow_result,
            execution_context=execution_context,
        )
        if generic_requeue_result is not None:
            return generic_requeue_result

        if workflow_result.outcome == "waiting_for_input":
            self._session.refresh(run)
            if str(getattr(run, "status", "") or "").strip().lower() == RUN_STATUS_WAITING_FOR_INPUT:
                return run
            workflow_result = replace(
                workflow_result,
                outcome="blocked",
                blocker_message=(
                    workflow_result.blocker_message
                    or "Workflow requested human input, but no pending human-input request was created."
                ),
            )
        if workflow_result.outcome in {"blocked", "failed"} and self._deps.stage_updates.run_failed_update_fn is not None:
            error_text = (
                workflow_result.blocker_message
                or (workflow_result.diagnostics.message if workflow_result.diagnostics is not None else None)
                or "Workflow did not complete successfully"
            )
            prepared.notifier.append(
                self._deps.stage_updates.run_failed_update_fn(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    jira_url=prepared.jira_issue_url,
                    run_url=prepared.run_dashboard_url,
                    error=error_text,
                )
            )
        finalization = WorkflowFinalizer(
            session=self._session,
            logger=self._deps.identity.logger,
            finalize_workflow_result_fn=self._deps.execution.finalize_workflow_result_fn,
            run_status_failed=self._deps.statuses.failed,
            project_id=project.project_id,
            agent_id=prepared.agent_id,
        ).finalize(
            run=run,
            workflow_result=workflow_result,
            stage_updates=prepared.notifier.stage_updates,
            execution_context=execution_context,
            expected_worker_service_instance_id=prepared.worker_service_instance_id,
            expected_claim_id=prepared.claim_id,
        )
        self._deps.identity.logger.info(
            "worker_run_finalized run_id=%s tenant_id=%s issue_key=%s outcome=%s final_status=%s",
            finalization.run.run_id,
            finalization.run.tenant_id,
            finalization.run.issue_key,
            workflow_result.outcome,
            finalization.run.status,
        )

        for event_type in finalization.event_types:
            self._deps.identity.emit_agent_event_fn(
                event_type=event_type,
                tenant_id=finalization.run.tenant_id,
                project_id=project.project_id,
                run_id=finalization.run.run_id,
                issue_key=finalization.run.issue_key,
                agent_id=prepared.agent_id,
            )

        CompletionTailExecutor(
            session=self._session,
            tenant=prepared.tenant,
            project=project,
            settings=self._settings,
            logger=self._deps.identity.logger,
            send_jira_message_fn=self._deps.stage_updates.send_jira_message_fn,
            cleanup_run_workspaces_fn=self._deps.identity.cleanup_run_workspaces_fn,
            base_dir=self._settings.project_repo_checkout_base_dir,
            jira_issue_url=prepared.jira_issue_url,
            agent_id=prepared.agent_id,
            workspace_key=prepared.worker_workspace_key,
        ).execute(finalization)
        return finalization.run

    def _complete_required_qa_demo_stage(
        self,
        *,
        prepared,
        workflow_result,
        preview_release,
        execution_context,
        proof_workflow_started: bool = False,
    ):
        snapshot = ExecutionSnapshot.require(getattr(prepared.run, "plan", None), allow_empty=True)
        plan = workflow_result.plan or snapshot.plan()
        dev_result = snapshot.dev_result()
        test_result = snapshot.test_result()
        review_result = snapshot.review_result()
        previous_qa_result = snapshot.qa_result()
        if plan is None or dev_result is None or test_result is None or review_result is None:
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=workflow_result.attempts,
                message="QA demo recording requires persisted PM, dev, test, and review stage artifacts.",
            )
        if preview_release is None:
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=workflow_result.attempts,
                message=(
                    "QA demo recording requires an available run preview deployment/release before recording."
                ),
            )
        attempt = max(1, int(workflow_result.attempts or 1))
        preview_release_status = _preview_release_status(preview_release)
        if preview_release_status in _QA_DEMO_PREVIEW_PENDING_STATUSES:
            message = (
                "QA demo recording is waiting for the run preview deployment/release to become live before "
                f"recording; current status is {preview_release_status}."
            )
            qa_result = QaResult(
                summary=[message],
                scenarios=list(previous_qa_result.scenarios if previous_qa_result is not None else []),
                recordings=list(previous_qa_result.recordings if previous_qa_result is not None else []),
                outcome="requeue",
                feedback=message,
            )
            self._persist_qa_stage_checkpoint(
                prepared=prepared,
                qa_result=qa_result,
                attempt=attempt,
                execution_context=execution_context,
            )
            return _workflow_result_with_generic_qa_requeue(
                workflow_result=workflow_result,
                attempt=attempt,
                message=message,
                wait_for_release_id=str(getattr(preview_release, "release_id", "") or "").strip() or None,
            )
        if preview_release_status in _QA_DEMO_PREVIEW_TERMINAL_FAILURE_STATUSES:
            message = (
                "QA demo recording requires a live run preview deployment/release before recording; "
                f"preview release is {preview_release_status}."
            )
            failure_detail = _preview_release_failure_detail(preview_release)
            if failure_detail:
                message = f"{message} Release failure: {failure_detail}"
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=attempt,
                message=message,
            )
        try:
            qa_result = execute_qa_demo_stage(
                session=self._session,
                settings=self._settings,
                tenant=prepared.tenant,
                project=prepared.project,
                run=prepared.run,
                request=prepared.workflow_request,
                plan=plan,
                dev_result=dev_result,
                test_result=test_result,
                review_result=review_result,
                preview_release=preview_release,
                previous_qa_result=previous_qa_result,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"QA demo recording failed: {type(exc).__name__}: {exc}"
            try:
                proof_context = _qa_demo_release_proof_context(
                    prepared=prepared,
                    preview_release=preview_release,
                    plan=plan,
                    workflow_result=workflow_result,
                )
                if not proof_workflow_started:
                    self._start_qa_demo_proof_workflow(
                        proof_context=proof_context,
                        trigger_event="run_failed_before_demo_recording_complete",
                    )
                self._advance_qa_demo_proof_events(
                    proof_context=proof_context,
                    events=(
                        "ProofLeaseAcquired",
                        "ReleaseRequested",
                        "ReleaseProvisioning",
                        "ReleaseLive",
                        "RouteReady",
                        "ServiceVerificationPassed",
                        "RecordingStarted",
                        "RecordingFailed",
                    ),
                )
            except Exception as proof_exc:  # noqa: BLE001
                message = (
                    f"{message} QA demo proof recording failure event failed: "
                    f"{type(proof_exc).__name__}: {proof_exc}"
                )
            qa_result = QaResult(
                summary=[message],
                scenarios=[],
                outcome="blocked",
                blocker_message=message,
            )
        if qa_result.outcome == "continue":
            missing_targets = remaining_capture_targets(plan, qa_result.recordings)
            if missing_targets:
                message = (
                    "QA demo recording completed without proof for required capture target(s): "
                    + ", ".join(missing_targets)
                )
                qa_result = replace(
                    qa_result,
                    outcome="blocked",
                    blocker_message=message,
                    summary=[*list(qa_result.summary or []), message],
                )
        self._persist_qa_stage_checkpoint(
            prepared=prepared,
            qa_result=qa_result,
            attempt=attempt,
            execution_context=execution_context,
        )
        if qa_result.outcome == "requeue":
            requeue_target = next_required_qa_demo_worker_capability(
                settings=self._settings,
                plan=plan,
                recordings=qa_result.recordings,
            )
            message = qa_result.feedback or qa_result.blocker_message or _summarize_qa_result(qa_result)
            if requeue_target is None:
                return _workflow_result_with_qa_blocker(
                    workflow_result=workflow_result,
                    attempt=attempt,
                    message=f"QA demo recording requested requeue but no remaining worker capability was resolvable. {message}",
                )
            return replace(
                workflow_result,
                outcome="requeue",
                requeue_target=requeue_target,
                requeue_reason=message,
                orchestration_stage_trace=[
                    *list(workflow_result.orchestration_stage_trace or []),
                    _stage_trace_entry(
                        stage="qa",
                        attempt=attempt,
                        status="requeue",
                        summary=message,
                    ),
                ],
            )
        if qa_result.outcome != "continue":
            proof_context = None
            proof_terminal_failure_emitted = False
            if qa_result.failure_evidence:
                try:
                    proof_context = _qa_demo_proof_context(
                        prepared=prepared,
                        qa_result=qa_result,
                        preview_release=preview_release,
                        plan=plan,
                        workflow_result=workflow_result,
                    )
                    self._start_qa_demo_proof_workflow(
                        proof_context=proof_context,
                        trigger_event="run_failed_with_demo_failure_evidence",
                    )
                    self._advance_qa_demo_proof_events(
                        proof_context=proof_context,
                        events=_QA_DEMO_FAILURE_PROOF_EVENTS_BEFORE_PR,
                    )
                    update_pull_request_with_demo_failure_evidence(
                        session=self._session,
                        settings=self._settings,
                        tenant=prepared.tenant,
                        project=prepared.project,
                        run=prepared.run,
                        workflow_result=workflow_result,
                        qa_result=qa_result,
                    )
                    self._advance_qa_demo_proof_events(
                        proof_context=proof_context,
                        events=("PRFailureEvidenceAttachStarted", "PRFailureEvidenceAttached"),
                    )
                except Exception as exc:  # noqa: BLE001
                    message = f"QA demo failure evidence PR update failed: {type(exc).__name__}: {exc}"
                    diagnostics = _qa_failure_evidence_diagnostics(qa_result)
                    if diagnostics:
                        message = f"{message} QA failure evidence diagnostics: {diagnostics}"
                    if proof_context is not None:
                        try:
                            self._advance_qa_demo_proof_events(
                                proof_context=proof_context,
                                events=("PRFailureEvidenceAttachStarted", "PRFailureEvidenceAttachFailed"),
                            )
                            proof_terminal_failure_emitted = True
                        except Exception as proof_exc:  # noqa: BLE001
                            message = (
                                f"{message} QA demo failure proof PR evidence failure event failed: "
                                f"{type(proof_exc).__name__}: {proof_exc}"
                            )
                    qa_result = replace(
                        qa_result,
                        blocker_message=(
                            (qa_result.blocker_message or qa_result.feedback or _summarize_qa_result(qa_result))
                            + f" {message}"
                        ),
                        summary=[*list(qa_result.summary or []), message],
                    )
                    self._persist_qa_stage_checkpoint(
                        prepared=prepared,
                        qa_result=qa_result,
                        attempt=attempt,
                        execution_context=execution_context,
                    )
            cleanup_error = self._cleanup_qa_demo_preview_release(
                prepared=prepared,
                preview_release=preview_release,
                reason="qa_demo_failed",
            )
            if cleanup_error:
                if proof_context is not None and not proof_terminal_failure_emitted:
                    try:
                        self._advance_qa_demo_proof_events(
                            proof_context=proof_context,
                            events=("FailurePreviewCleanupRequested", "FailurePreviewCleanupFailed"),
                        )
                    except Exception as exc:  # noqa: BLE001
                        cleanup_error = (
                            f"{cleanup_error} QA demo failure proof cleanup failure event failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                qa_result = replace(
                    qa_result,
                    blocker_message=(
                        (qa_result.blocker_message or qa_result.feedback or _summarize_qa_result(qa_result))
                        + f" {cleanup_error}"
                    ),
                    summary=[*list(qa_result.summary or []), cleanup_error],
                )
            elif proof_context is not None and not proof_terminal_failure_emitted:
                try:
                    self._advance_qa_demo_proof_events(
                        proof_context=proof_context,
                        events=("FailurePreviewCleanupRequested", "FailurePreviewCleanupCompleted"),
                    )
                except Exception as exc:  # noqa: BLE001
                    message = f"QA demo failure proof cleanup event failed: {type(exc).__name__}: {exc}"
                    qa_result = replace(
                        qa_result,
                        blocker_message=(
                            (qa_result.blocker_message or qa_result.feedback or _summarize_qa_result(qa_result))
                            + f" {message}"
                        ),
                        summary=[*list(qa_result.summary or []), message],
                    )
                    self._persist_qa_stage_checkpoint(
                        prepared=prepared,
                        qa_result=qa_result,
                        attempt=attempt,
                        execution_context=execution_context,
                    )
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=attempt,
                message=qa_result.blocker_message or qa_result.feedback or _summarize_qa_result(qa_result),
            )
        proof_context = _qa_demo_proof_context(
            prepared=prepared,
            qa_result=qa_result,
            preview_release=preview_release,
            plan=plan,
            workflow_result=workflow_result,
        )
        try:
            if not proof_workflow_started:
                self._start_qa_demo_proof_workflow(proof_context=proof_context)
            self._advance_qa_demo_proof_events(
                proof_context=proof_context,
                events=_QA_DEMO_SUCCESS_PROOF_EVENTS_BEFORE_PR,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"QA demo proof workflow advance failed before PR evidence update: {type(exc).__name__}: {exc}"
            blocked_qa_result = replace(
                qa_result,
                outcome="blocked",
                blocker_message=message,
                summary=[*list(qa_result.summary or []), message],
            )
            self._persist_qa_stage_checkpoint(
                prepared=prepared,
                qa_result=blocked_qa_result,
                attempt=attempt,
                execution_context=execution_context,
            )
            cleanup_error = self._cleanup_qa_demo_preview_release(
                prepared=prepared,
                preview_release=preview_release,
                reason="qa_demo_failed",
            )
            if cleanup_error:
                message = f"{message} {cleanup_error}"
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=attempt,
                message=message,
            )
        try:
            update_pull_request_with_demo_evidence(
                session=self._session,
                settings=self._settings,
                tenant=prepared.tenant,
                project=prepared.project,
                run=prepared.run,
                workflow_result=workflow_result,
                qa_result=qa_result,
                required_capture_targets=required_capture_targets(plan),
                required_recording_counts=required_recording_counts_by_target(plan),
            )
        except Exception as exc:  # noqa: BLE001
            message = f"QA demo evidence PR update failed: {type(exc).__name__}: {exc}"
            try:
                self._advance_qa_demo_proof_events(
                    proof_context=proof_context,
                    events=("PREvidenceAttachStarted", "PREvidenceAttachFailed"),
                )
            except Exception as proof_exc:  # noqa: BLE001
                message = (
                    f"{message} QA demo proof PR evidence failure event failed: "
                    f"{type(proof_exc).__name__}: {proof_exc}"
                )
            blocked_qa_result = replace(
                qa_result,
                outcome="blocked",
                blocker_message=message,
                summary=[*list(qa_result.summary or []), message],
            )
            self._persist_qa_stage_checkpoint(
                prepared=prepared,
                qa_result=blocked_qa_result,
                attempt=attempt,
                execution_context=execution_context,
            )
            cleanup_error = self._cleanup_qa_demo_preview_release(
                prepared=prepared,
                preview_release=preview_release,
                reason="qa_demo_failed",
            )
            if cleanup_error:
                message = f"{message} {cleanup_error}"
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=attempt,
                message=message,
            )
        try:
            self._advance_qa_demo_proof_events(
                proof_context=proof_context,
                events=("PREvidenceAttachStarted", "PREvidenceAttached"),
            )
        except Exception as exc:  # noqa: BLE001
            message = f"QA demo proof workflow PR evidence event failed: {type(exc).__name__}: {exc}"
            blocked_qa_result = replace(
                qa_result,
                outcome="blocked",
                blocker_message=message,
                summary=[*list(qa_result.summary or []), message],
            )
            self._persist_qa_stage_checkpoint(
                prepared=prepared,
                qa_result=blocked_qa_result,
                attempt=attempt,
                execution_context=execution_context,
            )
            cleanup_error = self._cleanup_qa_demo_preview_release(
                prepared=prepared,
                preview_release=preview_release,
                reason="qa_demo_failed",
            )
            if cleanup_error:
                message = f"{message} {cleanup_error}"
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=attempt,
                message=message,
            )
        cleanup_error = self._cleanup_qa_demo_preview_release(
            prepared=prepared,
            preview_release=preview_release,
            reason="qa_demo_complete",
        )
        if cleanup_error:
            try:
                self._advance_qa_demo_proof_events(
                    proof_context=proof_context,
                    events=("PreviewCleanupRequested", "PreviewCleanupFailed"),
                )
            except Exception as exc:  # noqa: BLE001
                cleanup_error = (
                    f"{cleanup_error} QA demo proof cleanup failure event failed: {type(exc).__name__}: {exc}"
                )
            blocked_qa_result = replace(
                qa_result,
                outcome="blocked",
                blocker_message=cleanup_error,
                summary=[*list(qa_result.summary or []), cleanup_error],
            )
            self._persist_qa_stage_checkpoint(
                prepared=prepared,
                qa_result=blocked_qa_result,
                attempt=attempt,
                execution_context=execution_context,
            )
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=attempt,
                message=cleanup_error,
            )
        try:
            self._advance_qa_demo_proof_events(
                proof_context=proof_context,
                events=("PreviewCleanupRequested", "PreviewCleanupCompleted"),
            )
            self._mark_pull_request_ready_after_demo_proof(
                prepared=prepared,
                workflow_result=workflow_result,
            )
        except Exception as exc:  # noqa: BLE001
            message = f"QA demo proof completion failed: {type(exc).__name__}: {exc}"
            blocked_qa_result = replace(
                qa_result,
                outcome="blocked",
                blocker_message=message,
                summary=[*list(qa_result.summary or []), message],
            )
            self._persist_qa_stage_checkpoint(
                prepared=prepared,
                qa_result=blocked_qa_result,
                attempt=attempt,
                execution_context=execution_context,
            )
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=attempt,
                message=message,
            )
        return replace(
            workflow_result,
            orchestration_stage_trace=[
                *list(workflow_result.orchestration_stage_trace or []),
                _stage_trace_entry(
                    stage="qa",
                    attempt=attempt,
                    status="completed",
                    summary=_summarize_qa_result(qa_result),
                ),
            ],
            )

    def _mark_pull_request_ready_after_demo_proof(self, *, prepared, workflow_result) -> None:  # noqa: ANN001
        ready_fn = getattr(
            self._deps.execution,
            "mark_pull_request_ready_after_demo_proof_fn",
            mark_pull_request_ready_after_demo_proof,
        )
        ready_fn(
            session=self._session,
            settings=self._settings,
            tenant=prepared.tenant,
            project=prepared.project,
            workflow_result=workflow_result,
        )

    def _start_qa_demo_proof_workflow(
        self,
        *,
        proof_context,  # noqa: ANN001
        trigger_event: str = "run_success_before_ready_for_review",
    ) -> None:
        start_fn = getattr(self._deps.execution, "start_demo_proof_workflow_fn", start_demo_proof_workflow)
        start_fn(
            session=self._session,
            settings=self._settings,
            tenant=proof_context.tenant,
            project=proof_context.project,
            proof_scope_id=proof_context.proof_scope_id,
            commit_sha=proof_context.commit_sha,
            trigger_mode=proof_context.trigger_mode,
            trigger_event=trigger_event,
            run_id=proof_context.run_id,
            pr_url=proof_context.pr_url,
            required_capture_targets=list(proof_context.required_capture_targets),
        )

    def _advance_qa_demo_proof_events(self, *, proof_context, events: tuple[str, ...]) -> None:  # noqa: ANN001
        advance_fn = getattr(
            self._deps.execution,
            "advance_demo_proof_workflow_event_fn",
            advance_demo_proof_workflow_event,
        )
        for event in events:
            event_metadata = _qa_demo_proof_event_metadata(proof_context=proof_context, event=event)
            advance_fn(
                session=self._session,
                settings=self._settings,
                tenant=proof_context.tenant,
                project=proof_context.project,
                proof_scope_id=proof_context.proof_scope_id,
                commit_sha=proof_context.commit_sha,
                trigger_mode=proof_context.trigger_mode,
                event=event,
                run_id=proof_context.run_id,
                pr_url=proof_context.pr_url,
                required_capture_targets=list(proof_context.required_capture_targets),
                event_metadata=event_metadata,
            )

    def _persist_qa_stage_checkpoint(
        self,
        *,
        prepared,
        qa_result: QaResult,
        attempt: int,
        execution_context,
    ) -> None:
        self._deps.execution.persist_stage_checkpoint_fn(
            self._session,
            run=prepared.run,
            checkpoint=WorkflowStageCheckpoint(
                stage="qa",
                attempt=attempt,
                status=_checkpoint_status_for_stage_outcome(qa_result.outcome),
                summary=_summarize_qa_result(qa_result),
                qa_result=qa_result,
            ),
            execution_context=execution_context,
            expected_worker_service_instance_id=prepared.worker_service_instance_id,
            expected_claim_id=prepared.claim_id,
        )

    def _cleanup_qa_demo_preview_release(
        self,
        *,
        prepared,
        preview_release,
        reason: str,
    ) -> str | None:
        release_id = str(getattr(preview_release, "release_id", "") or "").strip()
        if not release_id:
            return None
        if str(getattr(preview_release, "release_kind", "run_preview") or "").strip() != "run_preview":
            return None
        if str(getattr(preview_release, "status", "") or "").strip() == "destroyed":
            return None
        try:
            destroy_project_deployment_preview_release(
                session=self._session,
                tenant_id=prepared.run.tenant_id,
                project_id=prepared.project.project_id,
                release_id=release_id,
                reason=reason,
            )
        except Exception as exc:  # noqa: BLE001
            message = (
                "QA demo preview cleanup failed; run preview release was not proven destroyed: "
                f"{release_id}: {type(exc).__name__}: {exc}"
            )
            self._deps.identity.logger.exception(
                "qa_demo_preview_cleanup_failed tenant_id=%s project_id=%s run_id=%s release_id=%s reason=%s",
                prepared.run.tenant_id,
                prepared.project.project_id,
                prepared.run.run_id,
                release_id,
                reason,
            )
            return message
        self._deps.identity.logger.info(
            "qa_demo_preview_cleanup_completed tenant_id=%s project_id=%s run_id=%s release_id=%s reason=%s",
            prepared.run.tenant_id,
            prepared.project.project_id,
            prepared.run.run_id,
            release_id,
            reason,
        )
        return None

    def _handle_stale_snapshot(self, *, prepared, workflow_result, execution_context):
        if not (
            workflow_result.outcome == "success"
            and prepared.workflow_request.start_point_ref
            and prepared.workflow_request.start_point_sha
        ):
            return None
        freshness = self._deps.execution.check_run_snapshot_freshness_fn(
            base_dir=self._settings.project_repo_checkout_base_dir,
            tenant_id=prepared.tenant.tenant_id,
            project=prepared.project,
            start_point_ref=prepared.workflow_request.start_point_ref,
            start_point_sha=prepared.workflow_request.start_point_sha,
        )
        if self._stale_snapshot_was_self_published(prepared=prepared, freshness=freshness):
            return None
        if not freshness.stale:
            return None
        error_text = freshness.message or "Branch snapshot stale; requeueing from latest snapshot."
        self._deps.identity.logger.info(
            "worker_requeue_stale_snapshot run_id=%s tenant_id=%s issue_key=%s error=%s",
            prepared.run.run_id,
            prepared.run.tenant_id,
            prepared.run.issue_key,
            error_text,
        )
        if self._deps.stage_updates.run_requeued_stale_snapshot_update_fn is not None:
            prepared.notifier.append(
                self._deps.stage_updates.run_requeued_stale_snapshot_update_fn(
                    tenant_id=prepared.run.tenant_id,
                    issue_key=prepared.run.issue_key,
                    run_id=prepared.run.run_id,
                    jira_url=prepared.jira_issue_url,
                    run_url=prepared.run_dashboard_url,
                    error=error_text,
                )
            )
        self._cleanup_run_workspaces_safe_fn(
            cleanup_run_workspaces_fn=self._deps.identity.cleanup_run_workspaces_fn,
            logger=self._deps.identity.logger,
            base_dir=self._settings.project_repo_checkout_base_dir,
            tenant_id=prepared.run.tenant_id,
            project_id=prepared.project.project_id,
            run_id=prepared.run.run_id,
        )
        return self._deps.execution.requeue_workflow_result_for_stale_snapshot_fn(
            self._session,
            run=prepared.run,
            workflow_result=workflow_result,
            stage_updates=prepared.notifier.stage_updates,
            error=error_text,
            execution_context=execution_context,
            expected_worker_service_instance_id=prepared.worker_service_instance_id,
            expected_claim_id=prepared.claim_id,
        )

    def _stale_snapshot_was_self_published(self, *, prepared, freshness) -> bool:  # noqa: ANN001
        if not bool(getattr(freshness, "stale", False)):
            return False
        current_sha = str(getattr(freshness, "current_start_point_sha", "") or "").strip()
        if not current_sha:
            return False
        artifact = latest_pushed_execution_artifact_for_run(
            session=self._session,
            run_id=getattr(prepared.run, "run_id", None),
        )
        if artifact is None:
            return False
        artifact_commit_sha = str(getattr(artifact, "commit_sha", "") or "").strip()
        if not artifact_commit_sha or artifact_commit_sha != current_sha:
            return False
        self._deps.identity.logger.info(
            "worker_stale_snapshot_self_publication_ignored run_id=%s tenant_id=%s issue_key=%s start_ref=%s artifact_commit_sha=%s",
            prepared.run.run_id,
            prepared.run.tenant_id,
            prepared.run.issue_key,
            prepared.workflow_request.start_point_ref,
            artifact_commit_sha,
        )
        return True

    def _handle_capability_requeue(self, *, prepared, workflow_result, execution_context):
        if workflow_result.outcome != "requeue" or workflow_result.requeue_target is None:
            return None
        required_worker_label = worker_label_for_capability(workflow_result.requeue_target)
        error_text = workflow_result.requeue_reason or "Execution capability mismatch"
        self._deps.identity.logger.info(
            "worker_requeue_capability run_id=%s tenant_id=%s issue_key=%s required_worker_label=%s error=%s",
            prepared.run.run_id,
            prepared.run.tenant_id,
            prepared.run.issue_key,
            required_worker_label,
            error_text,
        )
        if self._deps.stage_updates.run_requeued_capability_update_fn is not None:
            prepared.notifier.append(
                self._deps.stage_updates.run_requeued_capability_update_fn(
                    tenant_id=prepared.run.tenant_id,
                    issue_key=prepared.run.issue_key,
                    run_id=prepared.run.run_id,
                    jira_url=prepared.jira_issue_url,
                    run_url=prepared.run_dashboard_url,
                    required_worker_label=required_worker_label,
                    error=error_text,
                )
            )
        self._cleanup_run_workspaces_safe_fn(
            cleanup_run_workspaces_fn=self._deps.identity.cleanup_run_workspaces_fn,
            logger=self._deps.identity.logger,
            base_dir=self._settings.project_repo_checkout_base_dir,
            tenant_id=prepared.run.tenant_id,
            project_id=prepared.project.project_id,
            run_id=prepared.run.run_id,
        )
        return self._deps.execution.requeue_workflow_result_for_capability_fn(
            self._session,
            run=prepared.run,
            workflow_result=workflow_result,
            stage_updates=prepared.notifier.stage_updates,
            required_worker_capability=workflow_result.requeue_target.value,
            required_worker_label=required_worker_label,
            execution_context=execution_context,
            expected_worker_service_instance_id=prepared.worker_service_instance_id,
            expected_claim_id=prepared.claim_id,
        )

    def _handle_generic_requeue(self, *, prepared, workflow_result, execution_context):
        if workflow_result.outcome != "requeue":
            return None
        error_text = (
            workflow_result.requeue_reason
            or workflow_result.blocker_message
            or (workflow_result.diagnostics.message if workflow_result.diagnostics is not None else None)
            or "Workflow requested requeue."
        )
        self._deps.identity.logger.info(
            "worker_requeue_generic run_id=%s tenant_id=%s issue_key=%s error=%s",
            prepared.run.run_id,
            prepared.run.tenant_id,
            prepared.run.issue_key,
            error_text,
        )
        self._cleanup_run_workspaces_safe_fn(
            cleanup_run_workspaces_fn=self._deps.identity.cleanup_run_workspaces_fn,
            logger=self._deps.identity.logger,
            base_dir=self._settings.project_repo_checkout_base_dir,
            tenant_id=prepared.run.tenant_id,
            project_id=prepared.project.project_id,
            run_id=prepared.run.run_id,
        )
        return self._deps.execution.requeue_workflow_result_for_stale_snapshot_fn(
            self._session,
            run=prepared.run,
            workflow_result=workflow_result,
            stage_updates=prepared.notifier.stage_updates,
            error=error_text,
            execution_context=execution_context,
            expected_worker_service_instance_id=prepared.worker_service_instance_id,
            expected_claim_id=prepared.claim_id,
            mark_stale_snapshot=False,
        )


def _stage_trace_entry(*, stage: str, attempt: int, status: str, summary: str) -> dict[str, object]:
    return {
        "stage": stage,
        "status": status,
        "attempt": attempt,
        "summary": summary,
    }


def _preview_release_failure_detail(preview_release) -> str:  # noqa: ANN001
    raw_error = str(getattr(preview_release, "last_error", "") or "").strip()
    if not raw_error:
        return ""
    text = raw_error
    try:
        parsed = json.loads(raw_error)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, list):
        visible_outputs = [
            str(item.get("output") or "").strip()
            for item in parsed
            if isinstance(item, dict) and not bool(item.get("hidden")) and str(item.get("output") or "").strip()
        ]
        if visible_outputs:
            text = "\n".join(visible_outputs)
    for line in text.splitlines():
        normalized = line.strip()
        lowered = normalized.lower()
        if not normalized:
            continue
        if "deployment failed" in lowered or "error:" in lowered or "failed to " in lowered:
            return normalized[:500]
    return text.strip().replace("\n", " ")[:500]


def _checkpoint_status_for_stage_outcome(outcome: str) -> str:
    normalized = str(outcome or "").strip().lower()
    if normalized == "continue":
        return "completed"
    if normalized == "requeue":
        return "requeue"
    if normalized == "waiting_for_input":
        return "waiting_for_input"
    if normalized == "failed":
        return "failed"
    return "blocked"


def _summarize_qa_result(result: QaResult) -> str:
    if result.blocker_message:
        return result.blocker_message
    if result.feedback:
        return result.feedback
    if result.recordings:
        return f"QA recorded {len(result.recordings)} demos across {max(1, len(result.scenarios))} walkthroughs."
    if result.summary:
        return "; ".join(result.summary[:2])
    return "QA demo recording completed."


def _qa_failure_evidence_diagnostics(result: QaResult) -> str:
    diagnostics: list[str] = []
    seen: set[str] = set()
    for item in list(result.failure_evidence or []):
        error_message = str(getattr(item, "error_message", "") or "").strip()
        if not error_message:
            continue
        name = str(getattr(item, "name", "") or "").strip()
        capture_target = str(getattr(item, "capture_target", "") or "").strip()
        label_parts = [part for part in (capture_target, name) if part]
        diagnostic = f"{' '.join(label_parts)}: {error_message}" if label_parts else error_message
        if diagnostic in seen:
            continue
        seen.add(diagnostic)
        diagnostics.append(diagnostic)
    return " | ".join(diagnostics)


def _qa_demo_proof_context(*, prepared, qa_result: QaResult, preview_release, plan, workflow_result):  # noqa: ANN001
    commit_sha = _qa_demo_proof_commit_sha(qa_result)
    run_id = str(getattr(prepared.run, "run_id", "") or "").strip()
    if not run_id:
        raise RuntimeError("QA demo proof workflow requires run_id")
    pr_url = str(getattr(workflow_result, "pr_url", "") or getattr(prepared.run, "pr_url", "") or "").strip()
    if not pr_url:
        raise RuntimeError("QA demo proof workflow requires PR URL")
    return SimpleNamespace(
        tenant=prepared.tenant,
        project=prepared.project,
        run_id=run_id,
        pr_url=pr_url,
        commit_sha=commit_sha,
        trigger_mode="from_run",
        proof_scope_id=f"run:{run_id}:{commit_sha}",
        required_capture_targets=tuple(required_capture_targets(plan)),
        preview_release=preview_release,
        qa_result=qa_result,
    )


def _qa_demo_pre_release_proof_context(*, prepared, plan, workflow_result):  # noqa: ANN001
    run_id = str(getattr(prepared.run, "run_id", "") or "").strip()
    if not run_id:
        raise RuntimeError("QA demo proof workflow requires run_id")
    pr_url = str(getattr(workflow_result, "pr_url", "") or getattr(prepared.run, "pr_url", "") or "").strip()
    if not pr_url:
        raise RuntimeError("QA demo proof workflow requires PR URL")
    return SimpleNamespace(
        tenant=prepared.tenant,
        project=prepared.project,
        run_id=run_id,
        pr_url=pr_url,
        commit_sha="pending-release-commit",
        trigger_mode="from_run",
        proof_scope_id=f"run:{run_id}:pending-release-commit",
        required_capture_targets=tuple(required_capture_targets(plan)),
        preview_release=None,
        qa_result=None,
    )


def _qa_demo_release_proof_context(*, prepared, preview_release, plan, workflow_result):  # noqa: ANN001
    commit_sha = str(getattr(preview_release, "commit_sha", "") or "").strip().lower()
    if not commit_sha:
        raise RuntimeError("QA demo proof workflow requires preview release commit SHA")
    run_id = str(getattr(prepared.run, "run_id", "") or "").strip()
    if not run_id:
        raise RuntimeError("QA demo proof workflow requires run_id")
    pr_url = str(getattr(workflow_result, "pr_url", "") or getattr(prepared.run, "pr_url", "") or "").strip()
    if not pr_url:
        raise RuntimeError("QA demo proof workflow requires PR URL")
    return SimpleNamespace(
        tenant=prepared.tenant,
        project=prepared.project,
        run_id=run_id,
        pr_url=pr_url,
        commit_sha=commit_sha,
        trigger_mode="from_run",
        proof_scope_id=f"run:{run_id}:{commit_sha}",
        required_capture_targets=tuple(required_capture_targets(plan)),
        preview_release=preview_release,
        qa_result=None,
    )


def _qa_demo_proof_event_metadata(*, proof_context, event: str) -> dict[str, object] | None:  # noqa: ANN001
    metadata: dict[str, object] = {}
    preview_release = getattr(proof_context, "preview_release", None)
    if event in {
        "ProofLeaseAcquired",
        "ReleaseRequested",
        "ReleaseProvisioning",
        "ReleaseLive",
        "RouteReady",
        "ServiceVerificationPassed",
        "RecordingStarted",
        "RecordingFailed",
        "PreviewCleanupRequested",
        "PreviewCleanupCompleted",
        "PreviewCleanupFailed",
        "FailurePreviewCleanupRequested",
        "FailurePreviewCleanupCompleted",
        "FailurePreviewCleanupFailed",
    } and preview_release is not None:
        metadata.update(
            {
                "release_id": str(getattr(preview_release, "release_id", "") or "").strip(),
                "release_kind": str(getattr(preview_release, "release_kind", "") or "").strip(),
                "release_status": str(getattr(preview_release, "status", "") or "").strip(),
                "release_commit_sha": str(getattr(preview_release, "commit_sha", "") or "").strip(),
            }
        )
        if event in {"PreviewCleanupCompleted", "FailurePreviewCleanupCompleted"}:
            metadata["cleanup_status"] = "completed"
            metadata["cleanup_mode"] = "destroy_or_ttl"
    qa_result = getattr(proof_context, "qa_result", None)
    if event in {"RecordingCompleted", "EvidenceUploadStarted", "EvidenceUploaded"} and qa_result is not None:
        recordings = list(getattr(qa_result, "recordings", ()) or ())
        metadata.update(
            {
                "recording_count": len(recordings),
                "artifact_urls": [
                    str(getattr(recording, "artifact_url", "") or "").strip()
                    for recording in recordings
                    if str(getattr(recording, "artifact_url", "") or "").strip()
                ],
                "capture_targets": [
                    str(getattr(recording, "capture_target", "") or "").strip()
                    for recording in recordings
                    if str(getattr(recording, "capture_target", "") or "").strip()
                ],
                "recordings": [
                    {
                        "capture_target": str(getattr(recording, "capture_target", "") or "").strip(),
                        "artifact_url": str(getattr(recording, "artifact_url", "") or "").strip(),
                        "object_key": str(getattr(recording, "object_key", "") or "").strip(),
                        "capture_reference": str(getattr(recording, "capture_reference", "") or "").strip(),
                        "content_sha256": str(getattr(recording, "content_sha256", "") or "").strip(),
                        "release_commit_sha": str(getattr(recording, "release_commit_sha", "") or "").strip(),
                        "release_context_sha256": str(
                            getattr(recording, "release_context_sha256", "") or ""
                        ).strip(),
                    }
                    for recording in recordings
                    if str(getattr(recording, "capture_target", "") or "").strip()
                    and str(getattr(recording, "artifact_url", "") or "").strip()
                    and str(getattr(recording, "object_key", "") or "").strip()
                    and str(getattr(recording, "capture_reference", "") or "").strip()
                    and str(getattr(recording, "content_sha256", "") or "").strip()
                    and str(getattr(recording, "release_commit_sha", "") or "").strip()
                    and str(getattr(recording, "release_context_sha256", "") or "").strip()
                ],
            }
        )
    if event in {
        "RecordingFailureEvidenceCaptured",
        "FailureEvidenceUploadStarted",
        "FailureEvidenceUploaded",
    } and qa_result is not None:
        failure_evidence = list(getattr(qa_result, "failure_evidence", ()) or ())
        metadata.update(
            {
                "failure_evidence_count": len(failure_evidence),
                "artifact_urls": [
                    str(getattr(item, "artifact_url", "") or "").strip()
                    for item in failure_evidence
                    if str(getattr(item, "artifact_url", "") or "").strip()
                ],
                "capture_targets": [
                    str(getattr(item, "capture_target", "") or "").strip()
                    for item in failure_evidence
                    if str(getattr(item, "capture_target", "") or "").strip()
                ],
                "failure_evidence": [
                    {
                        "capture_target": str(getattr(item, "capture_target", "") or "").strip(),
                        "artifact_url": str(getattr(item, "artifact_url", "") or "").strip(),
                        "error_message": str(getattr(item, "error_message", "") or "").strip(),
                    }
                    for item in failure_evidence
                    if str(getattr(item, "capture_target", "") or "").strip()
                    and str(getattr(item, "artifact_url", "") or "").strip()
                    and str(getattr(item, "error_message", "") or "").strip()
                ],
            }
        )
    if event in {
        "PREvidenceAttachStarted",
        "PREvidenceAttached",
        "PREvidenceAttachFailed",
        "PRFailureEvidenceAttachStarted",
        "PRFailureEvidenceAttached",
        "PRFailureEvidenceAttachFailed",
    }:
        metadata["pr_url"] = str(getattr(proof_context, "pr_url", "") or "").strip()
    compacted: dict[str, object] = {}
    for key, value in metadata.items():
        if value == "" or value == [] or value == {}:
            continue
        compacted[key] = value
    return compacted or None


def _qa_demo_proof_commit_sha(qa_result: QaResult) -> str:
    for item in [*list(qa_result.recordings or []), *list(qa_result.failure_evidence or [])]:
        commit_sha = str(getattr(item, "release_commit_sha", "") or "").strip().lower()
        if commit_sha:
            return commit_sha
    raise RuntimeError("QA demo proof workflow requires release commit SHA from recorded evidence")


def _workflow_plan_for_qa(*, workflow_result, persisted_plan):
    if workflow_result.plan is not None:
        return workflow_result.plan
    snapshot = ExecutionSnapshot.load(persisted_plan)
    if snapshot is None:
        return None
    return snapshot.plan()


def _qa_demo_project_capture_targets(workflow_request) -> tuple[str, ...]:  # noqa: ANN001
    targets = getattr(workflow_request, "project_demo_capture_targets", ()) if workflow_request is not None else ()
    return tuple(
        target
        for target in targets
        if str(target or "").strip() in {"browser", "ios", "android"}
    )


def _qa_demo_missing_project_capture_targets(*, qa_plan, workflow_request) -> tuple[str, ...]:  # noqa: ANN001
    project_targets = _qa_demo_project_capture_targets(workflow_request)
    if not project_targets:
        return ()
    selected_targets = set(required_capture_targets(qa_plan))
    return tuple(target for target in project_targets if target not in selected_targets)


def _qa_demo_preview_force_requested(
    *,
    workflow_request,
    demo_recording_required: bool,
    execution_context: dict[str, object] | None,
    persisted_plan: object | None = None,
) -> bool:  # noqa: ANN001
    if demo_recording_required:
        return False
    entry_mode = str(getattr(workflow_request, "entry_mode", "") or "").strip().lower()
    entry_stage = str(getattr(workflow_request, "entry_stage", "") or "").strip().lower()
    return entry_mode == "resume" and entry_stage == "qa"


def _qa_demo_waiting_release_id(
    *,
    execution_context: dict[str, object] | None,
    persisted_plan: object | None,
) -> str | None:
    transient_release_id = str((execution_context or {}).get("qa_demo_waiting_release_id") or "").strip()
    persisted_release_id = ""
    snapshot = ExecutionSnapshot.load(persisted_plan)
    if snapshot is not None:
        persisted_release_id = str(
            (snapshot.context.execution_context or {}).get("qa_demo_waiting_release_id") or ""
        ).strip()
    if transient_release_id and persisted_release_id and transient_release_id != persisted_release_id:
        raise RuntimeError(
            "QA demo waiting release id conflict between dispatch context and persisted run snapshot: "
            f"{transient_release_id} != {persisted_release_id}"
        )
    return persisted_release_id or transient_release_id or None


def _qa_demo_waiting_preview_release(
    *,
    session,
    tenant_id: str,
    project_id: str,
    run_id: str,
    execution_context: dict[str, object] | None,
    persisted_plan: object | None = None,
):
    release_id = _qa_demo_waiting_release_id(
        execution_context=execution_context,
        persisted_plan=persisted_plan,
    )
    if not release_id:
        return None
    release = session.get(ProjectDeploymentRelease, release_id)
    if release is None:
        raise RuntimeError(f"QA demo recording is waiting for unknown preview release: {release_id}")
    if str(getattr(release, "tenant_id", "") or "").strip() != tenant_id:
        raise RuntimeError(f"QA demo waiting release is outside the run tenant scope: {release_id}")
    if str(getattr(release, "project_id", "") or "").strip() != project_id:
        raise RuntimeError(f"QA demo waiting release is outside the run project scope: {release_id}")
    if str(getattr(release, "source_run_id", "") or "").strip() != run_id:
        raise RuntimeError(f"QA demo waiting release is not owned by the run: {release_id}")
    if str(getattr(release, "release_kind", "") or "").strip() != "run_preview":
        raise RuntimeError(f"QA demo waiting release is not a run preview release: {release_id}")
    if _preview_release_status(release) in _QA_DEMO_PREVIEW_TERMINAL_FAILURE_STATUSES:
        return None
    return release


def _workflow_result_with_qa_blocker(*, workflow_result, attempt: int, message: str):
    return replace(
        workflow_result,
        outcome="blocked",
        blocker_message=message,
        orchestration_stage_trace=[
            *list(workflow_result.orchestration_stage_trace or []),
            _stage_trace_entry(stage="qa", attempt=attempt, status="blocked", summary=message),
        ],
    )


def _workflow_result_with_generic_qa_requeue(
    *,
    workflow_result,
    attempt: int,
    message: str,
    wait_for_release_id: str | None = None,
):
    trace_entry = _stage_trace_entry(stage="qa", attempt=attempt, status="requeue", summary=message)
    if wait_for_release_id is not None:
        trace_entry["wait_for_release_id"] = wait_for_release_id
        trace_entry["wait_reason"] = "qa_demo_preview_release"
    return replace(
        workflow_result,
        outcome="requeue",
        requeue_target=None,
        requeue_reason=message,
        orchestration_stage_trace=[
            *list(workflow_result.orchestration_stage_trace or []),
            trace_entry,
        ],
    )


def _preview_release_status(preview_release) -> str:
    return str(getattr(preview_release, "status", "") or "").strip().lower()
