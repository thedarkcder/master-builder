from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from orchestrator.core.qa.demo_proof_handlers import DemoProofWorkflowAdvanceHandler
from orchestrator.core.qa.demo_proof_start import advance_demo_proof_workflow_event, start_demo_proof_workflow
from orchestrator.core.workflow.advance import WorkflowAdvanceRequest, WorkflowTrigger, execute_workflow_advance
from orchestrator.core.workflow.advance import execute_workflow_operation_retry
from orchestrator.core.workflow.execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow.handler_composition import build_installed_workflow_handler_registry
from orchestrator.core.workflow.operation_service import fail_workflow_operation
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

    def test_start_demo_proof_workflow_treats_trigger_event_as_start_metadata(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            project = session.get(Project, "project-a")
            captured_requests: list[WorkflowAdvanceRequest] = []
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")

            def _advance(request: WorkflowAdvanceRequest):  # noqa: ANN202
                captured_requests.append(request)
                return execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            with patch(
                "orchestrator.core.qa.demo_proof_start.build_workflow_runtime",
                return_value=SimpleNamespace(advance=_advance),
            ):
                result = start_demo_proof_workflow(
                    session=session,
                    settings=SimpleNamespace(),
                    tenant=tenant,
                    project=project,
                    proof_scope_id="run-1-main-abcdef1",
                    commit_sha="abcdef1",
                    trigger_event="run_success_before_ready_for_review",
                    run_id="run-1",
                    pr_url="https://github.com/acme/project-a/pull/8",
                    required_capture_targets=["browser", "ios", "android"],
                )

            assert result.workflow_id == "demo_proof:run-1-main-abcdef1"
            assert result.status == "waiting_for_input"
            assert captured_requests[0].trigger.event is None
            assert captured_requests[0].payload["request_id"].startswith(
                "demo-proof:tenant-a:project-a:run-1-main-abcdef1:run_success_before_ready_for_review:"
            )
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_lease",
                )
            ).scalar_one()
            assert operation.status == "waiting_for_input"
            assert operation.target_ref == "run-1-main-abcdef1"

    def test_advance_demo_proof_workflow_event_uses_explicit_lifecycle_event(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            project = session.get(Project, "project-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")

            def _advance(request: WorkflowAdvanceRequest):  # noqa: ANN202
                return execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            with patch(
                "orchestrator.core.qa.demo_proof_start.build_workflow_runtime",
                return_value=SimpleNamespace(advance=_advance),
            ):
                start_demo_proof_workflow(
                    session=session,
                    settings=SimpleNamespace(),
                    tenant=tenant,
                    project=project,
                    proof_scope_id="run-1-main-abcdef1",
                    commit_sha="abcdef1",
                    trigger_event="run_success_before_ready_for_review",
                    run_id="run-1",
                    pr_url="https://github.com/acme/project-a/pull/8",
                    required_capture_targets=["browser", "ios", "android"],
                )
                result = advance_demo_proof_workflow_event(
                    session=session,
                    settings=SimpleNamespace(),
                    tenant=tenant,
                    project=project,
                    proof_scope_id="run-1-main-abcdef1",
                    commit_sha="abcdef1",
                    event="ProofLeaseAcquired",
                    run_id="run-1",
                    pr_url="https://github.com/acme/project-a/pull/8",
                    required_capture_targets=["browser", "ios", "android"],
                )

            assert result.status == "waiting_for_input"
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one()
            assert release_operation.status == "waiting_for_input"

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
                "ReleaseRequested",
                "ReleaseProvisioning",
                "ReleaseLive",
                "RouteReady",
                "ServiceVerificationPassed",
                "RecordingStarted",
                "RecordingCompleted",
                "EvidenceUploadStarted",
                "EvidenceUploaded",
                "PREvidenceAttachStarted",
                "PREvidenceAttached",
                "PreviewCleanupRequested",
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
            description = json.loads(workflow.source_description or "{}")
            assert description["demo_proof_state"] == "complete"

    def test_demo_proof_tracks_required_platform_recording_workflows(self) -> None:
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
                "ReleaseRequested",
                "ReleaseProvisioning",
                "ReleaseLive",
                "RouteReady",
                "ServiceVerificationPassed",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, trigger=WorkflowTrigger(event=event)),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            recording_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "recording",
                )
            ).scalar_one()
            assert recording_operation.status == "waiting_for_input"
            assert "browser, ios, android" in str(recording_operation.summary)
            description = json.loads(workflow.source_description or "{}")
            assert description["recording_workflows"] == [
                {"capture_target": "browser", "state": "waiting_for_recording"},
                {"capture_target": "ios", "state": "waiting_for_recording"},
                {"capture_target": "android", "state": "waiting_for_recording"},
            ]

            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(request, trigger=WorkflowTrigger(event="RecordingStarted")),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            description = json.loads(workflow.source_description or "{}")
            assert description["recording_workflows"] == [
                {"capture_target": "browser", "state": "recording"},
                {"capture_target": "ios", "state": "recording"},
                {"capture_target": "android", "state": "recording"},
            ]

            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(request, trigger=WorkflowTrigger(event="RecordingCompleted")),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            description = json.loads(workflow.source_description or "{}")
            assert description["recording_workflows"] == [
                {"capture_target": "browser", "state": "recorded"},
                {"capture_target": "ios", "state": "recorded"},
                {"capture_target": "android", "state": "recorded"},
            ]

    def test_demo_proof_handler_rejects_service_verified_before_release_route_is_ready(self) -> None:
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
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(request, trigger=WorkflowTrigger(event="ProofLeaseAcquired")),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            with self.assertRaisesRegex(RuntimeError, "Cannot apply ServiceVerificationPassed"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, trigger=WorkflowTrigger(event="ServiceVerificationPassed")),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one()
            assert release_operation.status == "waiting_for_input"

    def test_demo_proof_release_failed_event_fails_waiting_release_operation_and_blocks_workflow(self) -> None:
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
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(request, trigger=WorkflowTrigger(event="ProofLeaseAcquired")),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(request, trigger=WorkflowTrigger(event="ReleaseRequested")),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(request, trigger=WorkflowTrigger(event="ReleaseFailed")),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert result.reason == "release_failed"
            assert result.failed is True
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one()
            release_attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == release_operation.operation_id,
                )
            ).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            assert workflow.status == "failed"
            assert workflow.last_error == "Demo proof event ReleaseFailed blocked proof scope run-1-main-abcdef1."
            assert description["demo_proof_state"] == "blocked"
            assert description["demo_proof_events"][-1] == "ReleaseFailed"
            assert release_operation.status == "failed"
            assert release_attempt.status == "failed"
            assert release_attempt.error_category == "release_failed"

    def test_demo_proof_pr_attach_failed_event_fails_waiting_pr_update_operation_and_blocks_workflow(self) -> None:
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
                "ReleaseRequested",
                "ReleaseProvisioning",
                "ReleaseLive",
                "RouteReady",
                "ServiceVerificationPassed",
                "RecordingStarted",
                "RecordingCompleted",
                "EvidenceUploadStarted",
                "EvidenceUploaded",
                "PREvidenceAttachStarted",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, trigger=WorkflowTrigger(event=event)),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(request, trigger=WorkflowTrigger(event="PREvidenceAttachFailed")),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert result.reason == "pr_evidence_attach_failed"
            assert result.failed is True
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == operation.operation_id,
                )
            ).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            assert workflow.status == "failed"
            assert description["demo_proof_state"] == "blocked"
            assert description["demo_proof_events"][-1] == "PREvidenceAttachFailed"
            assert operation.status == "failed"
            assert attempt.status == "failed"
            assert attempt.error_category == "pr_evidence_attach_failed"

    def test_demo_proof_full_lifecycle_events_complete_operations_in_order(self) -> None:
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
                "ReleaseRequested",
                "ReleaseProvisioning",
                "ReleaseLive",
                "RouteReady",
                "ServiceVerificationPassed",
                "RecordingStarted",
                "RecordingCompleted",
                "EvidenceUploadStarted",
                "EvidenceUploaded",
                "PREvidenceAttachStarted",
                "PREvidenceAttached",
                "PreviewCleanupRequested",
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

    def test_demo_proof_failure_evidence_events_complete_operations_then_block_workflow(self) -> None:
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
                "ReleaseRequested",
                "ReleaseProvisioning",
                "ReleaseLive",
                "RouteReady",
                "ServiceVerificationPassed",
                "RecordingStarted",
                "RecordingFailureEvidenceCaptured",
                "FailureEvidenceUploadStarted",
                "FailureEvidenceUploaded",
                "PRFailureEvidenceAttachStarted",
                "PRFailureEvidenceAttached",
                "FailurePreviewCleanupRequested",
                "FailurePreviewCleanupCompleted",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, trigger=WorkflowTrigger(event=event)),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            assert workflow.status == "failed"
            assert (
                workflow.last_error
                == "Demo proof recorded failure evidence for proof scope run-1-main-abcdef1."
            )
            description = json.loads(workflow.source_description or "{}")
            assert description["demo_proof_state"] == "blocked"
            assert description["demo_proof_events"][-1] == "FailurePreviewCleanupCompleted"
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

    def test_demo_proof_retry_reopens_failed_release_operation_waiting_for_event(self) -> None:
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
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(request, trigger=WorkflowTrigger(event="ProofLeaseAcquired")),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one()
            release_attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == release_operation.operation_id,
                )
            ).scalar_one()
            fail_workflow_operation(
                session,
                operation=release_operation,
                attempt=release_attempt,
                category="release_failed",
                message="Release provider did not become live.",
            )
            workflow.status = "failed"
            session.commit()
            registry = build_installed_workflow_handler_registry(
                integration_router=object(),
                extract_changed_fields_fn=lambda *_args, **_kwargs: (),
                extract_status_transition_fn=lambda *_args, **_kwargs: None,
                build_runtime_for_selector_fn=lambda *_args, **_kwargs: object(),
                seed_issues_with_runtime_fn=lambda *_args, **_kwargs: object(),
                post_jira_comment_fn=lambda *_args, **_kwargs: object(),
                create_jira_comment_fn=lambda *_args, **_kwargs: object(),
            )

            handle = execute_workflow_operation_retry(
                session=session,
                settings=SimpleNamespace(),
                session_factory=self.session_factory,
                workflow=workflow,
                operation=release_operation,
                resolve_operation_retry_handler_fn=registry.resolve_operation_retry_handler,
            )

            session.refresh(release_operation)
            attempts = session.execute(
                select(WorkflowOperationAttempt)
                .where(WorkflowOperationAttempt.operation_id == release_operation.operation_id)
                .order_by(WorkflowOperationAttempt.attempt_number)
            ).scalars().all()
            assert handle.operation_type == "release"
            assert handle.status == "waiting_for_input"
            assert workflow.status == "waiting_for_input"
            assert release_operation.status == "waiting_for_input"
            assert [attempt.status for attempt in attempts] == ["failed", "waiting_for_input"]
