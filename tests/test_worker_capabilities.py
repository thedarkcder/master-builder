from orchestrator.core.worker_capabilities import (
    infer_required_worker_capability,
    parse_worker_capabilities,
    required_worker_capability_for_run,
    worker_label_for_capability,
)


def test_parse_worker_capabilities_defaults_and_normalizes() -> None:
    assert parse_worker_capabilities("") == {"linux"}
    assert parse_worker_capabilities("linux,mac,invalid") == {"linux", "macos"}


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
    run = type(
        "RunStub",
        (),
        {
            "plan": {"required_worker_capability": "mac"},
            "issue_summary": "Build backend",
            "issue_description": "",
        },
    )()
    assert required_worker_capability_for_run(run) == "macos"


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
