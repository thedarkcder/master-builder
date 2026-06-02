from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.qa_demo_android_recorder import capture_screen_frame
from scripts.qa_demo_android_recorder import dump_ui_elements
from scripts.qa_demo_android_recorder import encode_frames_to_mp4
from scripts.qa_demo_android_recorder import execute_scenario
from scripts.qa_demo_android_recorder import find_element
from scripts.qa_demo_android_recorder import launch_app
from scripts.qa_demo_android_recorder import parse_bounds
from scripts.qa_demo_android_recorder import preferred_adb_device
from scripts.qa_demo_android_recorder import resolve_launch_activity
from scripts.qa_demo_android_recorder import validate_mp4_recording
from scripts.qa_demo_android_recorder import _run
from orchestrator.core.workflow.runner import QaScenario, QaStep


def test_preferred_adb_device_chooses_first_ready_device() -> None:
    output = """List of devices attached
emulator-5554	device
emulator-5556	offline
device-1	device
"""

    assert preferred_adb_device(output) == "emulator-5554"


def test_parse_bounds_returns_tappable_rectangle() -> None:
    assert parse_bounds("[10,20][110,220]") == (10, 20, 110, 220)
    assert parse_bounds("invalid") is None


def test_find_element_matches_text_and_short_resource_id(monkeypatch) -> None:
    xml_payload = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?>
<hierarchy>
  <node text="Start" resource-id="com.example:id/start_button" content-desc="Start demo" bounds="[10,20][110,120]" />
</hierarchy>
"""

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        class _Result:
            stdout = xml_payload if args[:4] == ["adb", "-s", "device-1", "exec-out"] else ""

        return _Result()

    monkeypatch.setattr("scripts.qa_demo_android_recorder._run", _fake_run)

    by_text = find_element(device_id="device-1", selector="text=Start")
    by_id = find_element(device_id="device-1", selector="id=start_button")
    by_description = find_element(device_id="device-1", selector="text=Start demo")

    assert by_text.center == (60, 70)
    assert by_id.center == (60, 70)
    assert by_description.center == (60, 70)
    assert dump_ui_elements(device_id="device-1")[0].resource_id == "com.example:id/start_button"


def test_execute_scenario_relaunches_android_app_with_existing_state(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        calls.append(list(args))

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr("scripts.qa_demo_android_recorder._run", _fake_run)

    execute_scenario(
        device_id="device-1",
        package_name="com.example.app",
        launch_activity="com.example.app/.MainActivity",
        scenario=QaScenario(
            name="Relaunch",
            objective="Prove returning state",
            capture_target="android",
            steps=[QaStep(action="relaunch_app", value="preserve")],
        ),
    )

    assert calls == [
        ["adb", "-s", "device-1", "shell", "am", "force-stop", "com.example.app"],
        [
            "adb",
            "-s",
            "device-1",
            "shell",
            "am",
            "start",
            "-n",
            "com.example.app/.MainActivity",
        ],
    ]


def test_resolve_launch_activity_uses_package_manager_brief_output(monkeypatch) -> None:
    def _fake_run(_args, **_kwargs):  # noqa: ANN001
        class _Result:
            stdout = (
                "priority=0 preferredOrder=0 match=0x108000 specificIndex=-1 isDefault=false\n"
                "com.example.app/.MainActivity\n"
            )

        return _Result()

    monkeypatch.setattr("scripts.qa_demo_android_recorder._run", _fake_run)

    assert resolve_launch_activity(device_id="device-1", package_name="com.example.app") == (
        "com.example.app/.MainActivity"
    )


def test_launch_app_uses_resolved_activity(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        calls.append(list(args))

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr("scripts.qa_demo_android_recorder._run", _fake_run)

    launch_app(device_id="device-1", launch_activity="com.example.app/.MainActivity")

    assert calls == [
        [
            "adb",
            "-s",
            "device-1",
            "shell",
            "am",
            "start",
            "-n",
            "com.example.app/.MainActivity",
        ]
    ]


def test_run_bounds_adb_commands_with_actionable_timeout(monkeypatch) -> None:
    def _fake_run(*_args, **_kwargs):  # noqa: ANN001
        raise subprocess.TimeoutExpired(cmd=["adb", "shell", "uiautomator"], timeout=60, output="", stderr="")

    monkeypatch.setattr("scripts.qa_demo_android_recorder.subprocess.run", _fake_run)

    try:
        _run(["adb", "shell", "uiautomator"], capture_output=True)
    except RuntimeError as exc:
        assert "timed out after 60 seconds" in str(exc)
        assert "adb shell uiautomator" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected timeout")


def test_capture_screen_frame_writes_device_screencap(monkeypatch, tmp_path) -> None:
    def _fake_run_binary(args, **_kwargs):  # noqa: ANN001
        assert args == ["adb", "-s", "device-1", "exec-out", "screencap", "-p"]

        class _Result:
            stdout = b"\x89PNG\r\n\x1a\n" + (b"real-screen" * 128)

        return _Result()

    monkeypatch.setattr("scripts.qa_demo_android_recorder._run_binary", _fake_run_binary)

    frame_path = tmp_path / "0000.png"
    capture_screen_frame(device_id="device-1", frame_path=frame_path)

    assert frame_path.read_bytes() == b"\x89PNG\r\n\x1a\n" + (b"real-screen" * 128)


def test_encode_frames_to_mp4_uses_ffmpeg_without_screenrecord(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        calls.append(list(args))
        Path(args[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x08moov")

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr("scripts.qa_demo_android_recorder._run", _fake_run)

    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    (frame_dir / "0000.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    output_path = tmp_path / "demo.mp4"
    encode_frames_to_mp4(frame_dir=frame_dir, output_path=output_path)

    assert calls[0][:7] == ["ffmpeg", "-y", "-framerate", "1", "-pattern_type", "glob", "-i"]
    assert "screenrecord" not in " ".join(calls[0])
    assert output_path.exists()


def test_validate_mp4_recording_rejects_incomplete_mp4(tmp_path) -> None:
    invalid_video = tmp_path / "invalid.mp4"
    invalid_video.write_bytes(b"\x00\x00\x00\x18ftypmp42incomplete")

    try:
        validate_mp4_recording(invalid_video)
    except RuntimeError as exc:
        assert "missing moov atom" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected invalid mp4 failure")
