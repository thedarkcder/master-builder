from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from orchestrator.core.qa.demo_proof_handlers import DemoProofWorkflowAdvanceHandler
from orchestrator.core.qa.demo_proof_start import advance_demo_proof_workflow_event, start_demo_proof_workflow
from orchestrator.core.workflow.advance import WorkflowAdvanceRequest, WorkflowTrigger, execute_workflow_advance
from orchestrator.core.workflow.advance import execute_workflow_operation_retry
from orchestrator.core.workflow.execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow.handler_composition import build_installed_workflow_handler_registry
from orchestrator.core.workflow.operation_service import fail_workflow_operation
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.core.workflow.work_units import workflow_work_unit_input_fingerprint
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import (
    Project,
    Tenant,
    WorkflowExecution,
    WorkflowOperation,
    WorkflowOperationAttempt,
    WorkflowOperationWorkUnit,
    WorkflowOperationWorkUnitAttempt,
)
from tests.test_support.db_harness import SqliteTemplateDbTestCase


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _recording_artifact_metadata(capture_target: str, artifact_url: str) -> dict[str, str]:
    object_name = artifact_url.rsplit("/", 1)[-1]
    return {
        "capture_target": capture_target,
        "artifact_url": artifact_url,
        "object_key": f"tenant-1/project-1/run-1/{object_name}",
        "capture_reference": _capture_reference_for_target(capture_target),
        "content_sha256": "a" * 64,
        "release_commit_sha": "b" * 40,
        "release_context_sha256": _default_release_context_sha256(),
    }


def _failure_evidence_metadata(capture_target: str, artifact_url: str) -> dict[str, str]:
    metadata = _recording_artifact_metadata(capture_target, artifact_url)
    metadata["error_message"] = f"QA Demo Ready was not visible for {capture_target}; app did not load"
    metadata["failure_phase"] = "app_load"
    metadata["observed_behavior"] = f"{capture_target} app did not load; QA Demo Ready marker was not visible"
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
        "lease_id": "demo-proof-lease:run-1-main-abcdef1:" + "b" * 40,
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


def _cleanup_failure_evidence_metadata() -> dict[str, object]:
    metadata = dict(_cleanup_evidence_metadata())
    metadata.update(
        {
            "cleanup_status": "failed",
            "cleanup_mode": "destroy_or_ttl",
            "lease_state": "cleanup_failed",
            "error_message": "Coolify deletion returned HTTP 500",
            "resource_refs": [
                {
                    **ref,
                    "cleanup_action": "cleanup_failed",
                }
                for ref in _cleanup_resource_refs()
            ],
        }
    )
    metadata.pop("destroy_reason", None)
    metadata.pop("destroyed_at", None)
    return metadata


def _demo_proof_lease_metadata() -> dict[str, str]:
    return {
        "lease_id": "demo-proof-lease:run-1-main-abcdef1:" + "b" * 40,
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


def _default_release_service_urls() -> list[dict[str, str]]:
    service_urls = _service_verification_metadata()["service_urls"]
    assert isinstance(service_urls, list)
    return [dict(item) for item in service_urls if isinstance(item, dict)]


def _default_release_context_sha256() -> str:
    payload = {
        "commit_sha": "b" * 40,
        "service_urls": _default_release_service_urls(),
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _capture_reference_for_target(capture_target: str) -> str:
    if capture_target == "browser":
        return "https://preview.example"
    return f"{capture_target}://configured"


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
        "ProofLeaseAcquired",
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
    if event == "RecordingFailed":
        return {
            "release_id": "release-preview-1",
            "release_kind": "run_preview",
            "release_status": "live",
            "release_commit_sha": "b" * 40,
            "demo_proof_lease": _demo_proof_lease_metadata(),
            "error_message": "QA demo recording failed before artifact evidence could be uploaded",
            "failure_evidence_unavailable_reason": "recording infrastructure failed before QA failure evidence upload",
        }
    if event == "RecordingFailedPreviewCleanupCompleted":
        return {
            "release_id": "release-preview-1",
            "release_kind": "run_preview",
            "release_status": "live",
            "release_commit_sha": "b" * 40,
            "demo_proof_lease": _demo_proof_lease_metadata(),
            "cleanup_status": "completed",
            "cleanup_mode": "destroy_or_ttl",
            "cleanup_evidence": _cleanup_evidence_metadata(),
        }
    if event == "ReleaseFailed":
        return {
            "release_id": "release-preview-1",
            "release_kind": "run_preview",
            "release_status": "failed",
            "release_commit_sha": "b" * 40,
            "demo_proof_lease": _demo_proof_lease_metadata(),
            "error_message": "Preview release failed before it became live",
        }
    if event == "ReleaseFailedPreviewCleanupCompleted":
        return {
            "release_id": "release-preview-1",
            "release_kind": "run_preview",
            "release_status": "destroyed",
            "release_commit_sha": "b" * 40,
            "demo_proof_lease": _demo_proof_lease_metadata(),
            "cleanup_status": "completed",
            "cleanup_mode": "destroy_or_ttl",
            "cleanup_evidence": _cleanup_evidence_metadata(),
        }
    if event == "PreviewCleanupFailed":
        return {
            "release_id": "release-preview-1",
            "release_kind": "run_preview",
            "release_status": "live",
            "release_commit_sha": "b" * 40,
            "demo_proof_lease": _demo_proof_lease_metadata(),
            "cleanup_status": "failed",
            "cleanup_mode": "destroy_or_ttl",
            "cleanup_evidence": _cleanup_failure_evidence_metadata(),
            "error_message": "Coolify deletion returned HTTP 500",
        }
    if event == "ServiceVerificationPassed":
        return _service_verification_metadata()
    if event == "ServiceVerificationFailed":
        metadata = _service_verification_metadata()
        metadata["error_message"] = "Website service returned HTTP 502 during verification"
        metadata["service_urls"] = [
            {
                "service_kind": "website",
                "url": "https://preview.example",
                "status": "failed",
            }
        ]
        artifact_urls = [
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios-failure.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android-failure.webm",
        ]
        metadata["artifact_urls"] = artifact_urls
        metadata["capture_targets"] = ["browser", "ios", "android"]
        metadata["failure_evidence"] = [
            _failure_evidence_metadata("browser", artifact_urls[0]),
            _failure_evidence_metadata("ios", artifact_urls[1]),
            _failure_evidence_metadata("android", artifact_urls[2]),
        ]
        return metadata
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
    if event in {"RecordingFailureEvidenceCaptured", "FailureEvidenceUploaded"}:
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
            artifact_urls = [
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
            ]
            return {
                "pr_url": "https://github.com/acme/project-a/pull/8",
                "artifact_urls": artifact_urls,
                "artifact_url_check_status": "passed",
                "checked_artifact_urls": artifact_urls,
                "pr_body_sha256": "e" * 64,
            }
        artifact_urls = [
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios-failure.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android-failure.webm",
        ]
        return {
            "pr_url": "https://github.com/acme/project-a/pull/8",
            "artifact_urls": artifact_urls,
            "artifact_url_check_status": "passed",
            "checked_artifact_urls": artifact_urls,
            "pr_body_sha256": "f" * 64,
        }
    if event == "PREvidenceAttachFailed":
        artifact_urls = [
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
        ]
        return {
            "pr_url": "https://github.com/acme/project-a/pull/8",
            "artifact_urls": artifact_urls,
            "artifact_url_check_status": "passed",
            "checked_artifact_urls": artifact_urls,
            "error_message": "GitHub rejected demo evidence update",
        }
    if event == "PREvidenceAttachFailedPreviewCleanupCompleted":
        return {
            "release_id": "release-preview-1",
            "release_kind": "run_preview",
            "release_status": "destroyed",
            "release_commit_sha": "b" * 40,
            "demo_proof_lease": _demo_proof_lease_metadata(),
            "cleanup_status": "completed",
            "cleanup_mode": "destroy_or_ttl",
            "cleanup_evidence": _cleanup_evidence_metadata(),
        }
    if event == "PRFailureEvidenceAttachFailed":
        artifact_urls = [
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios-failure.webm",
            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android-failure.webm",
        ]
        return {
            "pr_url": "https://github.com/acme/project-a/pull/8",
            "artifact_urls": artifact_urls,
            "artifact_url_check_status": "passed",
            "checked_artifact_urls": artifact_urls,
            "error_message": "GitHub rejected demo failure evidence update",
        }
    if event == "PRFailureEvidenceAttachFailedPreviewCleanupCompleted":
        return {
            "release_id": "release-preview-1",
            "release_kind": "run_preview",
            "release_status": "destroyed",
            "release_commit_sha": "b" * 40,
            "demo_proof_lease": _demo_proof_lease_metadata(),
            "cleanup_status": "completed",
            "cleanup_mode": "destroy_or_ttl",
            "cleanup_evidence": _cleanup_evidence_metadata(),
        }
    if event == "CleanupOnlyCompleted":
        return {
            "release_id": "release-preview-1",
            "release_kind": "run_preview",
            "release_status": "destroyed",
            "release_commit_sha": "b" * 40,
            "cleanup_status": "completed",
            "cleanup_mode": "destroy_or_ttl",
            "cleanup_evidence": _cleanup_evidence_metadata(),
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
                        "required_recording_counts": {"browser": 1, "ios": 1, "android": 1},
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
            work_units = session.execute(
                select(WorkflowOperationWorkUnit)
                .where(WorkflowOperationWorkUnit.operation_id == operation.operation_id)
                .order_by(WorkflowOperationWorkUnit.unit_key)
            ).scalars().all()
            expected_payload = {
                "proof_scope_id": "run-1-main-abcdef1",
                "commit_sha": "abcdef1",
                "trigger_mode": "from_run",
                "run_id": "run-1",
                "release_id": None,
                "pr_url": "https://github.com/acme/project-a/pull/8",
                "required_capture_targets": ["browser", "ios", "android"],
                "required_recording_counts": {"browser": 1, "ios": 1, "android": 1},
                "summary": "Acquire preview lease for demo proof scope run-1-main-abcdef1.",
            }
            assert [work_unit.unit_key for work_unit in work_units] == [
                "preview_lease.acquire",
                "preview_lease.enforce_single_active",
            ]
            assert [
                work_unit.input_fingerprint for work_unit in work_units
            ] == [
                workflow_work_unit_input_fingerprint(
                    {
                        "workflow_id": workflow.workflow_id,
                        "operation_id": operation.operation_id,
                        "operation_type": operation.operation_type,
                        "operation_attempt_id": attempt.attempt_id,
                        "unit_key": work_unit.unit_key,
                        "input": expected_payload,
                    }
                )
                for work_unit in work_units
            ]

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
                    required_recording_counts={"browser": 1, "ios": 1, "android": 1},
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
                        required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                    )
                except ValueError as exc:
                    assert "pr_url" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected demo proof start to require pr_url")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_rejects_from_run_without_run_id_before_creating_workflow(self) -> None:
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
                        run_id=None,
                        pr_url="https://github.com/acme/project-a/pull/8",
                        required_capture_targets=["browser", "ios", "android"],
                        required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                    )
                except ValueError as exc:
                    assert "from_run requires run_id" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected from_run demo proof start to require run_id")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_rejects_missing_required_capture_targets_before_creating_workflow(
        self,
    ) -> None:
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
                        pr_url="https://github.com/acme/project-a/pull/8",
                    )
                except ValueError as exc:
                    assert "required_capture_targets must be provided by PM demo requirements" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected demo proof start to require explicit capture targets")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_rejects_missing_required_recording_counts_before_creating_workflow(
        self,
    ) -> None:
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
                        pr_url="https://github.com/acme/project-a/pull/8",
                        required_capture_targets=["browser", "ios", "android"],
                    )
                except ValueError as exc:
                    assert "required_recording_counts must be provided by PM demo requirements" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected demo proof start to require explicit recording counts")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_rejects_incomplete_required_recording_counts_before_creating_workflow(
        self,
    ) -> None:
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
                        pr_url="https://github.com/acme/project-a/pull/8",
                        required_capture_targets=["browser", "ios", "android"],
                        required_recording_counts={"browser": 3},
                    )
                except ValueError as exc:
                    assert "required_recording_counts is missing required target(s): ios, android" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected demo proof start to require counts for every capture target")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_rejects_from_release_without_release_id_before_creating_workflow(
        self,
    ) -> None:
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
                        proof_scope_id="release-proof-1",
                        commit_sha="abcdef1",
                        trigger_mode="from_release",
                        trigger_event="admin_workflow_start",
                        pr_url="https://github.com/acme/project-a/pull/8",
                        required_capture_targets=["browser", "ios", "android"],
                        required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                    )
                except ValueError as exc:
                    assert "from_release requires release_id" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected from_release demo proof start to require release_id")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_rejects_from_pr_with_run_id_before_creating_workflow(
        self,
    ) -> None:
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
                        proof_scope_id="pr-8-abcdef1",
                        commit_sha="abcdef1",
                        trigger_mode="from_pr",
                        trigger_event="admin_pr_demo_proof_start",
                        run_id="run-1",
                        pr_url="https://github.com/acme/project-a/pull/8",
                        required_capture_targets=["browser", "ios", "android"],
                        required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                    )
                except ValueError as exc:
                    assert "from_pr must not include run_id" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected from_pr demo proof start to reject run_id")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_rejects_from_pr_with_release_id_before_creating_workflow(
        self,
    ) -> None:
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
                        proof_scope_id="pr-8-abcdef1",
                        commit_sha="abcdef1",
                        trigger_mode="from_pr",
                        trigger_event="admin_pr_demo_proof_start",
                        release_id="release-preview-1",
                        pr_url="https://github.com/acme/project-a/pull/8",
                        required_capture_targets=["browser", "ios", "android"],
                        required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                    )
                except ValueError as exc:
                    assert "from_pr must not include release_id" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected from_pr demo proof start to reject release_id")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_rejects_cleanup_only_without_release_id_before_creating_workflow(
        self,
    ) -> None:
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
                        trigger_mode="cleanup_only",
                        trigger_event="admin_cleanup_recovery",
                        pr_url=None,
                        required_capture_targets=["browser", "ios", "android"],
                        required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                    )
                except ValueError as exc:
                    assert "cleanup_only requires release_id" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected cleanup_only demo proof start to require release_id")

            runtime_mock.assert_not_called()
            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_rejects_retry_recording_without_existing_workflow(self) -> None:
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
                try:
                    start_demo_proof_workflow(
                        session=session,
                        settings=SimpleNamespace(),
                        tenant=tenant,
                        project=project,
                        proof_scope_id="run-1-main-abcdef1",
                        commit_sha="abcdef1",
                        trigger_mode="retry_recording",
                        trigger_event="admin_recording_retry",
                        run_id="run-1",
                        pr_url="https://github.com/acme/project-a/pull/8",
                        required_capture_targets=["browser", "ios", "android"],
                        required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                    )
                except RuntimeError as exc:
                    assert "retry_recording requires an existing demo proof workflow" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected recording retry to require an existing workflow")

            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_start_demo_proof_workflow_retry_recording_reuses_existing_deferred_recording(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            project = session.get(Project, "project-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)

            def _advance(request: WorkflowAdvanceRequest):  # noqa: ANN202
                return execute_workflow_advance(
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
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(
                    request,
                    payload={
                        **request.payload,
                        "event_metadata": {
                            "recorded_capture_targets": ["browser"],
                            "remaining_capture_targets": ["ios", "android"],
                        },
                    },
                    trigger=WorkflowTrigger(event="RecordingDeferred"),
                ),
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
                    trigger_mode="retry_recording",
                    trigger_event="admin_recording_retry",
                    run_id="run-1",
                    pr_url="https://github.com/acme/project-a/pull/8",
                    required_capture_targets=["browser", "ios", "android"],
                    required_recording_counts={"browser": 1, "ios": 1, "android": 1},
                )

            assert result.status == "waiting_for_input"
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            assert description["trigger_mode"] == "from_run"
            assert description["last_trigger_mode"] == "retry_recording"
            assert description["demo_proof_state"] == "recording"
            preview_lease_operations = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_lease",
                )
            ).scalars().all()
            assert len(preview_lease_operations) == 1
            recording_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "recording",
                )
            ).scalar_one()
            assert recording_operation.status == "waiting_for_input"

    def test_start_demo_proof_cleanup_only_starts_cleanup_without_pr_or_preview_lease(self) -> None:
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
                result = start_demo_proof_workflow(
                    session=session,
                    settings=SimpleNamespace(),
                    tenant=tenant,
                    project=project,
                    proof_scope_id="run-1-main-abcdef1",
                    commit_sha="abcdef1",
                    trigger_mode="cleanup_only",
                    trigger_event="admin_cleanup_recovery",
                    run_id=None,
                    release_id="release-preview-1",
                    pr_url=None,
                    required_capture_targets=["browser", "ios", "android"],
                    required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                )
                cleanup_result = advance_demo_proof_workflow_event(
                    session=session,
                    settings=SimpleNamespace(),
                    tenant=tenant,
                    project=project,
                    proof_scope_id="run-1-main-abcdef1",
                    commit_sha="abcdef1",
                    trigger_mode="cleanup_only",
                    event="CleanupOnlyCompleted",
                    run_id=None,
                    release_id="release-preview-1",
                    pr_url=None,
                    required_capture_targets=["browser", "ios", "android"],
                    required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                    event_metadata=_demo_proof_event_metadata("CleanupOnlyCompleted"),
                )

            assert result.status == "waiting_for_input"
            assert cleanup_result.status == "completed"
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            assert description["trigger_mode"] == "cleanup_only"
            assert description["demo_proof_state"] == "complete"
            operations = session.execute(
                select(WorkflowOperation).where(WorkflowOperation.workflow_id == workflow.workflow_id)
            ).scalars().all()
            status_by_type = {operation.operation_type: operation.status for operation in operations}
            assert status_by_type["preview_cleanup"] == "completed"
            assert status_by_type["preview_lease"] == "pending"
            preview_lease_operation = next(
                operation for operation in operations if operation.operation_type == "preview_lease"
            )
            preview_lease_attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == preview_lease_operation.operation_id
                )
            ).scalar_one_or_none()
            assert preview_lease_attempt is None

    def test_cleanup_only_completion_must_match_requested_release_id(self) -> None:
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

            wrong_release_metadata = _demo_proof_event_metadata("CleanupOnlyCompleted")
            assert wrong_release_metadata is not None
            wrong_release_metadata["release_id"] = "release-preview-other"
            cleanup_evidence = dict(wrong_release_metadata["cleanup_evidence"])  # type: ignore[arg-type]
            cleanup_evidence["release_id"] = "release-preview-other"
            cleanup_evidence["resource_refs"] = [
                {
                    "resource_type": "release",
                    "resource_id": "release-preview-other",
                    "cleanup_action": "destroyed",
                },
                {
                    "resource_type": "coolify_application",
                    "resource_id": "app-preview-other",
                    "cleanup_action": "destroyed",
                },
            ]
            wrong_release_metadata["cleanup_evidence"] = cleanup_evidence

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
                    trigger_mode="cleanup_only",
                    trigger_event="admin_cleanup_recovery",
                    run_id=None,
                    release_id="release-preview-1",
                    pr_url=None,
                    required_capture_targets=["browser", "ios", "android"],
                    required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                )
                try:
                    advance_demo_proof_workflow_event(
                        session=session,
                        settings=SimpleNamespace(),
                        tenant=tenant,
                        project=project,
                        proof_scope_id="run-1-main-abcdef1",
                        commit_sha="abcdef1",
                        trigger_mode="cleanup_only",
                        event="CleanupOnlyCompleted",
                        run_id=None,
                        release_id="release-preview-1",
                        pr_url=None,
                        required_capture_targets=["browser", "ios", "android"],
                        required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                        event_metadata=wrong_release_metadata,
                    )
                except RuntimeError as exc:
                    assert "cleanup_only requested release_id release-preview-1" in str(exc)
                else:  # pragma: no cover
                    raise AssertionError("expected cleanup_only completion to match requested release")

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

    def test_demo_proof_handler_rejects_cleanup_only_without_release_id_before_creating_workflow(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            cleanup_payload = {
                **request.payload,
                "trigger_mode": "cleanup_only",
                "run_id": None,
                "release_id": None,
                "pr_url": None,
            }

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, payload=cleanup_payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "cleanup_only requires release_id" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected cleanup_only demo proof handler to require release_id")

            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_demo_proof_handler_rejects_from_pr_with_run_id_before_creating_workflow(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            from_pr_payload = {
                **request.payload,
                "proof_scope_id": "pr-8-abcdef1",
                "trigger_mode": "from_pr",
                "run_id": "run-1",
                "release_id": None,
            }

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        request,
                        execution=WorkflowExecutionReference(
                            key="pr-8-abcdef1",
                            source=WorkflowSourceReference(
                                source_system="demo_proof",
                                source_ref="pr-8-abcdef1",
                                display_name="Demo proof pr-8-abcdef1",
                            ),
                        ),
                        payload=from_pr_payload,
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "from_pr must not include run_id" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected from_pr demo proof handler to reject run_id")

            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_demo_proof_handler_rejects_from_pr_with_release_id_before_creating_workflow(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            from_pr_payload = {
                **request.payload,
                "proof_scope_id": "pr-8-abcdef1",
                "trigger_mode": "from_pr",
                "run_id": None,
                "release_id": "release-preview-1",
            }

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        request,
                        execution=WorkflowExecutionReference(
                            key="pr-8-abcdef1",
                            source=WorkflowSourceReference(
                                source_system="demo_proof",
                                source_ref="pr-8-abcdef1",
                                display_name="Demo proof pr-8-abcdef1",
                            ),
                        ),
                        payload=from_pr_payload,
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "from_pr must not include release_id" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected from_pr demo proof handler to reject release_id")

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

    def test_demo_proof_handler_rejects_missing_required_capture_targets(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            payload = dict(request.payload)
            payload.pop("required_capture_targets")
            payload.pop("required_recording_counts")

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "required_capture_targets must be provided by PM demo requirements" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof handler to require explicit capture targets")

    def test_demo_proof_handler_rejects_unsupported_required_capture_targets(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            payload = {
                **request.payload,
                "required_capture_targets": ["browser", "tablet"],
                "required_recording_counts": {"browser": 1, "tablet": 1},
            }

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "required_capture_targets contains unsupported target(s): tablet" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof handler to reject unsupported capture targets")

            assert session.execute(select(WorkflowExecution)).scalar_one_or_none() is None

    def test_demo_proof_handler_rejects_missing_required_recording_counts(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            payload = dict(request.payload)
            payload.pop("required_recording_counts")

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "required_recording_counts must be provided by PM demo requirements" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof handler to require explicit recording counts")

    def test_demo_proof_handler_rejects_incomplete_required_recording_counts(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            payload = {
                **request.payload,
                "required_recording_counts": {"browser": 3},
            }

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "required_recording_counts is missing required target(s): ios, android" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof handler to require counts for every capture target")

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
                    required_recording_counts={"browser": 3, "ios": 3, "android": 3},
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
                    required_recording_counts={"browser": 3, "ios": 3, "android": 3},
                    event_metadata=_demo_proof_event_metadata("ProofLeaseAcquired"),
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

    def test_demo_proof_rejects_lease_acquired_without_scoped_lease_metadata(self) -> None:
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

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(request, trigger=WorkflowTrigger(event="ProofLeaseAcquired")),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "requires scoped lease metadata before release request" in str(exc)
                assert "ProofLeaseAcquired.demo_proof_lease.lease_id" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected lease acquisition to require scoped lease metadata")

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one_or_none()
            if release_operation is not None:
                assert release_operation.status != "waiting_for_input"

    def test_demo_proof_rejects_lease_acquired_when_lease_is_not_active(self) -> None:
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

            metadata = dict(_demo_proof_event_metadata("ProofLeaseAcquired") or {})
            demo_proof_lease = dict(metadata["demo_proof_lease"])
            demo_proof_lease["state"] = "destroyed"
            metadata["demo_proof_lease"] = demo_proof_lease

            with pytest.raises(RuntimeError, match="ProofLeaseAcquired.demo_proof_lease.state must be active"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        _demo_proof_request_for_event(request, "ProofLeaseAcquired"),
                        payload={**request.payload, "event_metadata": metadata},
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one_or_none()
            if release_operation is not None:
                assert release_operation.status != "waiting_for_input"

    def test_demo_proof_from_release_rejects_lease_for_different_release(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            from_release_payload = {
                **request.payload,
                "proof_scope_id": "release-proof-1",
                "trigger_mode": "from_release",
                "run_id": None,
                "release_id": "release-preview-1",
            }
            from_release_request = replace(
                request,
                execution=WorkflowExecutionReference(
                    key="release-proof-1",
                    source=WorkflowSourceReference(
                        source_system="demo_proof",
                        source_ref="release-proof-1",
                        display_name="Demo proof release-proof-1",
                    ),
                ),
                payload=from_release_payload,
            )
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=from_release_request,
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            event_request = _demo_proof_request_for_event(from_release_request, "ProofLeaseAcquired")
            event_metadata = dict(_demo_proof_event_metadata("ProofLeaseAcquired") or {})
            event_metadata["release_id"] = "release-preview-other"
            event_metadata["demo_proof_lease"] = {
                **dict(event_metadata["demo_proof_lease"]),
                "proof_scope_id": "release-proof-1",
            }

            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": event_metadata,
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "from_release requested release_id release-preview-1" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected from_release demo proof to reject a different release")

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one_or_none()
            if release_operation is not None:
                assert release_operation.status != "waiting_for_input"

    def test_demo_proof_lease_acquired_event_completes_lease_and_waits_for_release_once(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            event_request = _demo_proof_request_for_event(request, "ProofLeaseAcquired")
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

    def test_demo_proof_happy_path_allows_lease_acquired_before_release_identity_exists(self) -> None:
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
                event_request = _demo_proof_request_for_event(request, event)
                if event == "ProofLeaseAcquired":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    metadata.pop("release_id", None)
                    metadata.pop("release_commit_sha", None)
                    payload["event_metadata"] = metadata
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            assert workflow.status == "completed"
            description = json.loads(workflow.source_description or "{}")
            metadata_by_event = {
                item["event"]: item["metadata"]
                for item in description["demo_proof_event_metadata"]
                if isinstance(item, dict)
            }
            assert "release_id" not in metadata_by_event["ProofLeaseAcquired"]
            assert metadata_by_event["ReleaseLive"]["release_id"] == "release-preview-1"

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
                if event in {"RecordingCompleted", "EvidenceUploaded"}:
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
                if event == "PREvidenceAttached":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    artifact_urls = list(metadata["artifact_urls"])
                    artifact_urls.append("https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-variant.webm")
                    metadata["artifact_urls"] = artifact_urls
                    metadata["checked_artifact_urls"] = artifact_urls
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
                    request=replace(request, trigger=WorkflowTrigger(event="PreviewCleanupCompleted")),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "requires auditable metadata" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require auditable metadata")

    def test_demo_proof_rejects_evidence_upload_when_evidence_metadata_misses_required_capture_target(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            with pytest.raises(RuntimeError, match="missing required capture target\\(s\\): android, ios"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        _demo_proof_request_for_event(request, "EvidenceUploaded"),
                        payload={
                            **request.payload,
                            "event_metadata": {
                                "artifact_urls": [
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm"
                                ],
                                "recording_count": 1,
                                "capture_targets": ["browser"],
                                "recordings": [
                                    _recording_artifact_metadata(
                                        "browser",
                                        "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                                    )
                                ],
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_rejects_recording_completion_with_unplanned_capture_target(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            metadata = _demo_proof_event_metadata("RecordingCompleted")
            assert metadata is not None
            desktop_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/desktop.webm"
            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        _demo_proof_request_for_event(request, "RecordingCompleted"),
                        payload={
                            **request.payload,
                            "event_metadata": {
                                **metadata,
                                "artifact_urls": [*list(metadata["artifact_urls"]), desktop_url],
                                "capture_targets": ["browser", "ios", "android", "desktop"],
                                "recordings": [
                                    *list(metadata["recordings"]),
                                    _recording_artifact_metadata("desktop", desktop_url),
                                ],
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "recording completion metadata contains capture target(s) outside the demo plan: desktop" in str(
                    exc
                )
            else:  # pragma: no cover
                raise AssertionError("expected recording completion to reject unplanned capture target evidence")

    def test_demo_proof_rejects_recording_completion_with_mismatched_release_context(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "RecordingCompleted")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            recordings = [dict(item) for item in metadata["recordings"]]
            recordings[0]["release_context_sha256"] = "d" * 64
            metadata["recordings"] = recordings
            payload["event_metadata"] = metadata

            with pytest.raises(RuntimeError, match="RecordingCompleted.recordings release context does not match"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_rejects_browser_recording_completion_for_non_verified_release_url(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "RecordingCompleted")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            recordings = [dict(item) for item in metadata["recordings"]]
            recordings[0]["capture_reference"] = "https://fake-proof.example/browser"
            metadata["recordings"] = recordings
            payload["event_metadata"] = metadata

            with pytest.raises(RuntimeError, match="browser recording capture_reference must match verified release URL"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "EvidenceUploaded")
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
            with pytest.raises(RuntimeError, match="requires at least 3 playable artifact URL"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            pr_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            assert pr_operation.status == "pending"

    def test_demo_proof_rejects_evidence_upload_before_pr_update_when_capture_targets_are_missing(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            with pytest.raises(RuntimeError, match="evidence metadata is missing required capture target"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        _demo_proof_request_for_event(request, "EvidenceUploaded"),
                        payload={
                            **request.payload,
                            "event_metadata": {
                                "artifact_urls": [
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
                                ],
                                "recording_count": 1,
                                "capture_targets": ["browser"],
                                "recordings": [
                                    _recording_artifact_metadata(
                                        "browser",
                                        "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                                    )
                                ],
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            pr_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            assert pr_operation.status == "pending"

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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "EvidenceUploaded")
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
            with pytest.raises(RuntimeError, match="requires at least 3 distinct playable artifact URL"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            pr_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            assert pr_operation.status == "pending"

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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "EvidenceUploaded")
            with pytest.raises(RuntimeError, match="evidence metadata is missing uploaded artifact metadata"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": {
                                "artifact_urls": [
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser.webm",
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
                                ],
                                "recording_count": 3,
                                "capture_targets": ["browser", "ios", "android"],
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            pr_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            assert pr_operation.status == "pending"

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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "EvidenceUploaded")
            with pytest.raises(RuntimeError, match="evidence metadata is missing uploaded artifact metadata"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": {
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
                                        "artifact_url": (
                                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm"
                                        ),
                                    },
                                    {
                                        "capture_target": "android",
                                        "artifact_url": (
                                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm"
                                        ),
                                    },
                                ],
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            pr_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            assert pr_operation.status == "pending"

    def test_demo_proof_rejects_success_completion_when_artifact_urls_omit_required_walkthroughs(self) -> None:
        with self.session_factory() as session:
            tenant = session.get(Tenant, "tenant-a")
            workflow_type = get_workflow_type(session, workflow_type_key="demo_proof")
            request = _demo_proof_request(tenant=tenant)
            request = replace(
                request,
                payload={
                    **request.payload,
                    "required_capture_targets": ["browser"],
                    "required_recording_counts": {"browser": 3},
                },
            )
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=request,
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            artifact_urls = [
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-1.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-2.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-3.webm",
            ]
            recording_metadata = [_recording_artifact_metadata("browser", artifact_url) for artifact_url in artifact_urls]

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
                event_request = _demo_proof_request_for_event(request, event)
                if event == "RecordingCompleted":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    metadata["artifact_urls"] = [artifact_urls[0]]
                    metadata["recording_count"] = 3
                    metadata["capture_targets"] = ["browser"]
                    metadata["recordings"] = recording_metadata
                    payload["event_metadata"] = metadata
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "EvidenceUploaded")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            metadata["artifact_urls"] = [artifact_urls[0]]
            metadata["recording_count"] = 3
            metadata["capture_targets"] = ["browser"]
            metadata["recordings"] = recording_metadata
            payload["event_metadata"] = metadata
            with pytest.raises(RuntimeError) as exc_info:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            assert "EvidenceUploaded.artifact_urls is missing recording artifact URL(s)" in str(exc_info.value)
            assert artifact_urls[1] in str(exc_info.value)
            assert artifact_urls[2] in str(exc_info.value)

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            pr_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            assert pr_operation.status == "pending"

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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "RecordingCompleted":
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

            event_request = _demo_proof_request_for_event(request, "EvidenceUploaded")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            recordings = [dict(item) for item in metadata["recordings"]]
            recordings[1]["release_commit_sha"] = "c" * 40
            metadata["recordings"] = recordings
            payload["event_metadata"] = metadata
            with pytest.raises(RuntimeError) as exc_info:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            assert "recording metadata release commit does not match" in str(exc_info.value)
            assert "ios" in str(exc_info.value)

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            pr_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            assert pr_operation.status == "pending"

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
                if event in {"RecordingCompleted", "EvidenceUploaded"}:
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
                if event == "PREvidenceAttached":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    artifact_urls = list(metadata["artifact_urls"])
                    artifact_urls.append("https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-variant.webm")
                    metadata["artifact_urls"] = artifact_urls
                    metadata["checked_artifact_urls"] = artifact_urls
                    payload["event_metadata"] = metadata
                    event_request = replace(event_request, payload=payload)
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

    def test_demo_proof_rejects_cleanup_failure_without_cleanup_failure_evidence_metadata(self) -> None:
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

            terminal_request = _demo_proof_request_for_event(request, "PreviewCleanupFailed")
            payload = dict(terminal_request.payload)
            payload.pop("event_metadata", None)
            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(terminal_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "PreviewCleanupFailed.error_message" in str(exc)
                assert "PreviewCleanupFailed.cleanup_evidence" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof cleanup failure to require cleanup failure evidence")

    def test_demo_proof_records_cleanup_failure_with_cleanup_failure_evidence_metadata(self) -> None:
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

            result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=_demo_proof_request_for_event(request, "PreviewCleanupFailed"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert result.failed is True
            assert result.reason == "preview_cleanup_failed"
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            assert workflow.status == "failed"
            description = json.loads(workflow.source_description)
            metadata_by_event = {
                item["event"]: item["metadata"]
                for item in description["demo_proof_event_metadata"]
            }
            cleanup_metadata = metadata_by_event["PreviewCleanupFailed"]
            assert cleanup_metadata["cleanup_status"] == "failed"
            assert cleanup_metadata["cleanup_evidence"]["lease_state"] == "cleanup_failed"
            assert cleanup_metadata["cleanup_evidence"]["resource_refs"] == [
                {
                    "resource_type": "release",
                    "resource_id": "release-preview-1",
                    "cleanup_action": "cleanup_failed",
                },
                {
                    "resource_type": "coolify_application",
                    "resource_id": "app-preview-1",
                    "cleanup_action": "cleanup_failed",
                },
            ]

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

    def test_demo_proof_rejects_success_completion_without_lease_identity_metadata(self) -> None:
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
                if event == "ReleaseLive":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    demo_proof_lease = dict(metadata["demo_proof_lease"])
                    demo_proof_lease.pop("lease_id", None)
                    metadata["demo_proof_lease"] = demo_proof_lease
                    payload["event_metadata"] = metadata
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
                assert "ReleaseLive.demo_proof_lease" in str(exc)
                assert "lease_id" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to require lease identity metadata")

    def test_demo_proof_rejects_success_completion_when_live_release_lease_is_not_active(self) -> None:
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
                if event == "ReleaseLive":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    demo_proof_lease = dict(metadata["demo_proof_lease"])
                    demo_proof_lease["state"] = "destroyed"
                    metadata["demo_proof_lease"] = demo_proof_lease
                    payload["event_metadata"] = metadata
                    event_request = replace(event_request, payload=payload)
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            with pytest.raises(RuntimeError, match="ReleaseLive.demo_proof_lease.state must be active"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "PreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_rejects_success_completion_when_acquired_lease_mismatches_live_release(self) -> None:
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
                if event == "ProofLeaseAcquired":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    demo_proof_lease = dict(metadata["demo_proof_lease"])
                    demo_proof_lease["lease_id"] = "demo-proof-lease:run-2-main-abcdef1:" + "b" * 40
                    metadata["demo_proof_lease"] = demo_proof_lease
                    payload["event_metadata"] = metadata
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
                assert "ProofLeaseAcquired.demo_proof_lease.lease_id" in str(exc)
                assert "ReleaseLive.demo_proof_lease.lease_id" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to reject mismatched acquired lease")

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

    def test_demo_proof_rejects_success_completion_when_cleanup_refs_only_name_service_url(self) -> None:
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
            cleanup_evidence["resource_refs"] = [
                {
                    "resource_type": "release",
                    "resource_id": "release-preview-1",
                    "cleanup_action": "destroyed",
                },
                {
                    "resource_type": "service_url",
                    "resource_id": "https://preview.example",
                    "cleanup_action": "destroyed",
                },
            ]
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
                assert "managed preview resource" in str(exc)
                assert "service_url" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof completion to reject service-url-only cleanup refs")

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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "PREvidenceAttached")
            with pytest.raises(RuntimeError, match="PR evidence metadata is missing uploaded artifact URL"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        event_request,
                        payload={
                            **event_request.payload,
                            "event_metadata": {
                                "pr_url": "https://github.com/acme/project-a/pull/8",
                                "pr_body_sha256": "e" * 64,
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            assert cleanup_operation.status == "pending"

    def test_demo_proof_rejects_pr_evidence_attach_before_cleanup_when_pr_evidence_targets_another_pr(
        self,
    ) -> None:
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

            event_request = _demo_proof_request_for_event(request, "PREvidenceAttached")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            metadata["pr_url"] = "https://github.com/acme/project-a/pull/9"
            payload["event_metadata"] = metadata

            with pytest.raises(RuntimeError) as exc_info:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            assert "PREvidenceAttached.pr_url must match workflow pr_url" in str(exc_info.value)
            assert "pull/8" in str(exc_info.value)
            assert "pull/9" in str(exc_info.value)

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            assert cleanup_operation.status == "pending"

    def test_demo_proof_rejects_pr_evidence_attach_before_cleanup_when_pr_body_sha_is_not_a_digest(
        self,
    ) -> None:
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

            event_request = _demo_proof_request_for_event(request, "PREvidenceAttached")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            metadata["pr_body_sha256"] = "not-a-sha256"
            payload["event_metadata"] = metadata

            with pytest.raises(
                RuntimeError,
                match="PREvidenceAttached.pr_body_sha256 must be a SHA-256 hex digest",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            assert cleanup_operation.status == "pending"

    def test_demo_proof_rejects_success_completion_when_pr_artifact_links_were_not_checked(self) -> None:
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

            event_request = _demo_proof_request_for_event(request, "PREvidenceAttached")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            metadata.pop("artifact_url_check_status", None)
            metadata.pop("checked_artifact_urls", None)
            payload["event_metadata"] = metadata

            with pytest.raises(RuntimeError, match="PREvidenceAttached artifact URL checks must pass"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            assert cleanup_operation.status == "pending"

    def test_demo_proof_rejects_pr_evidence_attach_before_cleanup_when_artifact_links_were_not_checked(
        self,
    ) -> None:
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

            event_request = _demo_proof_request_for_event(request, "PREvidenceAttached")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            metadata.pop("artifact_url_check_status", None)
            metadata.pop("checked_artifact_urls", None)
            payload["event_metadata"] = metadata

            with pytest.raises(RuntimeError, match="PREvidenceAttached artifact URL checks must pass"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            assert cleanup_operation.status == "pending"

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

            with pytest.raises(RuntimeError) as exc_info:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, "EvidenceUploaded"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            assert "EvidenceUploaded.recordings must match RecordingCompleted.recordings" in str(exc_info.value)
            assert "browser" in str(exc_info.value)

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            pr_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            assert pr_operation.status == "pending"

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

    def test_demo_proof_rejects_failure_evidence_upload_when_required_failure_targets_are_missing(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            artifact_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm"
            with pytest.raises(RuntimeError, match="failure evidence metadata is missing required capture target"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        _demo_proof_request_for_event(request, "FailureEvidenceUploaded"),
                        payload={
                            **request.payload,
                            "event_metadata": {
                                "artifact_urls": [artifact_url],
                                "failure_evidence_count": 1,
                                "capture_targets": ["browser"],
                                "failure_evidence": [
                                    _failure_evidence_metadata("browser", artifact_url),
                                ],
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_rejects_failure_evidence_capture_with_mismatched_release_context(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "RecordingFailureEvidenceCaptured")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            failure_evidence = [dict(item) for item in metadata["failure_evidence"]]
            failure_evidence[0]["release_context_sha256"] = "d" * 64
            metadata["failure_evidence"] = failure_evidence
            payload["event_metadata"] = metadata

            with pytest.raises(
                RuntimeError,
                match="RecordingFailureEvidenceCaptured.failure_evidence release context does not match",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_rejects_browser_failure_evidence_capture_for_non_verified_release_url(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "RecordingFailureEvidenceCaptured")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            failure_evidence = [dict(item) for item in metadata["failure_evidence"]]
            failure_evidence[0]["capture_reference"] = "https://fake-proof.example/browser"
            metadata["failure_evidence"] = failure_evidence
            payload["event_metadata"] = metadata

            with pytest.raises(
                RuntimeError,
                match="browser failure_evidence capture_reference must match verified release URL",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_rejects_failure_evidence_upload_with_unplanned_capture_target(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            metadata = _demo_proof_event_metadata("FailureEvidenceUploaded")
            assert metadata is not None
            desktop_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/desktop-failure.webm"
            try:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        _demo_proof_request_for_event(request, "FailureEvidenceUploaded"),
                        payload={
                            **request.payload,
                            "event_metadata": {
                                **metadata,
                                "artifact_urls": [*list(metadata["artifact_urls"]), desktop_url],
                                "capture_targets": ["browser", "ios", "android", "desktop"],
                                "failure_evidence": [
                                    *list(metadata["failure_evidence"]),
                                    _failure_evidence_metadata("desktop", desktop_url),
                                ],
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "failure evidence metadata contains capture target(s) outside the demo plan: desktop" in str(
                    exc
                )
            else:  # pragma: no cover
                raise AssertionError("expected failure evidence upload to reject unplanned capture target evidence")

    def test_demo_proof_rejects_failure_evidence_without_observed_problem_detail(self) -> None:
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
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "RecordingFailureEvidenceCaptured":
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
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            artifact_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm"
            failure_metadata = _failure_evidence_metadata("browser", artifact_url)
            failure_metadata.pop("observed_behavior", None)

            with pytest.raises(
                RuntimeError,
                match="FailureEvidenceUploaded.failure_evidence.browser.observed_behavior",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        _demo_proof_request_for_event(request, "FailureEvidenceUploaded"),
                        payload={
                            **request.payload,
                            "event_metadata": {
                                "artifact_urls": [artifact_url],
                                "failure_evidence_count": 1,
                                "capture_targets": ["browser"],
                                "failure_evidence": [failure_metadata],
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_rejects_failure_evidence_upload_before_pr_update_when_capture_targets_are_missing(
        self,
    ) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            artifact_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm"
            with pytest.raises(RuntimeError, match="failure evidence metadata is missing required capture target"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        _demo_proof_request_for_event(request, "FailureEvidenceUploaded"),
                        payload={
                            **request.payload,
                            "event_metadata": {
                                "artifact_urls": [
                                    artifact_url,
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios-failure.webm",
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android-failure.webm",
                                ],
                                "failure_evidence_count": 1,
                                "capture_targets": ["browser"],
                                "failure_evidence": [
                                    _failure_evidence_metadata("browser", artifact_url),
                                ],
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            pr_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            assert pr_operation.status == "pending"

    def test_demo_proof_rejects_failure_evidence_upload_without_failure_capture_target_metadata(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            with pytest.raises(RuntimeError, match="FailureEvidenceUploaded.capture_targets|missing required capture target"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(
                        _demo_proof_request_for_event(request, "FailureEvidenceUploaded"),
                        payload={
                            **request.payload,
                            "event_metadata": {
                                "artifact_urls": [
                                    "https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-failure-1.webm"
                                ],
                                "failure_evidence_count": 1,
                            },
                        },
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_rejects_pr_failure_evidence_attach_before_cleanup_when_pr_targets_another_pr(
        self,
    ) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "PRFailureEvidenceAttached")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            metadata["pr_url"] = "https://github.com/acme/project-a/pull/9"
            payload["event_metadata"] = metadata

            with pytest.raises(RuntimeError) as exc_info:
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            assert "PRFailureEvidenceAttached.pr_url must match workflow pr_url" in str(exc_info.value)
            assert "pull/8" in str(exc_info.value)
            assert "pull/9" in str(exc_info.value)

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            assert cleanup_operation.status == "pending"

    def test_demo_proof_rejects_failure_completion_when_artifact_urls_omit_failure_evidence(self) -> None:
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
            artifact_urls = [
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure-1.webm",
                "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure-2.txt",
            ]
            failure_metadata = [_failure_evidence_metadata("browser", artifact_url) for artifact_url in artifact_urls]

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
                if event in {"RecordingFailureEvidenceCaptured", "FailureEvidenceUploaded"}:
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    metadata["artifact_urls"] = [artifact_urls[0]]
                    metadata["failure_evidence_count"] = 2
                    metadata["capture_targets"] = ["browser"]
                    metadata["failure_evidence"] = failure_metadata
                    payload["event_metadata"] = metadata
                    event_request = replace(event_request, payload=payload)
                if event == "PRFailureEvidenceAttached":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    metadata["artifact_urls"] = [artifact_urls[0]]
                    metadata["checked_artifact_urls"] = [artifact_urls[0]]
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
                    request=_demo_proof_request_for_event(request, "FailurePreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "FailureEvidenceUploaded.artifact_urls is missing failure_evidence artifact URL(s)" in str(exc)
                assert artifact_urls[1] in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof failure completion to reject missing failure artifact URLs")

    def test_demo_proof_rejects_pr_failure_evidence_attach_before_cleanup_when_body_sha_is_not_a_digest(
        self,
    ) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "PRFailureEvidenceAttached")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            metadata["pr_body_sha256"] = "not-a-sha256"
            payload["event_metadata"] = metadata

            with pytest.raises(
                RuntimeError,
                match="PRFailureEvidenceAttached.pr_body_sha256 must be a SHA-256 hex digest",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            assert cleanup_operation.status == "pending"

    def test_demo_proof_rejects_pr_failure_evidence_attach_before_cleanup_when_artifact_links_were_not_checked(
        self,
    ) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "PRFailureEvidenceAttached")
            payload = dict(event_request.payload)
            metadata = dict(payload["event_metadata"])
            metadata.pop("artifact_url_check_status", None)
            metadata.pop("checked_artifact_urls", None)
            payload["event_metadata"] = metadata

            with pytest.raises(
                RuntimeError,
                match="PRFailureEvidenceAttached artifact URL checks must pass",
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            assert cleanup_operation.status == "pending"

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
                if event == "PRFailureEvidenceAttached":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    artifact_urls = ["https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-failure-1.webm"]
                    metadata["artifact_urls"] = artifact_urls
                    metadata["checked_artifact_urls"] = artifact_urls
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
                if event == "PRFailureEvidenceAttached":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    artifact_urls = ["https://cdn.example/qa-demos/tenant-1/project-1/run-1/qa-failure-1.webm"]
                    metadata["artifact_urls"] = artifact_urls
                    metadata["checked_artifact_urls"] = artifact_urls
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
                if event in {"RecordingFailureEvidenceCaptured", "FailureEvidenceUploaded"}:
                    metadata = _demo_proof_event_metadata(event)
                    assert metadata is not None
                    artifact_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm"
                    failure_evidence = [
                        {
                            **_failure_evidence_metadata("browser", artifact_url),
                            "release_commit_sha": "e" * 40,
                        }
                    ]
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        **metadata,
                        "artifact_urls": [artifact_url],
                        "failure_evidence_count": 1,
                        "capture_targets": ["browser"],
                        "failure_evidence": failure_evidence,
                    }
                    event_request = replace(event_request, payload=payload)
                if event == "PRFailureEvidenceAttached":
                    artifact_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm"
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        "pr_url": "https://github.com/acme/project-a/pull/8",
                        "artifact_urls": [artifact_url],
                        "artifact_url_check_status": "passed",
                        "checked_artifact_urls": [artifact_url],
                        "pr_body_sha256": "f" * 64,
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
                assert "failure evidence metadata release commit does not match ReleaseLive.release_commit_sha" in str(
                    exc
                )
                assert "browser" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof failure completion to reject mismatched release commit")

    def test_demo_proof_rejects_success_completion_when_uploaded_recordings_do_not_match_completed_recordings(
        self,
    ) -> None:
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
            mismatched_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-uploaded.webm"

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
                    metadata = _demo_proof_event_metadata(event)
                    assert metadata is not None
                    recordings = list(metadata["recordings"])
                    recordings[0] = _recording_artifact_metadata("browser", mismatched_url)
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        **metadata,
                        "artifact_urls": [
                            mismatched_url,
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
                        ],
                        "recordings": recordings,
                    }
                    event_request = replace(event_request, payload=payload)
                if event == "PREvidenceAttached":
                    metadata = _demo_proof_event_metadata(event)
                    assert metadata is not None
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        **metadata,
                        "artifact_urls": [
                            mismatched_url,
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android.webm",
                        ],
                    }
                    event_request = replace(event_request, payload=payload)
                if event == "EvidenceUploaded":
                    try:
                        execute_workflow_advance(
                            session=session,
                            settings=SimpleNamespace(),
                            workflow_type=workflow_type,
                            request=event_request,
                            resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                        )
                    except RuntimeError as exc:
                        assert "EvidenceUploaded.recordings must match RecordingCompleted.recordings" in str(exc)
                        assert "browser" in str(exc)
                    else:  # pragma: no cover
                        raise AssertionError("expected demo proof upload to reject mismatched uploaded recordings")
                    break
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_rejects_failure_completion_when_uploaded_failure_evidence_does_not_match_captured_evidence(
        self,
    ) -> None:
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
            mismatched_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure-uploaded.webm"

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
                    failure_evidence[0] = _failure_evidence_metadata("browser", mismatched_url)
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        **metadata,
                        "artifact_urls": [
                            mismatched_url,
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios-failure.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android-failure.webm",
                        ],
                        "failure_evidence": failure_evidence,
                    }
                    event_request = replace(event_request, payload=payload)
                if event == "PRFailureEvidenceAttached":
                    metadata = _demo_proof_event_metadata(event)
                    assert metadata is not None
                    payload = dict(event_request.payload)
                    payload["event_metadata"] = {
                        **metadata,
                        "artifact_urls": [
                            mismatched_url,
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/ios-failure.webm",
                            "https://cdn.example/qa-demos/tenant-1/project-1/run-1/android-failure.webm",
                        ],
                    }
                    event_request = replace(event_request, payload=payload)
                if event == "FailureEvidenceUploaded":
                    try:
                        execute_workflow_advance(
                            session=session,
                            settings=SimpleNamespace(),
                            workflow_type=workflow_type,
                            request=event_request,
                            resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                        )
                    except RuntimeError as exc:
                        assert (
                            "FailureEvidenceUploaded.failure_evidence must match "
                            "RecordingFailureEvidenceCaptured.failure_evidence"
                        ) in str(exc)
                        assert "browser" in str(exc)
                    else:  # pragma: no cover
                        raise AssertionError("expected demo proof failure upload to reject mismatched failure evidence")
                    break
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=event_request,
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

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
                "ProofLeaseAcquired": _demo_proof_event_metadata("ProofLeaseAcquired"),
                "ReleaseLive": {
                    "release_id": "release-preview-1",
                    "release_kind": "run_preview",
                    "release_status": "live",
                },
                "RecordingCompleted": _demo_proof_event_metadata("RecordingCompleted"),
                "EvidenceUploaded": _demo_proof_event_metadata("EvidenceUploaded"),
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

    def test_demo_proof_failure_evidence_marks_only_evidenced_recording_workflow_targets(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            artifact_url = "https://cdn.example/qa-demos/tenant-1/project-1/run-1/browser-failure.webm"
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(
                    _demo_proof_request_for_event(request, "RecordingFailureEvidenceCaptured"),
                    payload={
                        **request.payload,
                        "event_metadata": {
                            "artifact_urls": [artifact_url],
                            "failure_evidence_count": 1,
                            "capture_targets": ["browser"],
                            "failure_evidence": [
                                _failure_evidence_metadata("browser", artifact_url),
                            ],
                        },
                    },
                ),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            assert description["recording_workflows"] == [
                {"capture_target": "browser", "state": "failed"},
                {"capture_target": "ios", "state": "recording"},
                {"capture_target": "android", "state": "recording"},
            ]

    def test_demo_proof_tracks_deferred_recording_targets_until_next_worker(self) -> None:
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

            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(
                    request,
                    payload={
                        **request.payload,
                        "event_metadata": {
                            "recorded_capture_targets": ["browser"],
                            "remaining_capture_targets": ["ios", "android"],
                        },
                    },
                    trigger=WorkflowTrigger(event="RecordingDeferred"),
                ),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            assert description["demo_proof_state"] == "recording_deferred"
            assert description["recording_workflows"] == [
                {"capture_target": "browser", "state": "recorded"},
                {"capture_target": "ios", "state": "deferred"},
                {"capture_target": "android", "state": "deferred"},
            ]
            metadata_by_event = {
                item["event"]: item["metadata"]
                for item in description["demo_proof_event_metadata"]
            }
            assert metadata_by_event["RecordingDeferred"]["remaining_capture_targets"] == ["ios", "android"]

            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=replace(request, trigger=WorkflowTrigger(event="RecordingStarted")),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            description = json.loads(workflow.source_description or "{}")
            assert description["demo_proof_state"] == "recording"
            assert description["recording_workflows"] == [
                {"capture_target": "browser", "state": "recorded"},
                {"capture_target": "ios", "state": "recording"},
                {"capture_target": "android", "state": "recording"},
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
                request=_demo_proof_request_for_event(request, "ProofLeaseAcquired"),
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

    def test_demo_proof_release_failed_event_waits_for_cleanup_before_blocking_workflow(self) -> None:
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
                request=_demo_proof_request_for_event(request, "ProofLeaseAcquired"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=_demo_proof_request_for_event(request, "ReleaseRequested"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )
            execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=_demo_proof_request_for_event(request, "ReleaseProvisioning"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=_demo_proof_request_for_event(request, "ReleaseFailed"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert result.reason == "release_failed_cleanup_requested"
            assert result.failed is False
            cleanup_result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=_demo_proof_request_for_event(request, "ReleaseFailedPreviewCleanupCompleted"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert cleanup_result.reason == "demo_proof_blocked_after_release_failure_cleanup"
            assert cleanup_result.failed is True
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            release_attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == release_operation.operation_id,
                )
            ).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            assert workflow.status == "failed"
            assert workflow.last_error == (
                "Demo proof recorded release failure cleanup for proof scope run-1-main-abcdef1."
            )
            assert description["demo_proof_state"] == "blocked"
            assert description["demo_proof_events"][-1] == "ReleaseFailedPreviewCleanupCompleted"
            assert release_operation.status == "completed"
            assert cleanup_operation.status == "completed"
            assert release_attempt.status == "completed"

    def test_demo_proof_service_verification_failed_event_attaches_diagnostics_before_cleanup(self) -> None:
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
                request=_demo_proof_request_for_event(request, "ServiceVerificationFailed"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert result.reason == "service_verification_failed_evidence_upload_requested"
            assert result.failed is False
            for event in (
                "FailureEvidenceUploadStarted",
                "FailureEvidenceUploaded",
                "PRFailureEvidenceAttachStarted",
                "PRFailureEvidenceAttached",
                "FailurePreviewCleanupRequested",
                "FailurePreviewCleanupCompleted",
            ):
                cleanup_result = execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            assert cleanup_result.reason == "demo_proof_blocked_with_failure_evidence"
            assert cleanup_result.failed is True
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            release_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "release",
                )
            ).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            assert workflow.status == "failed"
            assert workflow.last_error == (
                "Demo proof recorded failure evidence for proof scope run-1-main-abcdef1."
            )
            assert description["demo_proof_state"] == "blocked"
            assert description["demo_proof_events"][-1] == "FailurePreviewCleanupCompleted"
            assert release_operation.status == "completed"
            assert cleanup_operation.status == "completed"

    def test_demo_proof_pr_attach_failed_event_requests_cleanup_before_blocking_workflow(self) -> None:
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
                request=_demo_proof_request_for_event(request, "PREvidenceAttachFailed"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert result.reason == "pr_evidence_attach_failed_cleanup_requested"
            assert result.failed is False
            cleanup_result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=_demo_proof_request_for_event(request, "PREvidenceAttachFailedPreviewCleanupCompleted"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert cleanup_result.reason == "demo_proof_blocked_after_pr_evidence_attach_failure_cleanup"
            assert cleanup_result.failed is True
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
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
            assert description["demo_proof_events"][-1] == "PREvidenceAttachFailedPreviewCleanupCompleted"
            assert operation.status == "completed"
            assert cleanup_operation.status == "completed"
            assert attempt.status == "completed"

    def test_demo_proof_rejects_pr_attach_failure_completion_when_artifact_links_were_not_checked(self) -> None:
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
                "PREvidenceAttachFailed",
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "PREvidenceAttachFailed":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    metadata.pop("artifact_url_check_status", None)
                    metadata.pop("checked_artifact_urls", None)
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
                    request=_demo_proof_request_for_event(request, "PREvidenceAttachFailedPreviewCleanupCompleted"),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "PREvidenceAttachFailed.artifact_url_check_status" in str(exc)
                assert "PREvidenceAttachFailed.checked_artifact_urls" in str(exc)
            else:  # pragma: no cover
                raise AssertionError("expected demo proof PR attach failure completion to require checked links")

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

    def test_demo_proof_full_lifecycle_completes_declared_work_units(self) -> None:
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
            rows = session.execute(
                select(WorkflowOperation, WorkflowOperationWorkUnit, WorkflowOperationWorkUnitAttempt)
                .join(
                    WorkflowOperationWorkUnit,
                    WorkflowOperationWorkUnit.operation_id == WorkflowOperation.operation_id,
                )
                .join(
                    WorkflowOperationWorkUnitAttempt,
                    WorkflowOperationWorkUnitAttempt.work_unit_id == WorkflowOperationWorkUnit.work_unit_id,
                )
                .where(WorkflowOperation.workflow_id == workflow.workflow_id)
                .order_by(WorkflowOperation.operation_type, WorkflowOperationWorkUnit.unit_key)
            ).all()
            unit_keys_by_operation: dict[str, list[str]] = {}
            unit_statuses_by_operation: dict[str, list[str]] = {}
            attempt_statuses_by_operation: dict[str, list[str]] = {}
            for operation, work_unit, work_unit_attempt in rows:
                unit_keys_by_operation.setdefault(operation.operation_type, []).append(work_unit.unit_key)
                unit_statuses_by_operation.setdefault(operation.operation_type, []).append(work_unit.status)
                attempt_statuses_by_operation.setdefault(operation.operation_type, []).append(work_unit_attempt.status)

            assert unit_keys_by_operation == {
                "preview_lease": ["preview_lease.acquire", "preview_lease.enforce_single_active"],
                "release": ["release.create_or_reuse", "release.wait_for_live"],
                "recording": ["recording.android", "recording.browser", "recording.ios"],
                "evidence_upload": ["evidence_upload.persist"],
                "pr_evidence_update": ["pr_evidence_update.attach_links"],
                "preview_cleanup": ["preview_cleanup.destroy_or_ttl"],
            }
            assert {
                operation_type: sorted(set(statuses))
                for operation_type, statuses in unit_statuses_by_operation.items()
            } == {
                "preview_lease": ["completed"],
                "release": ["completed"],
                "recording": ["completed"],
                "evidence_upload": ["completed"],
                "pr_evidence_update": ["completed"],
                "preview_cleanup": ["completed"],
            }
            assert {
                operation_type: sorted(set(statuses))
                for operation_type, statuses in attempt_statuses_by_operation.items()
            } == {
                "preview_lease": ["completed"],
                "release": ["completed"],
                "recording": ["completed"],
                "evidence_upload": ["completed"],
                "pr_evidence_update": ["completed"],
                "preview_cleanup": ["completed"],
            }

    def test_demo_proof_recording_work_units_include_real_release_context(self) -> None:
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
            recording_attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == recording_operation.operation_id,
                )
            ).scalar_one()
            recording_work_units = session.execute(
                select(WorkflowOperationWorkUnit)
                .where(WorkflowOperationWorkUnit.operation_id == recording_operation.operation_id)
                .order_by(WorkflowOperationWorkUnit.unit_key)
            ).scalars().all()
            service_metadata = _service_verification_metadata()
            expected_payload = {
                "proof_scope_id": "run-1-main-abcdef1",
                "commit_sha": "abcdef1",
                "trigger_mode": "from_run",
                "run_id": "run-1",
                "release_id": "release-preview-1",
                "release_commit_sha": "b" * 40,
                "demo_proof_lease": _demo_proof_lease_metadata(),
                "release_service_urls": service_metadata["service_urls"],
                "pr_url": "https://github.com/acme/project-a/pull/8",
                "required_capture_targets": ["browser", "ios", "android"],
                "required_recording_counts": {"browser": 1, "ios": 1, "android": 1},
                "summary": (
                    "Waiting for recording event for demo proof scope run-1-main-abcdef1 "
                    "across required capture target(s): browser, ios, android."
                ),
            }

            assert [work_unit.unit_key for work_unit in recording_work_units] == [
                "recording.android",
                "recording.browser",
                "recording.ios",
            ]
            assert [
                work_unit.input_fingerprint for work_unit in recording_work_units
            ] == [
                workflow_work_unit_input_fingerprint(
                    {
                        "workflow_id": workflow.workflow_id,
                        "operation_id": recording_operation.operation_id,
                        "operation_type": recording_operation.operation_type,
                        "operation_attempt_id": recording_attempt.attempt_id,
                        "unit_key": work_unit.unit_key,
                        "input": expected_payload,
                    }
                )
                for work_unit in recording_work_units
            ]

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

    def test_demo_proof_failure_pr_attach_failed_event_requests_cleanup_before_blocking_workflow(self) -> None:
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
                request=_demo_proof_request_for_event(request, "PRFailureEvidenceAttachFailed"),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert result.reason == "pr_failure_evidence_attach_failed_cleanup_requested"
            assert result.failed is False
            cleanup_result = execute_workflow_advance(
                session=session,
                settings=SimpleNamespace(),
                workflow_type=workflow_type,
                request=_demo_proof_request_for_event(
                    request,
                    "PRFailureEvidenceAttachFailedPreviewCleanupCompleted",
                ),
                resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
            )

            assert cleanup_result.reason == "demo_proof_blocked_after_pr_failure_evidence_attach_failure_cleanup"
            assert cleanup_result.failed is True
            workflow = session.execute(select(WorkflowExecution)).scalar_one()
            operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "pr_evidence_update",
                )
            ).scalar_one()
            cleanup_operation = session.execute(
                select(WorkflowOperation).where(
                    WorkflowOperation.workflow_id == workflow.workflow_id,
                    WorkflowOperation.operation_type == "preview_cleanup",
                )
            ).scalar_one()
            description = json.loads(workflow.source_description or "{}")
            assert workflow.status == "failed"
            assert description["demo_proof_state"] == "blocked"
            assert description["demo_proof_events"][-1] == "PRFailureEvidenceAttachFailedPreviewCleanupCompleted"
            assert operation.status == "completed"
            assert cleanup_operation.status == "completed"

    def test_demo_proof_rejects_failure_pr_attach_failure_completion_when_artifact_links_were_not_checked(self) -> None:
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
                "PRFailureEvidenceAttachFailed",
            ):
                event_request = _demo_proof_request_for_event(request, event)
                if event == "PRFailureEvidenceAttachFailed":
                    payload = dict(event_request.payload)
                    metadata = dict(payload["event_metadata"])
                    metadata.pop("artifact_url_check_status", None)
                    metadata.pop("checked_artifact_urls", None)
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
                    request=_demo_proof_request_for_event(
                        request,
                        "PRFailureEvidenceAttachFailedPreviewCleanupCompleted",
                    ),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )
            except RuntimeError as exc:
                assert "PRFailureEvidenceAttachFailed.artifact_url_check_status" in str(exc)
                assert "PRFailureEvidenceAttachFailed.checked_artifact_urls" in str(exc)
            else:  # pragma: no cover
                raise AssertionError(
                    "expected demo proof failure PR attach failure completion to require checked links"
                )

    def test_demo_proof_recording_failure_without_evidence_requires_unavailable_reason(self) -> None:
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
            ):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=_demo_proof_request_for_event(request, event),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

            event_request = _demo_proof_request_for_event(request, "RecordingFailed")
            event_payload = dict(event_request.payload)
            event_metadata = dict(event_payload["event_metadata"])
            event_metadata.pop("failure_evidence_unavailable_reason")
            event_payload["event_metadata"] = event_metadata

            with pytest.raises(RuntimeError, match="RecordingFailed.failure_evidence_unavailable_reason"):
                execute_workflow_advance(
                    session=session,
                    settings=SimpleNamespace(),
                    workflow_type=workflow_type,
                    request=replace(event_request, payload=event_payload),
                    resolve_advance_handler_fn=lambda _handler_key: DemoProofWorkflowAdvanceHandler(),
                )

    def test_demo_proof_recording_failure_without_evidence_waits_for_cleanup_before_blocking(self) -> None:
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
                "RecordingFailed",
                "RecordingFailedPreviewCleanupCompleted",
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
                == "Demo proof recorded recording failure cleanup for proof scope run-1-main-abcdef1."
            )
            description = json.loads(workflow.source_description or "{}")
            assert description["demo_proof_state"] == "blocked"
            assert description["demo_proof_events"][-1] == "RecordingFailedPreviewCleanupCompleted"
            operations = session.execute(
                select(WorkflowOperation).where(WorkflowOperation.workflow_id == workflow.workflow_id)
            ).scalars().all()
            status_by_type = {operation.operation_type: operation.status for operation in operations}
            assert status_by_type == {
                "preview_lease": "completed",
                "release": "completed",
                "recording": "completed",
                "evidence_upload": "pending",
                "pr_evidence_update": "pending",
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
                request=_demo_proof_request_for_event(request, "ProofLeaseAcquired"),
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
            retried_work_unit_rows = session.execute(
                select(WorkflowOperationWorkUnit, WorkflowOperationWorkUnitAttempt)
                .join(
                    WorkflowOperationWorkUnitAttempt,
                    WorkflowOperationWorkUnitAttempt.work_unit_id == WorkflowOperationWorkUnit.work_unit_id,
                )
                .where(
                    WorkflowOperationWorkUnit.operation_id == release_operation.operation_id,
                    WorkflowOperationWorkUnit.parent_attempt_id == attempts[-1].attempt_id,
                    WorkflowOperationWorkUnitAttempt.operation_attempt_id == attempts[-1].attempt_id,
                )
                .order_by(WorkflowOperationWorkUnit.unit_key)
            ).all()
            assert handle.operation_type == "release"
            assert handle.status == "waiting_for_input"
            assert workflow.status == "waiting_for_input"
            assert release_operation.status == "waiting_for_input"
            assert [attempt.status for attempt in attempts] == ["failed", "waiting_for_input"]
            assert [work_unit.unit_key for work_unit, _attempt in retried_work_unit_rows] == [
                "release.create_or_reuse",
                "release.wait_for_live",
            ]
            assert {work_unit.status for work_unit, _attempt in retried_work_unit_rows} == {"running"}
            assert {attempt.status for _work_unit, attempt in retried_work_unit_rows} == {"running"}
            expected_payload = {
                "proof_scope_id": "run-1-main-abcdef1",
                "commit_sha": "abcdef1",
                "trigger_mode": "from_run",
                "run_id": "run-1",
                "release_id": "release-preview-1",
                "release_commit_sha": "b" * 40,
                "demo_proof_lease": _demo_proof_lease_metadata(),
                "pr_url": "https://github.com/acme/project-a/pull/8",
                "required_capture_targets": ["browser", "ios", "android"],
                "required_recording_counts": {"browser": 1, "ios": 1, "android": 1},
                "summary": "Retry demo proof operation release for proof scope run-1-main-abcdef1.",
            }
            assert [
                work_unit.input_fingerprint for work_unit, _attempt in retried_work_unit_rows
            ] == [
                workflow_work_unit_input_fingerprint(
                    {
                        "workflow_id": workflow.workflow_id,
                        "operation_id": release_operation.operation_id,
                        "operation_type": release_operation.operation_type,
                        "operation_attempt_id": attempts[-1].attempt_id,
                        "unit_key": work_unit.unit_key,
                        "input": expected_payload,
                    }
                )
                for work_unit, _attempt in retried_work_unit_rows
            ]
