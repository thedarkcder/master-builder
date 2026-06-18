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


def _recording_artifact_metadata(capture_target: str, artifact_url: str) -> dict[str, str]:
    object_name = artifact_url.rsplit("/", 1)[-1]
    return {
        "capture_target": capture_target,
        "artifact_url": artifact_url,
        "object_key": f"tenant-1/project-1/run-1/{object_name}",
        "capture_reference": f"https://preview.example/{capture_target}",
        "content_sha256": "a" * 64,
        "release_commit_sha": "b" * 40,
        "release_context_sha256": "c" * 64,
    }


def _failure_evidence_metadata(capture_target: str, artifact_url: str) -> dict[str, str]:
    metadata = _recording_artifact_metadata(capture_target, artifact_url)
    metadata["error_message"] = f"QA Demo Ready was not visible for {capture_target}; app did not load"
    return metadata


def _cleanup_resource_refs() -> list[dict[str, str]]:
    return [
        {
            "resource_type": "release",
            "resource_id": "release-preview-1",
            "cleanup_action": "destroyed",
        },
        {
            "resource_type": "coolify_application",
            "resource_id": "app-preview-1",
            "cleanup_action": "destroyed",
        },
    ]


def _cleanup_evidence_metadata() -> dict[str, object]:
    return {
        "release_id": "release-preview-1",
        "proof_scope_id": "run-1-main-abcdef1",
        "commit_sha": "b" * 40,
        "cleanup_status": "completed",
        "cleanup_mode": "qa_demo_complete",
        "lease_state": "destroyed",
        "acquired_at": "2026-06-18T11:00:00+00:00",
        "expires_at": "2026-06-19T11:00:00+00:00",
        "destroy_reason": "qa_demo_complete",
        "destroyed_at": "2026-06-18T12:00:00+00:00",
        "resource_refs": _cleanup_resource_refs(),
    }


def _demo_proof_lease_metadata() -> dict[str, str]:
    return {
        "proof_scope_id": "run-1-main-abcdef1",
        "commit_sha": "b" * 40,
        "state": "active",
        "acquired_at": "2026-06-18T11:00:00+00:00",
        "expires_at": "2026-06-19T11:00:00+00:00",
    }


def _service_verification_metadata() -> dict[str, object]:
    return {
        "release_id": "release-preview-1",
        "release_kind": "run_preview",
        "release_status": "live",
        "release_commit_sha": "b" * 40,
        "required_service_kinds": ["website"],
        "service_urls": [
            {
                "service_kind": "website",
                "url": "https://preview.example",
                "status": "active",
                "service_name": "web",
                "service_key": "web",
            }
        ],
    }


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
            "trigger_mode": "from_run",
            "run_id": "run-1",
            "pr_url": "https://github.com/acme/project-a/pull/8",
            "required_capture_targets": ["browser", "ios", "android"],
            "required_recording_counts": {"browser": 1, "ios": 1, "android": 1},
        },
    )


def _demo_proof_event_metadata(event: str) -> dict[str, object] | None:
    if event in {
        "ReleaseLive",
        "PreviewCleanupCompleted",
        "FailurePreviewCleanupCompleted",
    }:
        return {
            "release_id": "release-preview-1",
            "release_kind": "run_preview",
            "release_status": "live",
            "release_commit_sha": "b" * 40,
            "demo_proof_lease": _demo_proof_lease_metadata(),
            **(
                {
                    "cleanup_status": "completed",
                    "cleanup_mode": "destroy_or_ttl",
                    "cleanup_evidence": _cleanup_evidence_metadata(),
                }
                if event.endswith("CleanupCompleted")
                else {}
            ),
        }
    if event == "ServiceVerificationPassed":
        return _service_verification_metadata()
    if event in {"RecordingCompleted", "EvidenceUploaded"}:
        return {
            "artifact_urls": [
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
            ],
            "recording_count": 3,
            "capture_targets": ["browser", "ios", "android"],
            "recordings": [
                _recording_artifact_metadata(
                    "browser",
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                ),
                _recording_artifact_metadata(
                    "ios",
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                ),
                _recording_artifact_metadata(
                    "android",
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
                ),
            ],
        }
    if event == "FailureEvidenceUploaded":
        artifact_urls = [
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios-failure.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android-failure.webm",
        ]
        return {
            "artifact_urls": artifact_urls,
            "failure_evidence_count": 3,
            "capture_targets": ["browser", "ios", "android"],
            "failure_evidence": [
                _failure_evidence_metadata("browser", artifact_urls[0]),
                _failure_evidence_metadata("ios", artifact_urls[1]),
                _failure_evidence_metadata("android", artifact_urls[2]),
            ],
        }
    if event in {"PREvidenceAttached", "PRFailureEvidenceAttached"}:
        if event == "PREvidenceAttached":
            return {
                "pr_url": "https://github.com/acme/project-a/pull/8",
                "artifact_urls": [
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
                ],
                "pr_body_sha256": "e" * 64,
            }
        return {
            "pr_url": "https://github.com/acme/project-a/pull/8",
            "artifact_urls": [
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios-failure.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android-failure.webm",
            ],
            "pr_body_sha256": "f" * 64,
        }
    return None


def _demo_proof_request_for_event(request: WorkflowAdvanceRequest, event: str) -> WorkflowAdvanceRequest:
    payload = dict(request.payload)
    metadata = _demo_proof_event_metadata(event)
    if metadata is not None:
        payload["event_metadata"] = metadata
    return replace(request, payload=payload, trigger=WorkflowTrigger(event=event))


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
                        "trigger_mode": "from_run",
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
                    trigger_mode="from_run",
                    trigger_event="run_success_before_ready_for_review",
                    run_id="run-1",
                    pr_url="https://github.com/acme/project-a/pull/8",
                    required_capture_targets=["browser", "ios", "android"],
                )

            assert result.workflow_id == "demo_proof:run-1-main-abcdef1"
            assert result.status == "waiting_for_input"
            assert captured_requests[0].trigger.event is None
            assert captured_requests[0].payload["trigger_mode"] == "from_run"
            assert captured_requests[0].execution.source.attributes["trigger_mode"] == "from_run"
            assert captured_requests[0].payload["request_id"].startswith(
                "demo-proof:tenant-a:project-a:run-1-main-abcdef1:run_success_before_ready_for_review:"
            )
            description = json.loads(session.execute(select(WorkflowExecution)).scalar_one().source_description or "{}")
            assert description["trigger_mode"] == "from_run"
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_lease",
                )
            ).scalar_one()
            assert operation.status == "waiting_for_input"
            assert operation.target_ref == "run-1-main-abcdef1"

    def test_start_demo_proof_workflow_rejects_missing_pr_url_before_creating_workflow(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            project = session.get(Project, "project-a")

            with patch("orchestrator.core.qa.demo_proof_start.build_workflow_runtime") as runtime_mock:
                try:
                    start_demo_proof_workflow(
                        session=session,
                        settings=SimpleNamespace(),
                        tenant=tenant,
                        project=project,
                        proof_scope_id="run-1-main-abcdef1",
                        commit_sha="abcdef1",
                        trigger_mode="from_run",
                        trigger_event="admin_workflow_start",
                        run_id="run-1",
                        pr_url=None,
                        required_capture_targets=["browser", "ios", "android"],
                    )
                except ValueError as exc:
                    assert "pr_url" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected demo proof start to require pr_url")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_demo_proof_handler_rejects_missing_trigger_mode_before_creating_workflow(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            payload = dict(request.payload)
            payload.pop("trigger_mode")

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "trigger_mode" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof to require trigger_mode")

            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_demo_proof_handler_rejects_trigger_mode_changes_for_existing_scope(self) -> None:
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
            changed_payload = {**request.payload, "trigger_mode": "from_pr"}

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, payload=changed_payload, trigger=WorkflowTrigger(event="ProofLeaseAcquired")),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "trigger_mode cannot change" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof to reject trigger_mode mutation")

    def test_demo_proof_handler_rejects_commit_changes_for_existing_scope(self) -> None:
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
            changed_payload = {**request.payload, "commit_sha": "fedcba9"}

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        request,
                        payload=changed_payload,
                        trigger=WorkflowTrigger(event="ProofLeaseAcquired"),
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "commit_sha cannot change" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof to reject commit mutation")

    def test_demo_proof_handler_rejects_required_capture_target_changes_for_existing_scope(self) -> None:
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
            changed_payload = {
                **request.payload,
                "required_capture_targets": ["browser"],
                "required_recording_counts": {"browser": 1},
            }

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        request,
                        payload=changed_payload,
                        trigger=WorkflowTrigger(event="ProofLeaseAcquired"),
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "required_capture_targets cannot change" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof to reject required capture target mutation")

    def test_demo_proof_handler_rejects_required_recording_count_changes_for_existing_scope(self) -> None:
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
            changed_payload = {
                **request.payload,
                "required_recording_counts": {"browser": 2, "ios": 1, "android": 1},
            }

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        request,
                        payload=changed_payload,
                        trigger=WorkflowTrigger(event="ProofLeaseAcquired"),
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "required_recording_counts cannot change" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof to reject required recording count mutation")

    def test_demo_proof_handler_rejects_run_identity_changes_for_existing_scope(self) -> None:
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
            changed_payload = {
                **request.payload,
                "run_id": "run-2",
                "pr_url": "https://github.com/acme/project-a/pull/9",
            }

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        request,
                        payload=changed_payload,
                        trigger=WorkflowTrigger(event="ProofLeaseAcquired"),
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "run_id cannot change" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof to reject run identity mutation")

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
                    trigger_mode="from_run",
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
                    trigger_mode="from_run",
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
                    request=_demo_proof_request_for_event(request, event),
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

    def test_demo_proof_rejects_completion_without_auditable_lifecycle_metadata(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "RecordingCompleted":
                    metadata = _demo_proof_event_metadata(event)
                    assert metadata is not None
                    recordings = list(metadata["recordings"])
                    recordings.append(
                        _recording_artifact_metadata(
                            "browser",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-variant.webm",
                        )
                    )
                    artifact_urls = list(metadata["artifact_urls"])
                    artifact_urls.append("https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-variant.webm")
                    event_request = replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": {
                                **metadata,
                                "artifact_urls": artifact_urls,
                                "recording_count": 4,
                                "capture_targets": ["browser", "browser", "ios", "android"],
                                "recordings": recordings,
                            },
                        },
                    )
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, trigger=WorkflowTrigger(event="PreviewCleanupCompleted")),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "requires auditable metadata" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require auditable metadata")

    def test_demo_proof_rejects_success_completion_when_evidence_metadata_misses_required_capture_target(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "EvidenceUploaded":
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        "artifact_urls": ["https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm"],
                        "recording_count": 1,
                        "capture_targets": ["browser"],
                        "recordings": [
                            _recording_artifact_metadata(
                                "browser",
                                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            )
                        ],
                    }
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "PreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "missing required capture target(s): android, ios" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require all capture targets")

    def test_demo_proof_rejects_success_completion_when_artifact_urls_do_not_cover_required_targets(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "EvidenceUploaded":
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        "artifact_urls": ["https://cdn.example/qa-demos/tenant-1/project-1/run-1/shared.webm"],
                        "recording_count": 1,
                        "capture_targets": ["browser", "ios", "android"],
                        "recordings": [
                            _recording_artifact_metadata(
                                "browser",
                                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/shared.webm",
                            )
                        ],
                    }
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "PreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "requires at least 3 playable artifact URL(s)" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require one artifact URL per target")

    def test_demo_proof_rejects_success_completion_when_artifact_urls_are_duplicated(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "EvidenceUploaded":
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        "artifact_urls": [
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/shared.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/shared.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/shared.webm",
                        ],
                        "recording_count": 3,
                        "capture_targets": ["browser", "ios", "android"],
                        "recordings": [
                            _recording_artifact_metadata(
                                "browser",
                                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/shared.webm",
                            ),
                            _recording_artifact_metadata(
                                "ios",
                                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/shared.webm",
                            ),
                            _recording_artifact_metadata(
                                "android",
                                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/shared.webm",
                            ),
                        ],
                    }
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "PreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "requires at least 3 distinct playable artifact URL(s)" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require distinct artifact URLs")

    def test_demo_proof_rejects_success_completion_without_per_target_artifact_url_mappings(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "EvidenceUploaded":
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        "artifact_urls": [
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
                        ],
                        "recording_count": 3,
                        "capture_targets": ["browser", "ios", "android"],
                    }
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "PreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "EvidenceUploaded.recordings" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require per-target artifact URL mappings")

    def test_demo_proof_rejects_success_completion_without_uploaded_artifact_metadata(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "EvidenceUploaded":
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        "artifact_urls": [
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
                        ],
                        "recording_count": 3,
                        "capture_targets": ["browser", "ios", "android"],
                        "recordings": [
                            {
                                "capture_target": "browser",
                                "artifact_url": (
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm"
                                ),
                            },
                            {
                                "capture_target": "ios",
                                "artifact_url": "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                            },
                            {
                                "capture_target": "android",
                                "artifact_url": (
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm"
                                ),
                            },
                        ],
                    }
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "PreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "missing uploaded artifact metadata" in str(exc)
                assert "browser" in str(exc)
                assert "ios" in str(exc)
                assert "android" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require uploaded artifact metadata")

    def test_demo_proof_rejects_success_completion_when_recording_release_commit_mismatches_release(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "EvidenceUploaded":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    recordings = [dict(item) for item in metadata["recordings"]]
                    recordings[1]["release_commit_sha"] = "c" * 40
                    metadata["recordings"] = recordings
                    payload["event_metadata"] = metadata
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "PreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "recording metadata release commit does not match" in str(exc)
                assert "ios" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require matching release commit metadata")

    def test_demo_proof_rejects_success_completion_without_cleanup_status_metadata(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "RecordingCompleted":
                    metadata = _demo_proof_event_metadata(event)
                    assert metadata is not None
                    recordings = list(metadata["recordings"])
                    recordings.append(
                        _recording_artifact_metadata(
                            "browser",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-variant.webm",
                        )
                    )
                    artifact_urls = list(metadata["artifact_urls"])
                    artifact_urls.append("https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-variant.webm")
                    event_request = replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": {
                                **metadata,
                                "artifact_urls": artifact_urls,
                                "recording_count": 4,
                                "capture_targets": ["browser", "browser", "ios", "android"],
                                "recordings": recordings,
                            },
                        },
                    )
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            terminal_request = _demo_proof_request_for_event(request, "PreviewCleanupCompleted")
            payload = dict(terminal_request.payload)
            payload["event_metadata"] = {
                "release_id": "release-preview-1",
                "release_kind": "run_preview",
                "release_status": "live",
            }
            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(terminal_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "PreviewCleanupCompleted.cleanup_status" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require cleanup status metadata")

    def test_demo_proof_rejects_success_completion_without_cleanup_evidence_metadata(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            terminal_request = _demo_proof_request_for_event(request, "PreviewCleanupCompleted")
            payload = dict(terminal_request.payload)
            payload["event_metadata"] = {
                "release_id": "release-preview-1",
                "release_kind": "run_preview",
                "release_status": "live",
                "cleanup_status": "completed",
                "cleanup_mode": "destroy_or_ttl",
            }
            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(terminal_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "PreviewCleanupCompleted.cleanup_evidence" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require cleanup evidence metadata")

    def test_demo_proof_rejects_success_completion_without_service_verification_metadata(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "ServiceVerificationPassed":
                    payload = dict(event_request.payload)
                    payload.pop("event_metadata", None)
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            terminal_request = _demo_proof_request_for_event(request, "PreviewCleanupCompleted")
            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=terminal_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "ServiceVerificationPassed.service_urls" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require service verification metadata")

    def test_demo_proof_rejects_success_completion_without_cleanup_resource_refs(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            terminal_request = _demo_proof_request_for_event(request, "PreviewCleanupCompleted")
            payload = dict(terminal_request.payload)
            metadata = dict(payload["event_metadata"])
            cleanup_evidence = dict(metadata["cleanup_evidence"])
            cleanup_evidence["resource_refs"] = []
            metadata["cleanup_evidence"] = cleanup_evidence
            payload["event_metadata"] = metadata
            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(terminal_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "PreviewCleanupCompleted.cleanup_evidence.resource_refs" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require cleanup resource refs")

    def test_demo_proof_rejects_success_completion_when_cleanup_release_mismatches_live_release(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            terminal_request = _demo_proof_request_for_event(request, "PreviewCleanupCompleted")
            payload = dict(terminal_request.payload)
            metadata = dict(payload["event_metadata"])
            cleanup_evidence = dict(metadata["cleanup_evidence"])
            cleanup_evidence["release_id"] = "release-preview-other"
            metadata["cleanup_evidence"] = cleanup_evidence
            payload["event_metadata"] = metadata
            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(terminal_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "cleanup evidence must reference ReleaseLive.release_id release-preview-1" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require matching cleanup release metadata")

    def test_demo_proof_rejects_success_completion_when_cleanup_lease_scope_mismatches_workflow(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            terminal_request = _demo_proof_request_for_event(request, "PreviewCleanupCompleted")
            payload = dict(terminal_request.payload)
            metadata = dict(payload["event_metadata"])
            cleanup_evidence = dict(metadata["cleanup_evidence"])
            cleanup_evidence["proof_scope_id"] = "run-2-main-abcdef1"
            metadata["cleanup_evidence"] = cleanup_evidence
            payload["event_metadata"] = metadata
            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(terminal_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "cleanup evidence must reference proof scope run-1-main-abcdef1" in str(exc)
                assert "run-2-main-abcdef1" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require matching cleanup lease scope metadata")

    def test_demo_proof_rejects_success_completion_when_pr_evidence_lacks_uploaded_artifact_urls(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "PREvidenceAttached":
                    event_request = replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": {
                                "pr_url": "https://github.com/acme/project-a/pull/8",
                                "pr_body_sha256": "e" * 64,
                            },
                        },
                    )
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "PreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "PR evidence metadata is missing uploaded artifact URL(s)" in str(exc)
                assert "browser.webm" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require PR evidence artifact URL metadata")

    def test_demo_proof_rejects_success_completion_when_required_recording_count_is_missing(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            request = replace(
                request,
                payload={
                    **request.payload,
                    "required_recording_counts": {"browser": 2, "ios": 1, "android": 1},
                },
            )
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "RecordingCompleted":
                    metadata = _demo_proof_event_metadata(event)
                    assert metadata is not None
                    recordings = list(metadata["recordings"])
                    recordings.append(
                        _recording_artifact_metadata(
                            "browser",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-variant.webm",
                        )
                    )
                    artifact_urls = list(metadata["artifact_urls"])
                    artifact_urls.append("https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-variant.webm")
                    event_request = replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": {
                                **metadata,
                                "artifact_urls": artifact_urls,
                                "recording_count": 4,
                                "capture_targets": ["browser", "browser", "ios", "android"],
                                "recordings": recordings,
                            },
                        },
                    )
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "PreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "requires 2 recording artifact(s) for capture target browser, got 1" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to enforce required recording counts")

    def test_demo_proof_rejects_recording_completed_when_required_recording_count_is_missing(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            request = replace(
                request,
                payload={
                    **request.payload,
                    "required_recording_counts": {"browser": 2, "ios": 1, "android": 1},
                },
            )
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "RecordingCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "requires 2 recording artifact(s) for capture target browser, got 1" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected recording completion to enforce required recording counts")

    def test_demo_proof_rejects_failure_completion_when_required_failure_targets_are_missing(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "FailureEvidenceUploaded":
                    artifact_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm"
                    event_request = replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": {
                                "artifact_urls": [artifact_url],
                                "failure_evidence_count": 1,
                                "capture_targets": ["browser"],
                                "failure_evidence": [
                                    _failure_evidence_metadata("browser", artifact_url),
                                ],
                            },
                        },
                    )
                if event == "PRFailureEvidenceAttached":
                    event_request = replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": {
                                "pr_url": "https://github.com/acme/project-a/pull/8",
                                "artifact_urls": [
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm"
                                ],
                                "pr_body_sha256": "f" * 64,
                            },
                        },
                    )
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "FailurePreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "failure evidence metadata is missing required capture target(s)" in str(exc)
                assert "ios" in str(exc)
                assert "android" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected failure completion to require evidence for every required target")

    def test_demo_proof_rejects_failure_completion_without_failure_capture_target_metadata(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "FailureEvidenceUploaded":
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        "artifact_urls": ["https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-failure-1.webm"],
                        "failure_evidence_count": 1,
                    }
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "FailurePreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "FailureEvidenceUploaded.capture_targets" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof failure completion to require capture target metadata")

    def test_demo_proof_rejects_failure_completion_without_diagnostic_evidence_metadata(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            request = replace(
                request,
                payload={
                    **request.payload,
                    "required_capture_targets": ["browser"],
                    "required_recording_counts": {"browser": 1},
                },
            )
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "FailureEvidenceUploaded":
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        "artifact_urls": [
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-failure-1.webm"
                        ],
                        "failure_evidence_count": 1,
                        "capture_targets": ["browser"],
                        "failure_evidence": [
                            {
                                "capture_target": "browser",
                                "artifact_url": (
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-failure-1.webm"
                                ),
                            }
                        ],
                    }
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "FailurePreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "failure evidence metadata is missing uploaded diagnostic artifact metadata" in str(exc)
                assert "browser" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof failure completion to require diagnostic evidence metadata")

    def test_demo_proof_rejects_failure_completion_without_uploaded_failure_artifact_metadata(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            request = replace(
                request,
                payload={
                    **request.payload,
                    "required_capture_targets": ["browser"],
                    "required_recording_counts": {"browser": 1},
                },
            )
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "FailureEvidenceUploaded":
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        "artifact_urls": [
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-failure-1.webm"
                        ],
                        "failure_evidence_count": 1,
                        "capture_targets": ["browser"],
                        "failure_evidence": [
                            {
                                "capture_target": "browser",
                                "artifact_url": (
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-failure-1.webm"
                                ),
                                "error_message": "QA Demo Ready was not visible; app did not load",
                            }
                        ],
                    }
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "FailurePreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "failure evidence metadata is missing uploaded diagnostic artifact metadata" in str(exc)
                assert "browser" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof failure completion to require failure artifact metadata")

    def test_demo_proof_rejects_failure_completion_when_failure_evidence_release_commit_mismatches_release(
        self,
    ) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            request = replace(
                request,
                payload={
                    **request.payload,
                    "required_capture_targets": ["browser"],
                    "required_recording_counts": {"browser": 1},
                },
            )
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "FailureEvidenceUploaded":
                    metadata = _demo_proof_event_metadata(event)
                    assert metadata is not None
                    failure_evidence = list(metadata["failure_evidence"])
                    failure_evidence[0] = {**failure_evidence[0], "release_commit_sha": "e" * 40}
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {**metadata, "failure_evidence": failure_evidence}
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "FailurePreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "failure evidence metadata release commit does not match ReleaseLive.release_commit_sha" in str(
                    exc
                )
                assert "browser" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof failure completion to reject mismatched release commit")

    def test_demo_proof_persists_lifecycle_event_metadata_for_auditable_chain(self) -> None:
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

            event_metadata = {
                "ReleaseLive": {
                    "release_id": "release-preview-1",
                    "release_kind": "run_preview",
                    "release_status": "live",
                },
                "RecordingCompleted": _demo_proof_event_metadata("RecordingCompleted"),
                "EvidenceUploaded": {
                    "artifact_urls": ["https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-demo-1.webm"],
                    "recording_count": 1,
                },
            }
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
            ):
                payload = dict(request.payload)
                if event in event_metadata:
                    payload["event_metadata"] = event_metadata[event]
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, payload=payload, trigger=WorkflowTrigger(event=event)),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            metadata_by_event = {
                item["event"]: item["metadata"]
                for item in description["demo_proof_event_metadata"]
            }
            assert metadata_by_event["ReleaseLive"] == event_metadata["ReleaseLive"]
            assert metadata_by_event["EvidenceUploaded"] == event_metadata["EvidenceUploaded"]

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
                    request=_demo_proof_request_for_event(request, event),
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
                request=_demo_proof_request_for_event(request, "RecordingCompleted"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            description = json.loads(workflow.source_description or "{}")
            assert description["recording_workflows"] == [
                {"capture_target": "browser", "state": "recorded"},
                {"capture_target": "ios", "state": "recorded"},
                {"capture_target": "android", "state": "recorded"},
            ]

    def test_demo_proof_rejects_pr_evidence_update_without_pr_url(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            request = replace(request, payload={**request.payload, "pr_url": None})
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            with self.assertRaisesRegex(RuntimeError, "requires PR URL before PR evidence update"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, trigger=WorkflowTrigger(event="EvidenceUploaded")),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

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
                    request=_demo_proof_request_for_event(request, event),
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
                    request=_demo_proof_request_for_event(request, event),
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
                    request=_demo_proof_request_for_event(request, event),
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
