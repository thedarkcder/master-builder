from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select

from orchestrator.core.qa.demo_proof_handlers import DemoProofWorkflowAdvanceHandler
from orchestrator.core.workflow.advance import WorkflowAdvanceRequest, execute_workflow_advance
from orchestrator.core.workflow.execution_projection import WorkflowExecutionReference, WorkflowSourceReference
from orchestrator.core.workflow.type_catalog import get_workflow_type
from orchestrator.storage.db import create_session_factory
from orchestrator.storage.models import Project, Tenant, WorkflowExecution, WorkflowOperation, WorkflowOperationAttempt
from tests.test_support.db_harness import SqliteTemplateDbTestCase


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
                    WorkflowOperation.idempotency_key == "demo-proof:run-1-main-abcdef1:preview-lease",
                )
            ).scalar_one()
            assert operation.status == "waiting_for_input"
            assert operation.run_id == "run-1"
            assert operation.target_ref == "run-1-main-abcdef1"
            attempt = session.execute(
                select(WorkflowOperationAttempt).where(
                    WorkflowOperationAttempt.operation_id == operation.operation_id,
                )
            ).scalar_one()
            assert attempt.status == "waiting_for_input"
