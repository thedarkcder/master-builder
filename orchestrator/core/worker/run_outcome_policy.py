from __future__ import annotations

from dataclasses import replace

from orchestrator.core.deployment_previews import create_run_preview_deployment
from orchestrator.core.qa.demo_service import (
    execute_qa_demo_stage,
    next_required_qa_demo_worker_capability,
    qa_demo_recording_enabled,
    remaining_capture_targets,
    update_pull_request_with_demo_evidence,
)
from orchestrator.core.runs.service import RUN_STATUS_WAITING_FOR_INPUT
from orchestrator.core.worker.finalization import CompletionTailExecutor, WorkflowFinalizer
from orchestrator.core.workflow.execution_artifacts import latest_pushed_execution_artifact_for_run
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.workflow.runner import QaResult, WorkflowStageCheckpoint
from orchestrator.core.worker.capabilities import worker_label_for_capability


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
            try:
                if not demo_recording_required or qa_plan is not None:
                    preview_result = create_run_preview_deployment(
                        session=self._session,
                        tenant=prepared.tenant,
                        project=project,
                        run=run,
                        settings=self._settings,
                        pr_url=workflow_result.pr_url,
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
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=attempt,
                message=qa_result.blocker_message or qa_result.feedback or _summarize_qa_result(qa_result),
            )
        try:
            update_pull_request_with_demo_evidence(
                session=self._session,
                settings=self._settings,
                tenant=prepared.tenant,
                project=prepared.project,
                workflow_result=workflow_result,
                qa_result=qa_result,
            )
        except Exception as exc:  # noqa: BLE001
            return _workflow_result_with_qa_blocker(
                workflow_result=workflow_result,
                attempt=attempt,
                message=f"QA demo evidence PR update failed: {type(exc).__name__}: {exc}",
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


def _workflow_plan_for_qa(*, workflow_result, persisted_plan):
    if workflow_result.plan is not None:
        return workflow_result.plan
    snapshot = ExecutionSnapshot.load(persisted_plan)
    if snapshot is None:
        return None
    return snapshot.plan()


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
