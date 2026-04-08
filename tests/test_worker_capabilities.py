from orchestrator.core.worker_capability_normalization import WorkerCapability
from orchestrator.core.worker_capabilities import (
    infer_required_worker_capability,
    parse_worker_capabilities,
    parse_worker_capabilities_strict,
    parse_worker_capability_labels,
    required_worker_capability_for_run,
    worker_label_for_capability,
)
from orchestrator.core.workflow.execution_snapshot import ExecutionSnapshot


def test_parse_worker_capabilities_defaults_and_requires_canonical_values() -> None:
    assert parse_worker_capabilities("") == {WorkerCapability.LINUX}
    assert parse_worker_capabilities("linux,macos,invalid") == {WorkerCapability.LINUX, WorkerCapability.MACOS}
    assert parse_worker_capabilities("linux,mac") == {WorkerCapability.LINUX}


def test_parse_worker_capabilities_strict_rejects_invalid_tokens() -> None:
    try:
        parse_worker_capabilities_strict("linux,invalid", source="ORCHESTRATOR_WORKER_CAPABILITIES")
        raise AssertionError("expected strict parser to reject invalid token")
    except ValueError as exc:
        assert "Invalid worker capability token(s)" in str(exc)


def test_parse_worker_capability_labels_reports_invalid_labels() -> None:
    parsed = parse_worker_capability_labels(["worker:darwin", "worker:macos"])
    assert parsed.selected_capability == WorkerCapability.MACOS
    assert parsed.invalid_labels == ("worker:darwin",)
    assert parsed.conflicting_capabilities == ()


def test_parse_worker_capability_labels_reports_conflicting_labels() -> None:
    parsed = parse_worker_capability_labels(["worker:linux", "worker:macos"])
    assert parsed.selected_capability is None
    assert parsed.invalid_labels == ()
    assert parsed.conflicting_capabilities == (WorkerCapability.LINUX, WorkerCapability.MACOS)


def test_parse_worker_capability_labels_collects_all_invalid_labels() -> None:
    parsed = parse_worker_capability_labels(["worker:darwin", "worker:unix"])
    assert parsed.selected_capability is None
    assert parsed.invalid_labels == ("worker:darwin", "worker:unix")


def test_infer_required_worker_capability_prefers_explicit_label() -> None:
    inferred = infer_required_worker_capability(
        issue_summary="Build backend",
        issue_description="No mobile code",
        issue_labels=["worker:macos"],
    )
    assert inferred == "macos"


def test_infer_required_worker_capability_defaults_to_linux_without_label() -> None:
    inferred = infer_required_worker_capability(
        issue_summary="Implement SwiftUI onboarding",
        issue_description="Use Xcode and XCTest",
        issue_labels=[],
    )
    assert inferred == "linux"
    assert worker_label_for_capability(inferred) == "worker:linux"


def test_required_worker_capability_for_run_prefers_plan() -> None:
    snapshot = ExecutionSnapshot.empty()
    snapshot.workflow.outcome = "requeue"
    snapshot.workflow.requeue_target = "macos"
    snapshot.workflow.requeue_reason = "Requires macOS worker"
    run = type(
        "RunStub",
        (),
        {
            "plan": snapshot.dump(),
            "issue_summary": "Build backend",
            "issue_description": "",
        },
    )()
    assert required_worker_capability_for_run(run) == WorkerCapability.MACOS


def test_required_worker_capability_for_run_is_none_without_plan_requirement() -> None:
    run = type(
        "RunStub",
        (),
        {
            "plan": {},
            "issue_summary": "Build backend",
            "issue_description": "",
        },
    )()
    assert required_worker_capability_for_run(run) is None
