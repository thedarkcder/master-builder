from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from orchestrator.core.workflow.checkpoints import upsert_workflow_checkpoint


def test_upsert_workflow_checkpoint_rejects_create_without_payload() -> None:
    session = MagicMock()
    with patch(
        "orchestrator.core.workflow.checkpoints.load_checkpoint", return_value=None
    ):
        try:
            upsert_workflow_checkpoint(
                session,
                workflow_id="workflow-1",
                run_id="run-1",
                checkpoint_kind="pm",
                stage="pm",
                payload=None,
            )
        except ValueError as exc:
            assert "payload is required when creating a workflow checkpoint" in str(exc)
        else:
            raise AssertionError(
                "Expected ValueError when creating checkpoint without payload"
            )

    session.add.assert_not_called()


def test_upsert_workflow_checkpoint_preserves_existing_payload_when_updating_session_only() -> (
    None
):
    session = MagicMock()
    workflow = SimpleNamespace(latest_checkpoint_id=None, updated_at=None)
    existing_checkpoint = SimpleNamespace(
        checkpoint_id="run-1-pm",
        workflow_id="workflow-1",
        run_id="run-1",
        checkpoint_kind="pm",
        stage="pm",
        payload_json={
            "version": 1,
            "context": {},
            "workflow": {},
            "events": {},
            "stages": {},
        },
        codex_session_id=None,
        updated_at=None,
    )
    session.get.return_value = workflow
    with patch(
        "orchestrator.core.workflow.checkpoints.load_checkpoint",
        return_value=existing_checkpoint,
    ):
        checkpoint = upsert_workflow_checkpoint(
            session,
            workflow_id="workflow-1",
            run_id="run-1",
            checkpoint_kind="pm",
            stage="pm",
            payload=None,
            codex_session_id="sess-1",
        )

    assert checkpoint is existing_checkpoint
    assert existing_checkpoint.payload_json == {
        "version": 1,
        "context": {},
        "workflow": {},
        "events": {},
        "stages": {},
    }
    assert existing_checkpoint.codex_session_id == "sess-1"
    assert workflow.latest_checkpoint_id == "run-1-pm"
