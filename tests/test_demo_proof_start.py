from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from orchestrator.core.qa.demo_proof_handlers import DemoProofWorkflowAdvanceHandler
from orchestrator.core.workflow.advance import WorkflowAdvanceRequest, WorkflowTrigger, execute_workflow_advance
from orchestrator.core.workflow.execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt
from tests.test_support.db_harness import SqliteTemplateDbTestCase


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _demo_proof_request(*, tenant) -> WorkflowAdvanceRequest:  # noqa: ANN001
    return WorkflowAdvanceRequest(
        workflow_handler_key="demo_proof",
        tenant_id="tenant-a",
        tenant=tenant,
        project_id="project-a",
        execution=WorkflowExecutionReference(
            key="run-1-main-abcdef1",
            source=WorkflowSourceReference(
                source_system="demo_proof",
                source_ref="run-1-main-abcdef1",
                display_name="Demo proof run-1-main-abcdef1",
            ),
        ),
        payload={
            "request_id": "request-1",
            "proof_scope_id": "run-1-main-abcdef1",
            "commit_sha": "abcdef1",
            "run_id": "run-1",
            "pr_url": "https://github.com/acme/project-a/pull/8",
            "required_capture_targets": ["browser", "ios", "android"],
        },
    )


class DemoProofStartTests(SqliteTemplateDbTestCase):
    def setUp(self) -> None:
        self.database_url = self._prepare_test_database(name_prefix="demo-proof-start")
        self.session_factory = create_session_factory(self.database_url)
        with self.session_factory() as session:
            now = _now()
            session.add(
                Tenant(
                    tenant_id="tenant-a",
                    name="Tenant A",
                    is_enabled=True,
                    archived_at=None,
                    purge_after_at=None,
                    jira_config={},
                    github_config={},
                    repos_config={},
                    policy_config={},
                    discord_config={},
                    experience_config={},
                    setup_state={},
                    created_at=now,
                    updated_at=now,
                )
            )
            session.add(
                Project(
                    project_id="project-a",
                    tenant_id="tenant-a",
                    name="Project A",
                    github_repository="acme/project-a",
                    jira_project_key="MAB",
                    policy_overrides={},
                    architecture_docs_config={},
                    environment={},
                    secret_refs={},
                    discord_config={},
                    is_archived=False,
                    created_at=now,
                    updated_at=now,
                )
            )
            session.commit()

    def tearDown(self) -> None:
        self._cleanup_test_database()

    def test_demo_proof_handler_creates_workflow_waiting_for_preview_lease(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")

            result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=WorkflowAdvanceRequest(
                    workflow_handler_key="demo_proof",
                    tenant_id="tenant-a",
                    tenant=tenant,
                    project_id="project-a",
                    execution=WorkflowExecutionReference(
                        key="run-1-main-abcdef1",
                        source=WorkflowSourceReference(
                            source_system="demo_proof",
                            source_ref="run-1-main-abcdef1",
                            display_name="Demo proof run-1-main-abcdef1",
                        ),
                    ),
                    payload={
                        "request_id": "request-1",
                        "proof_scope_id": "run-1-main-abcdef1",
                        "commit_sha": "abcdef1",
                        "run_id": "run-1",
                        "pr_url": "https://github.com/acme/project-a/pull/8",
                        "required_capture_targets": ["browser", "ios", "android"],
                    },
                ),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert result.handled is True
            assert result.reason == "preview_lease_requested"
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            assert workflow.workflow_id == "demo_proof:run-1-main-abcdef1"
            assert workflow.workflow_type_key == "demo_proof"
            assert workflow.status == "waiting_for_input"
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_lease",
                )
            ).scalars().all()
            assert len(operation) == 1
            operation = operation[0]
            assert operation.status == "waiting_for_input"
            assert operation.idempotency_key == "workflow-definition:preview_lease"
            assert operation.run_id == "run-1"
            assert operation.target_ref == "run-1-main-abcdef1"
            attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == operation.operation_id,
                )
            ).scalar_one()
            assert attempt.status == "waiting_for_input"

    def test_demo_proof_lease_acquired_event_completes_lease_and_waits_for_release_once(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            event_request = replace(request, trigger=WorkflowTrigger(event="ProofLeaseAcquired"))
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=request,
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            first_event = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=event_request,
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            duplicate_event = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=event_request,
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert first_event.reason == "release_requested"
            assert duplicate_event.reason == "release_requested"
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            assert workflow.status == "waiting_for_input"
            preview_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_lease",
                )
            ).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one()
            assert preview_operation.status == "completed"
            assert release_operation.status == "waiting_for_input"
            release_attempts = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == release_operation.operation_id,
                )
            ).scalars().all()
            assert len(release_attempts) == 1
            assert release_attempts[0].status == "waiting_for_input"

    def test_demo_proof_duplicate_start_reuses_waiting_preview_lease(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)

            first_start = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=request,
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            duplicate_start = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=request,
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert first_start.reason == "preview_lease_requested"
            assert duplicate_start.reason == "preview_lease_requested"
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            preview_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_lease",
                )
            ).scalar_one()
            preview_attempts = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == preview_operation.operation_id,
                )
            ).scalars().all()
            assert preview_operation.status == "waiting_for_input"
            assert len(preview_attempts) == 1

    def test_demo_proof_happy_path_events_complete_operations_in_order(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=request,
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            for event in (
                "ProofLeaseAcquired",
                "ServiceVerificationPassed",
                "RecordingCompleted",
                "EvidenceUploaded",
                "PREvidenceAttached",
                "PreviewCleanupCompleted",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, trigger=WorkflowTrigger(event=event)),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            assert workflow.status == "completed"
            operations = session.execute(
                select(WorkflowOperation).where(WorkflowOperation.workflow_id == workflow.workflow_id)
            ).scalars().all()
            status_by_type = {operation.operation_type: operation.status for operation in operations}
            assert status_by_type == {
                "preview_lease": "completed",
                "release": "completed",
                "recording": "completed",
                "evidence_upload": "completed",
                "pr_evidence_update": "completed",
                "preview_cleanup": "completed",
            }
