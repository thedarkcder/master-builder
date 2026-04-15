from __future__ import annotations

from dataclasses import dataclass

from orchestrator.core.dashboard_links import admin_run_url
from orchestrator.core.project_policy import resolve_effective_policy
from orchestrator.core.worker.repo_setup_service import (
    RetryableRepoSetupError,
    TerminalRepoSetupError,
    repo_setup_attempt_count_from_plan,
)
from orchestrator.core.worker.stage_notifier import RunStageNotifier


@dataclass(frozen=True)
class PreparedRunDispatch:
    run: object
    tenant: object
    project: object
    notifier: RunStageNotifier
    effective_policy: dict
    workflow_request: object
    jira_issue_url: str | None
    run_dashboard_url: str | None
    agent_id: str
    worker_service_instance_id: str
    claim_id: str
    worker_workspace_key: str | None


class RunPreparationService:
    def __init__(
        self,
        *,
        session,
        settings,
        deps,
        emit_issue_assigned_fn,
        workspace_key_resolver_fn,
        ownership_matcher_fn,
        cleanup_run_workspaces_safe_fn,
    ) -> None:  # noqa: ANN001
        self._session = session
        self._settings = settings
        self._deps = deps
        self._emit_issue_assigned_fn = emit_issue_assigned_fn
        self._workspace_key_resolver_fn = workspace_key_resolver_fn
        self._ownership_matcher_fn = ownership_matcher_fn
        self._cleanup_run_workspaces_safe_fn = cleanup_run_workspaces_safe_fn

    def prepare(self, *, selection):  # noqa: ANN001
        if selection.terminal_run is not None:
            return selection.terminal_run
        if selection.run is None or selection.tenant is None:
            return None

        worker_workspace_key = self._workspace_key_resolver_fn(settings=self._settings)
        agent_id = self._deps.identity.resolve_agent_id_fn()
        worker_service_instance_id = self._deps.identity.resolve_worker_service_instance_id_fn()
        run = selection.run
        tenant = selection.tenant
        claim_id = str(getattr(run, "claim_id", "") or "").strip()
        self._deps.identity.logger.info(
            "worker_run_dispatch_started run_id=%s tenant_id=%s issue_key=%s worker_service_instance_id=%s claim_id=%s",
            run.run_id,
            run.tenant_id,
            run.issue_key,
            worker_service_instance_id,
            claim_id,
        )

        run, should_exit = self._promote_or_fail(
            run=run,
            worker_service_instance_id=worker_service_instance_id,
            claim_id=claim_id,
        )
        if should_exit or run is None:
            return run

        self._emit_issue_assigned_fn(run=run, agent_id=agent_id)

        project = getattr(selection, "project", None)
        if project is None:
            project = self._deps.project.resolve_project_for_run_fn(self._session, run=run)
        if project is None:
            return self._deps.project.fail_missing_project_mapping_fn(self._session, run=run)
        if project.is_archived:
            return self._deps.project.block_archived_project_fn(self._session, run=run, project=project)

        notifier = RunStageNotifier(
            session=self._session,
            tenant=tenant,
            run=run,
            settings=self._settings,
            project=project,
            send_discord_message=self._deps.stage_updates.send_discord_message_fn,
            send_jira_message=self._deps.stage_updates.send_jira_message_fn,
        )
        effective_policy = (
            dict(selection.effective_policy)
            if isinstance(getattr(selection, "effective_policy", None), dict)
            else resolve_effective_policy(
                tenant_policy=tenant.policy_config,
                project_overrides=project.policy_overrides,
            )
        )

        self._deps.project.bind_run_project_fn(self._session, run=run, project=project)
        jira_issue_url = self._deps.project.tenant_jira_issue_url_fn(
            session=self._session,
            tenant=tenant,
            issue_key=run.issue_key,
        )
        run_dashboard_url = admin_run_url(
            admin_ui_base_url=self._settings.admin_ui_base_url,
            run_id=run.run_id,
        )

        workflow_request = self._build_request_or_exit(
            tenant=tenant,
            run=run,
            project=project,
            notifier=notifier,
            effective_policy=effective_policy,
            worker_workspace_key=worker_workspace_key,
            worker_service_instance_id=worker_service_instance_id,
            claim_id=claim_id,
            jira_issue_url=jira_issue_url,
            run_dashboard_url=run_dashboard_url,
        )
        if workflow_request is None or getattr(workflow_request, "status", None):
            return workflow_request

        if self._deps.stage_updates.repo_setup_ready_update_fn is not None:
            notifier.append(
                self._deps.stage_updates.repo_setup_ready_update_fn(
                    tenant_id=run.tenant_id,
                    issue_key=run.issue_key,
                    run_id=run.run_id,
                    jira_url=jira_issue_url,
                    run_url=run_dashboard_url,
                )
            )
        notifier.append(
            self._deps.stage_updates.lock_acquired_update_fn(
                tenant_id=run.tenant_id,
                issue_key=run.issue_key,
                run_id=run.run_id,
                jira_url=jira_issue_url,
                run_url=run_dashboard_url,
            )
        )
        self._deps.identity.emit_agent_event_fn(
            event_type="LOCK_ACQUIRED",
            tenant_id=run.tenant_id,
            project_id=project.project_id,
            run_id=run.run_id,
            issue_key=run.issue_key,
            agent_id=agent_id,
        )

        return PreparedRunDispatch(
            run=run,
            tenant=tenant,
            project=project,
            notifier=notifier,
            effective_policy=effective_policy,
            workflow_request=workflow_request,
            jira_issue_url=jira_issue_url,
            run_dashboard_url=run_dashboard_url,
            agent_id=agent_id,
            worker_service_instance_id=worker_service_instance_id,
            claim_id=claim_id,
            worker_workspace_key=worker_workspace_key,
        )

    def _promote_or_fail(
        self,
        *,
        run,
        worker_service_instance_id: str,
        claim_id: str,
    ) -> tuple[object, bool]:
        if self._deps.execution.promote_run_to_running_fn is not None:
            promoted_run = self._deps.execution.promote_run_to_running_fn(
                self._session,
                run=run,
                expected_worker_service_instance_id=worker_service_instance_id,
                expected_claim_id=claim_id,
            )
            if promoted_run is None:
                self._deps.identity.logger.error(
                    "worker_run_promotion_failed run_id=%s tenant_id=%s issue_key=%s worker_service_instance_id=%s",
                    run.run_id,
                    run.tenant_id,
                    run.issue_key,
                    worker_service_instance_id,
                )
                return (
                    self._deps.execution.fail_guardrail_violation_fn(
                        self._session,
                        run=run,
                        error="Claimed run could not transition from dispatching to running",
                    ),
                    True,
                )
            run = promoted_run
            if not self._ownership_matcher_fn(
                run,
                expected_worker_service_instance_id=worker_service_instance_id,
                expected_claim_id=claim_id,
                expected_status=self._deps.statuses.running,
            ):
                current_owner = str(getattr(run, "worker_service_instance_id", "") or "").strip()
                current_claim_id = str(getattr(run, "claim_id", "") or "").strip()
                current_status = str(getattr(run, "status", "") or "").strip().lower()
                if (
                    current_owner != str(worker_service_instance_id or "").strip()
                    or current_claim_id != claim_id
                ):
                    self._deps.identity.logger.warning(
                        "worker_run_ownership_lost_before_execution run_id=%s tenant_id=%s issue_key=%s status=%s current_owner=%s expected_owner=%s current_claim_id=%s expected_claim_id=%s",
                        run.run_id,
                        run.tenant_id,
                        run.issue_key,
                        current_status,
                        current_owner,
                        worker_service_instance_id,
                        current_claim_id,
                        claim_id,
                    )
                    return run, True
                self._deps.identity.logger.error(
                    "worker_run_promotion_invalid_state run_id=%s tenant_id=%s issue_key=%s status=%s worker_service_instance_id=%s claim_id=%s",
                    run.run_id,
                    run.tenant_id,
                    run.issue_key,
                    current_status,
                    current_owner,
                    current_claim_id,
                )
                return (
                    self._deps.execution.fail_guardrail_violation_fn(
                        self._session,
                        run=run,
                        error="Claimed run could not transition from dispatching to running",
                    ),
                    True,
                )
        elif getattr(run, "status", None) == self._deps.statuses.dispatching:
            return (
                self._deps.execution.fail_guardrail_violation_fn(
                    self._session,
                    run=run,
                    error="Dispatching run reached execution without a running-state transition",
                ),
                True,
            )
        return run, False

    def _build_request_or_exit(
        self,
        *,
        tenant,
        run,
        project,
        notifier: RunStageNotifier,
        effective_policy: dict,
        worker_workspace_key: str,
        worker_service_instance_id: str,
        claim_id: str,
        jira_issue_url: str | None,
        run_dashboard_url: str | None,
    ):
        fail_project_repository_setup_fn = (
            self._deps.execution.fail_project_repository_setup_fn
            or self._deps.execution.fail_project_repository_checkout_fn
            or self._deps.execution.fail_guardrail_violation_fn
        )
        requeue_run_for_repo_setup_fn = self._deps.execution.requeue_run_for_repo_setup_fn
        if requeue_run_for_repo_setup_fn is None:

            def _fallback_requeue_run_for_repo_setup(  # noqa: ANN202
                session,
                *,
                run,
                stage_updates,
                error,
                expected_worker_service_instance_id=None,
                expected_claim_id=None,
            ):
                _ = (stage_updates, expected_worker_service_instance_id, expected_claim_id)
                return fail_project_repository_setup_fn(
                    session,
                    run=run,
                    error=error,
                )

            requeue_run_for_repo_setup_fn = _fallback_requeue_run_for_repo_setup

        try:
            return self._deps.execution.workflow_request_for_run_fn(
                self._session,
                tenant,
                run,
                project=project,
                effective_policy=effective_policy,
            )
        except RetryableRepoSetupError as exc:
            error_text = str(exc)
            self._deps.identity.logger.warning(
                "worker_repo_setup_retryable_failure run_id=%s tenant_id=%s issue_key=%s error=%s",
                run.run_id,
                run.tenant_id,
                run.issue_key,
                error_text,
            )
            repo_setup_attempts = repo_setup_attempt_count_from_plan(getattr(run, "plan", None)) + 1
            max_repo_setup_attempts = max(
                1,
                int(getattr(self._settings, "worker_repo_setup_max_attempts", 3)),
            )
            self._cleanup_run_workspaces_safe_fn(
                cleanup_run_workspaces_fn=self._deps.identity.cleanup_run_workspaces_fn,
                logger=self._deps.identity.logger,
                base_dir=self._settings.project_repo_checkout_base_dir,
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
                workspace_key=worker_workspace_key,
            )
            if repo_setup_attempts < max_repo_setup_attempts:
                if self._deps.stage_updates.run_requeued_repo_setup_update_fn is not None:
                    notifier.append(
                        self._deps.stage_updates.run_requeued_repo_setup_update_fn(
                            tenant_id=run.tenant_id,
                            issue_key=run.issue_key,
                            run_id=run.run_id,
                            jira_url=jira_issue_url,
                            run_url=run_dashboard_url,
                            error=error_text,
                        )
                    )
                return requeue_run_for_repo_setup_fn(
                    self._session,
                    run=run,
                    stage_updates=notifier.stage_updates,
                    error=error_text,
                    expected_worker_service_instance_id=worker_service_instance_id,
                    expected_claim_id=claim_id,
                )
            return fail_project_repository_setup_fn(
                self._session,
                run=run,
                error=f"{error_text} (repo setup retry budget exhausted)",
            )
        except TerminalRepoSetupError as exc:
            error_text = str(exc)
            self._deps.identity.logger.warning(
                "worker_repo_setup_terminal_failure run_id=%s tenant_id=%s issue_key=%s error=%s",
                run.run_id,
                run.tenant_id,
                run.issue_key,
                error_text,
            )
            self._cleanup_run_workspaces_safe_fn(
                cleanup_run_workspaces_fn=self._deps.identity.cleanup_run_workspaces_fn,
                logger=self._deps.identity.logger,
                base_dir=self._settings.project_repo_checkout_base_dir,
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
                workspace_key=worker_workspace_key,
            )
            return fail_project_repository_setup_fn(self._session, run=run, error=error_text)
        except (PermissionError, ValueError) as exc:
            self._deps.identity.logger.warning(
                "worker_workflow_request_build_failed run_id=%s tenant_id=%s issue_key=%s error=%s",
                run.run_id,
                run.tenant_id,
                run.issue_key,
                exc,
            )
            self._cleanup_run_workspaces_safe_fn(
                cleanup_run_workspaces_fn=self._deps.identity.cleanup_run_workspaces_fn,
                logger=self._deps.identity.logger,
                base_dir=self._settings.project_repo_checkout_base_dir,
                tenant_id=run.tenant_id,
                project_id=project.project_id,
                run_id=run.run_id,
                workspace_key=worker_workspace_key,
            )
            return self._deps.execution.fail_guardrail_violation_fn(self._session, run=run, error=str(exc))
