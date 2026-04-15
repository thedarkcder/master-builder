from types import SimpleNamespace

from orchestrator.core.communications.enqueue_conflict_presentation import (
    present_discord_enqueue_conflict,
)
from orchestrator.core.run_enqueue_types import EnqueueFailureReason


def test_present_discord_enqueue_conflict_includes_active_run_context() -> None:
    presentation = present_discord_enqueue_conflict(
        prefix="Run could not be queued",
        reason=EnqueueFailureReason.RUN_ALREADY_ACTIVE,
        enqueue_run_obj=SimpleNamespace(run_id="run-1", status="queued"),
    )

    assert "run_already_active" in presentation.detail
    assert "run-1" in presentation.detail
    assert "already active" in presentation.detail.lower()


def test_present_discord_enqueue_conflict_uses_typed_reason_guidance() -> None:
    presentation = present_discord_enqueue_conflict(
        prefix="Run could not be queued",
        reason=EnqueueFailureReason.TENANT_CONCURRENCY_LIMIT_REACHED,
        enqueue_run_obj=None,
    )

    assert "tenant_concurrency_limit_reached" in presentation.detail
    assert "concurrency limit" in presentation.detail.lower()


def test_enqueue_failure_reason_owns_guidance() -> None:
    assert "already active" in EnqueueFailureReason.RUN_ALREADY_ACTIVE.guidance.lower()
