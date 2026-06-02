from __future__ import annotations

from unittest.mock import patch

from scripts.qa_demo_mobile_recorder import _combined_recording_failure
from scripts.qa_demo_mobile_recorder import _reboot_simulator


def test_combined_recording_failure_prefers_primary_error() -> None:
    primary = RuntimeError("xcodebuild failed")

    combined = _combined_recording_failure(primary_error=primary, stop_error=None)

    assert combined is primary


def test_combined_recording_failure_keeps_shutdown_error_when_primary_missing() -> None:
    stop_error = RuntimeError("recordVideo failed")

    combined = _combined_recording_failure(primary_error=None, stop_error=stop_error)

    assert combined is stop_error


def test_combined_recording_failure_surfaces_both_errors() -> None:
    combined = _combined_recording_failure(
        primary_error=RuntimeError("xcodebuild failed"),
        stop_error=RuntimeError("recordVideo failed"),
    )

    assert isinstance(combined, RuntimeError)
    assert "xcodebuild failed" in str(combined)
    assert "recordVideo failed" in str(combined)


def test_reboot_simulator_shuts_down_then_boots() -> None:
    calls: list[list[str]] = []

    def _record_run(args, **kwargs):  # noqa: ANN001
        calls.append(list(args))
        return None

    with (
        patch("scripts.qa_demo_mobile_recorder.subprocess.run", side_effect=_record_run),
        patch("scripts.qa_demo_mobile_recorder._ensure_simulator_booted") as ensure_mock,
    ):
        _reboot_simulator("SIM-123")

    assert calls == [["xcrun", "simctl", "shutdown", "SIM-123"]]
    ensure_mock.assert_called_once_with("SIM-123")
