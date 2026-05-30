from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.fernet import Fernet
from sqlalchemy import select

from orchestrator.core.platform.binding_resolution_service import check_project_bindings
from orchestrator.core.config import get_settings
from orchestrator.core.platform.install_request_service import (
    INSTALL_REQUEST_STATUS_APPROVED,
    INSTALL_REQUEST_STATUS_REJECTED,
    INSTALL_REQUEST_KIND_PROJECT_MISSING,
    approve_install_request,
    create_install_request,
    reject_install_request,
    ProjectInstallRequestWrite,
)
from orchestrator.core.runs.human_input_service import resume_run_from_human_input_answer
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot
from orchestrator.core.platform.trusted_install_executor import run_install
from orchestrator.storage.db import create_session_factory, reset_db_engine_cache
from orchestrator.storage.migrations import run_migrations
from orchestrator.storage.models import (
    Project,
    ProjectInstall,
    ProjectInstallRequest,
    RunHumanInputRequest,
    Tenant,
    WorkflowCheckpoint,
    WorkflowExecution,
)
from tests.workflow_test_support import add_human_input_request, add_run_with_workflow, make_run


class TestProjectInstallServices:
    def setup_method(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.database_url = f"sqlite:///{self.temp_dir.name}/project_install_services.db"
        os.environ["ORCHESTRATOR_DATABASE_URL"] = self.database_url
        os.environ["ORCHESTRATOR_SECRETS_ENCRYPTION_KEY"] = Fernet.generate_key().decode("utf-8")
        get_settings.cache_clear()
        reset_db_engine_cache()
        run_migrations(database_url=self.database_url)
        self.session_factory = create_session_factory(self.database_url)

    def teardown_method(self) -> None:
        self.temp_dir.cleanup()
        os.environ.pop("ORCHESTRATOR_DATABASE_URL", None)
        os.environ.pop("ORCHESTRATOR_SECRETS_ENCRYPTION_KEY", None)
        get_settings.cache_clear()
        reset_db_engine_cache()

    def test_create_install_request_persists_request_and_pauses_workflow(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="https://github.com/example/repo",
                jira_project_key="PA",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            run = make_run(
                run_id="run-1",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-1",
                issue_summary="Needs Fastlane",
                created_at=now,
                status="running",
            )
            session.add(tenant)
            session.add(project)
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()
            settings = get_settings()

            with patch("orchestrator.core.runs.human_input_service._dispatch_human_input_request"):
                request = create_install_request(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    run=run,
                    source_stage="dev",
                    payload=ProjectInstallRequestWrite(
                        kind="fastlane_lane",
                        label="iOS Beta Lane",
                        reason="Ticket requires Fastlane",
                        suggested_config={"working_dir": ".", "platform": "ios", "lane": "beta"},
                        required_bindings=("MATCH_PASSWORD",),
                    ),
                )

            persisted_run = session.get(type(run), run.run_id)
            workflow = session.get(WorkflowExecution, run.workflow_id)
            human_request = session.execute(select(RunHumanInputRequest)).scalar_one()

        assert request.request_kind == INSTALL_REQUEST_KIND_PROJECT_MISSING
        assert request.status == "pending"
        assert persisted_run is not None and persisted_run.status == "waiting_for_input"
        assert workflow is not None and workflow.status == "waiting_for_input"
        assert human_request is not None
        assert human_request.request_type == "install_request"

    def test_install_request_human_prompt_explains_dependency_approval(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="https://github.com/example/repo",
                jira_project_key="PA",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            run = make_run(
                run_id="run-2",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-248",
                issue_summary="Needs HubSpot",
                created_at=now,
                status="running",
            )
            session.add(tenant)
            session.add(project)
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()
            settings = get_settings()
            settings.admin_ui_base_url = "http://localhost:4100"

            with patch("orchestrator.core.runs.human_input_service._dispatch_human_input_request"):
                request = create_install_request(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    run=run,
                    source_stage="pm",
                    payload=ProjectInstallRequestWrite(
                        kind="integration",
                        label="HubSpot annual billing",
                        reason=(
                            "AP-248 needs permission to add HubSpot billing support in the project "
                            "and in Master Builder before implementation continues."
                        ),
                        suggested_config={
                            "operator_decision": (
                                "Can Master Builder add the HubSpot dependency work needed for AP-248?"
                            ),
                            "source_project_changes": ["Add HubSpot billing provider code to the project."],
                            "master_builder_changes": ["Configure Master Builder project support for HubSpot."],
                            "package_changes": ["Add HubSpot client libraries if the implementation needs them."],
                            "secrets_or_bindings_needed": ["HUBSPOT_ACCESS_TOKEN"],
                            "resume_when": "The operator replies yes or no.",
                        },
                        required_bindings=("HUBSPOT_ACCESS_TOKEN",),
                    ),
                )

            human_request = session.execute(select(RunHumanInputRequest)).scalar_one()

        assert request.request_kind == INSTALL_REQUEST_KIND_PROJECT_MISSING
        assert "needs approval before the run can continue" in human_request.prompt
        assert "Can Master Builder add the HubSpot dependency work needed for AP-248?" in human_request.prompt
        assert "Source project changes:" in human_request.prompt
        assert "Master Builder changes:" in human_request.prompt
        assert "Package or dependency changes:" in human_request.prompt
        assert "Secrets or bindings the operator may need to configure:" in human_request.prompt
        assert "http://localhost:4100/tenant-a/projects/project-a/installs" in human_request.prompt
        assert human_request.expected_reply_format == "Reply `yes` to approve, or `no` to stop and replan."
        context = human_request.request_context_json
        assert context["admin_url"] == "http://localhost:4100/tenant-a/projects/project-a/installs"
        assert context["questions"] == [
            {
                "id": "approve_install_dependency_work",
                "question": "Can Master Builder add the HubSpot dependency work needed for AP-248?",
                "options": ["yes - approve and continue", "no - stop and replan"],
            }
        ]

    def test_approve_install_request_creates_secret_placeholders_and_resumes_workflow(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="https://github.com/example/repo",
                jira_project_key="PA",
                policy_overrides={},
                environment={},
                secret_refs={"EXISTING_TOKEN": "tenant/tenant-a/EXISTING_TOKEN"},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            run = make_run(
                run_id="run-3",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-248",
                issue_summary="Needs HubSpot",
                created_at=now,
                status="running",
            )
            session.add(tenant)
            session.add(project)
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()
            settings = get_settings()

            with patch("orchestrator.core.runs.human_input_service._dispatch_human_input_request"):
                request = create_install_request(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    run=run,
                    source_stage="pm",
                    payload=ProjectInstallRequestWrite(
                        kind="integration",
                        label="HubSpot annual billing",
                        reason="Needs HubSpot billing dependency approval.",
                        suggested_config={},
                        required_bindings=("HUBSPOT_ACCESS_TOKEN", "EXISTING_TOKEN"),
                    ),
                )

            fake_runtime = SimpleNamespace(resume_input=lambda workflow, request: run)
            with patch("orchestrator.core.runs.human_input_service.build_workflow_runtime", return_value=fake_runtime):
                updated = approve_install_request(
                    session=session,
                    settings=settings,
                    request=request,
                    project=project,
                    source_ref="test",
                )

            persisted_project = session.get(Project, "project-a")
            human_request = session.execute(select(RunHumanInputRequest)).scalar_one()
            install = session.execute(select(ProjectInstall)).scalar_one()

        assert updated.status == INSTALL_REQUEST_STATUS_APPROVED
        assert persisted_project is not None
        assert persisted_project.secret_refs == {
            "EXISTING_TOKEN": "tenant/tenant-a/EXISTING_TOKEN",
            "HUBSPOT_ACCESS_TOKEN": "project/tenant-a/project-a/HUBSPOT_ACCESS_TOKEN",
        }
        assert human_request.status == "answered"
        assert human_request.answer_source_ref == "test"
        assert install.kind == "integration"
        assert install.label == "hubspot"
        assert install.enabled is True
        assert install.binding_names_json == ["HUBSPOT_ACCESS_TOKEN", "EXISTING_TOKEN"]

    def test_create_install_request_returns_existing_approved_request_without_reopening_input(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="https://github.com/example/repo",
                jira_project_key="PA",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            first_run = make_run(
                run_id="run-approved-1",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-248",
                issue_summary="Needs HubSpot",
                created_at=now,
                status="running",
            )
            second_run = make_run(
                run_id="run-approved-2",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-249",
                issue_summary="Needs HubSpot again",
                created_at=now,
                status="running",
            )
            session.add(tenant)
            session.add(project)
            add_run_with_workflow(session, first_run, workflow_status="running")
            add_run_with_workflow(session, second_run, workflow_status="running")
            session.commit()
            settings = get_settings()

            with patch("orchestrator.core.runs.human_input_service._dispatch_human_input_request"):
                request = create_install_request(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    run=first_run,
                    source_stage="pm",
                    payload=ProjectInstallRequestWrite(
                        kind="integration",
                        label="hubspot",
                        reason="Needs HubSpot billing dependency approval.",
                        suggested_config={},
                        required_bindings=(),
                    ),
                )

            fake_runtime = SimpleNamespace(resume_input=lambda workflow, request: first_run)
            with patch("orchestrator.core.runs.human_input_service.build_workflow_runtime", return_value=fake_runtime):
                approve_install_request(
                    session=session,
                    settings=settings,
                    request=request,
                    project=project,
                    source_ref="test",
                )

            reopened = create_install_request(
                session=session,
                settings=settings,
                tenant=tenant,
                project=project,
                run=second_run,
                source_stage="pm",
                payload=ProjectInstallRequestWrite(
                    kind="integration",
                    label="ap249_hubspot_install",
                    reason="Needs HubSpot billing dependency approval.",
                    suggested_config={},
                    required_bindings=(),
                ),
            )
            human_requests = session.execute(select(RunHumanInputRequest)).scalars().all()
            installs = session.execute(select(ProjectInstall)).scalars().all()

        assert reopened.request_id == request.request_id
        assert reopened.status == INSTALL_REQUEST_STATUS_APPROVED
        assert reopened.label == "hubspot"
        assert len(human_requests) == 1
        assert len(installs) == 1
        assert installs[0].kind == "integration"
        assert installs[0].label == "hubspot"

    def test_approve_duplicate_provider_request_resumes_active_pending_gate(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="https://github.com/example/repo",
                jira_project_key="PA",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            run = make_run(
                run_id="run-approved-duplicates",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-248",
                issue_summary="Needs HubSpot",
                created_at=now,
                status="waiting_for_input",
            )
            session.add(tenant)
            session.add(project)
            add_run_with_workflow(session, run, workflow_status="waiting_for_input")
            session.commit()
            settings = get_settings()

            with patch("orchestrator.core.runs.human_input_service._dispatch_human_input_request"):
                active_request = create_install_request(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    run=run,
                    source_stage="pm",
                    payload=ProjectInstallRequestWrite(
                        kind="integration",
                        label="hubspot_integration_for_ap_248",
                        reason="Needs HubSpot billing dependency approval.",
                        suggested_config={},
                        required_bindings=(),
                    ),
                )
            duplicate_request = ProjectInstallRequest(
                request_id="duplicate-hubspot-request",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                workflow_id=run.workflow_id,
                run_id=run.run_id,
                issue_key=run.issue_key,
                kind="integration",
                label="ap248_hubspot_install",
                reason="Duplicate HubSpot request row.",
                suggested_config_json={},
                required_bindings_json=[],
                status="pending",
                request_kind="unsupported_kind",
                created_at=now,
                updated_at=now,
            )
            session.add(duplicate_request)
            session.commit()

            fake_runtime = SimpleNamespace(resume_input=lambda workflow, request: run)
            with patch("orchestrator.core.runs.human_input_service.build_workflow_runtime", return_value=fake_runtime):
                approve_install_request(
                    session=session,
                    settings=settings,
                    request=duplicate_request,
                    project=project,
                    source_ref="test",
                )

            active_request = session.get(ProjectInstallRequest, active_request.request_id)
            duplicate_request = session.get(ProjectInstallRequest, duplicate_request.request_id)
            human_request = session.execute(select(RunHumanInputRequest)).scalar_one()

        assert active_request is not None
        assert active_request.status == INSTALL_REQUEST_STATUS_APPROVED
        assert active_request.label == "hubspot"
        assert duplicate_request is not None
        assert duplicate_request.status == INSTALL_REQUEST_STATUS_APPROVED
        assert duplicate_request.label == "hubspot"
        assert human_request.status == "answered"

    def test_reapproving_equivalent_approved_request_resumes_stale_pending_gate(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="https://github.com/example/repo",
                jira_project_key="PA",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            run = make_run(
                run_id="run-stale-approved-duplicates",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-248",
                issue_summary="Needs HubSpot",
                created_at=now,
                status="waiting_for_input",
            )
            session.add(tenant)
            session.add(project)
            add_run_with_workflow(session, run, workflow_status="waiting_for_input")
            session.commit()
            settings = get_settings()

            with patch("orchestrator.core.runs.human_input_service._dispatch_human_input_request"):
                context_request = create_install_request(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    run=run,
                    source_stage="pm",
                    payload=ProjectInstallRequestWrite(
                        kind="integration",
                        label="hubspot_integration_for_ap_248",
                        reason="Needs HubSpot billing dependency approval.",
                        suggested_config={},
                        required_bindings=(),
                    ),
                )
            clicked_request = ProjectInstallRequest(
                request_id="clicked-approved-hubspot-request",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                workflow_id=run.workflow_id,
                run_id=run.run_id,
                issue_key=run.issue_key,
                kind="integration",
                label="hubspot",
                reason="Duplicate HubSpot request row.",
                suggested_config_json={},
                required_bindings_json=[],
                status=INSTALL_REQUEST_STATUS_APPROVED,
                request_kind="unsupported_kind",
                created_at=now,
                updated_at=now,
            )
            context_request.status = INSTALL_REQUEST_STATUS_APPROVED
            context_request.label = "hubspot"
            human_request = session.execute(select(RunHumanInputRequest)).scalar_one()
            human_request.status = "expired"
            human_request.expires_at = now - timedelta(minutes=1)
            old_run = make_run(
                run_id="old-run-stale-approved-duplicates",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-248",
                issue_summary="Old HubSpot run",
                created_at=now,
                status="blocked",
            )
            add_run_with_workflow(session, old_run, workflow_status="blocked")
            old_request = ProjectInstallRequest(
                request_id="old-approved-hubspot-request",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                workflow_id=old_run.workflow_id,
                run_id=old_run.run_id,
                issue_key=old_run.issue_key,
                kind="integration",
                label="hubspot",
                reason="Old duplicate HubSpot request row.",
                suggested_config_json={},
                required_bindings_json=[],
                status=INSTALL_REQUEST_STATUS_APPROVED,
                request_kind="unsupported_kind",
                created_at=now,
                updated_at=now,
            )
            session.add(old_request)
            add_human_input_request(
                session,
                request_id="old-expired-hubspot-input",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                workflow_id=old_run.workflow_id,
                checkpoint_id="old-checkpoint",
                source_run_id=old_run.run_id,
                issue_key=old_run.issue_key,
                source_stage="pm",
                request_type="install_request",
                status="expired",
                expires_at=now - timedelta(minutes=1),
                request_context_json={"install_request_id": old_request.request_id},
                now=now,
            )
            session.add(clicked_request)
            session.commit()

            def resume_input(workflow, request):  # noqa: ANN001
                assert request.request_id == human_request.request_id
                return run

            fake_runtime = SimpleNamespace(resume_input=resume_input)
            with patch("orchestrator.core.runs.human_input_service.build_workflow_runtime", return_value=fake_runtime):
                approve_install_request(
                    session=session,
                    settings=settings,
                    request=clicked_request,
                    project=project,
                    source_ref="test",
                )

            human_request = session.get(RunHumanInputRequest, human_request.request_id)

        assert human_request is not None
        assert human_request.status == "answered"
        assert human_request.answer_source_ref == "test"

    def test_reject_install_request_resumes_without_creating_secret_placeholders(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="https://github.com/example/repo",
                jira_project_key="PA",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            run = make_run(
                run_id="run-4",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-248",
                issue_summary="Needs HubSpot",
                created_at=now,
                status="running",
            )
            session.add(tenant)
            session.add(project)
            add_run_with_workflow(session, run, workflow_status="running")
            session.commit()
            settings = get_settings()

            with patch("orchestrator.core.runs.human_input_service._dispatch_human_input_request"):
                request = create_install_request(
                    session=session,
                    settings=settings,
                    tenant=tenant,
                    project=project,
                    run=run,
                    source_stage="pm",
                    payload=ProjectInstallRequestWrite(
                        kind="integration",
                        label="HubSpot annual billing",
                        reason="Needs HubSpot billing dependency approval.",
                        suggested_config={},
                        required_bindings=("HUBSPOT_ACCESS_TOKEN",),
                    ),
                )

            fake_runtime = SimpleNamespace(resume_input=lambda workflow, request: run)
            with patch("orchestrator.core.runs.human_input_service.build_workflow_runtime", return_value=fake_runtime):
                updated = reject_install_request(
                    session=session,
                    settings=settings,
                    request=request,
                    source_ref="test",
                )

            persisted_project = session.get(Project, "project-a")
            human_request = session.execute(select(RunHumanInputRequest)).scalar_one()

        assert updated.status == INSTALL_REQUEST_STATUS_REJECTED
        assert persisted_project is not None
        assert persisted_project.secret_refs == {}
        assert human_request.status == "answered"
        assert human_request.answer_source_ref == "test"

    def test_consumed_human_input_with_failed_resume_run_can_create_new_resume_attempt(self) -> None:
        now = datetime.now(timezone.utc)
        with self.session_factory() as session:
            tenant = Tenant(
                tenant_id="tenant-a",
                name="Tenant A",
                is_enabled=True,
                jira_config={},
                github_config={},
                repos_config={},
                policy_config={},
                discord_config={},
                created_at=now,
                updated_at=now,
            )
            project = Project(
                project_id="project-a",
                tenant_id="tenant-a",
                name="Project A",
                github_repository="https://github.com/example/repo",
                jira_project_key="PA",
                policy_overrides={},
                environment={},
                secret_refs={},
                discord_config={},
                is_archived=False,
                created_at=now,
                updated_at=now,
            )
            source_run = make_run(
                run_id="source-run-retry",
                tenant_id="tenant-a",
                project_id="project-a",
                issue_key="PA-248",
                issue_summary="Needs HubSpot",
                created_at=now,
                status="blocked",
                pre_check_outcome="ready_for_agent",
            )
            failed_resume_run = make_run(
                run_id="failed-resume-run",
                tenant_id="tenant-a",
                project_id="project-a",
                workflow_id=source_run.workflow_id,
                issue_key="PA-248",
                issue_summary="Needs HubSpot",
                created_at=now,
                attempt_number=2,
                parent_run_id=source_run.run_id,
                entry_mode="resume",
                entry_stage="pm",
                entry_checkpoint_id="checkpoint-retry",
                status="failed",
                last_error="stale dispatching recovery",
                pre_check_outcome="ready_for_agent",
                finished_at=now,
            )
            checkpoint_snapshot = ExecutionSnapshot.empty()
            checkpoint_snapshot.context.execution_context["pre_check_outcome"] = "ready_for_agent"
            checkpoint = WorkflowCheckpoint(
                checkpoint_id="checkpoint-retry",
                workflow_id=source_run.workflow_id,
                run_id=source_run.run_id,
                checkpoint_kind="stage",
                stage="pm",
                payload_json=checkpoint_snapshot.dump(),
                codex_session_id=None,
                created_at=now,
                updated_at=now,
            )
            session.add(tenant)
            session.add(project)
            workflow = add_run_with_workflow(
                session,
                source_run,
                workflow_status="failed",
                latest_checkpoint=checkpoint,
                failure_reason="previous resume failed",
            )
            workflow.active_run_id = failed_resume_run.run_id
            session.add(failed_resume_run)
            request = add_human_input_request(
                session,
                request_id="input-retry",
                tenant_id=tenant.tenant_id,
                project_id=project.project_id,
                workflow_id=source_run.workflow_id,
                checkpoint_id=checkpoint.checkpoint_id,
                source_run_id=source_run.run_id,
                consumed_by_run_id=failed_resume_run.run_id,
                issue_key=source_run.issue_key,
                source_stage="pm",
                request_type="install_request",
                status="consumed",
                answer_source_ref="test",
                answered_at=now,
                now=now,
            )
            session.commit()

            resumed = resume_run_from_human_input_answer(
                session=session,
                settings=get_settings(),
                request=request,
            )
            session.commit()

            refreshed_request = session.get(RunHumanInputRequest, request.request_id)
            refreshed_workflow = session.get(WorkflowExecution, source_run.workflow_id)

        assert resumed.run_id != failed_resume_run.run_id
        assert resumed.status == "queued"
        assert resumed.parent_run_id == source_run.run_id
        assert resumed.entry_mode == "resume"
        assert refreshed_request is not None
        assert refreshed_request.status == "consumed"
        assert refreshed_request.consumed_by_run_id == resumed.run_id
        assert refreshed_workflow is not None
        assert refreshed_workflow.status == "queued"
        assert refreshed_workflow.active_run_id == resumed.run_id

    def test_binding_status_reports_secret_ref_source_without_value(self) -> None:
        project = SimpleNamespace(
            project_id="project-a",
            environment={"SUPABASE_URL": "https://example.supabase.co"},
            secret_refs={"SUPABASE_ANON_KEY": "tenant/tenant-a/SUPABASE_ANON_KEY"},
        )
        with patch("orchestrator.core.platform.binding_resolution_service.resolve_scoped_secret_ref", return_value=None):
            statuses = check_project_bindings(
                session=object(),  # type: ignore[arg-type]
                project=project,  # type: ignore[arg-type]
                tenant_id="tenant-a",
                encryption_key="enc-key",
                keys=["SUPABASE_URL", "SUPABASE_ANON_KEY", "MISSING_KEY"],
            )

        assert [(item.key, item.present, item.source) for item in statuses] == [
            ("SUPABASE_URL", True, "environment"),
            ("SUPABASE_ANON_KEY", False, "secret_ref"),
            ("MISSING_KEY", False, "missing"),
        ]

    def test_run_install_redacts_echoed_binding_values(self) -> None:
        with TemporaryDirectory() as repo_dir:
            install = SimpleNamespace(
                install_id="install-1",
                tenant_id="tenant-a",
                project_id="project-a",
                enabled=True,
                kind="fastlane_lane",
                label="iOS Beta Lane",
                config_json={"working_dir": ".", "platform": "ios", "lane": "beta", "use_bundle_exec": True},
                binding_names_json=["MATCH_PASSWORD"],
            )
            project = SimpleNamespace(project_id="project-a", tenant_id="tenant-a")

            with (
                patch(
                    "orchestrator.core.platform.trusted_install_executor.resolve_project_binding_values",
                    return_value={"MATCH_PASSWORD": "super-secret-value"},
                ),
                patch(
                    "orchestrator.core.platform.trusted_install_executor.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=0,
                        stdout="MATCH_PASSWORD=super-secret-value\nfinished",
                        stderr="",
                    ),
                ),
            ):
                payload = run_install(
                    session=object(),  # type: ignore[arg-type]
                    settings=SimpleNamespace(secrets_encryption_key="enc-key"),
                    project=project,  # type: ignore[arg-type]
                    install=install,  # type: ignore[arg-type]
                    repo_dir=Path(repo_dir),
                )

        assert payload["ok"] is True
        assert "super-secret-value" not in payload["stdout"]
        assert "[REDACTED]" in payload["stdout"]
