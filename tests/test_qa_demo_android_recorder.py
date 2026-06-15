from __future__ import annotations

import subprocess
from pathlib import Path

from scripts.qa_demo_android_recorder import dump_ui_elements
from scripts.qa_demo_android_recorder import build_debug_apk
from scripts.qa_demo_android_recorder import discover_android_project_dir
from scripts.qa_demo_android_recorder import execute_scenario
from scripts.qa_demo_android_recorder import find_element
from scripts.qa_demo_android_recorder import launch_app
from scripts.qa_demo_android_recorder import parse_bounds
from scripts.qa_demo_android_recorder import preferred_adb_device
from scripts.qa_demo_android_recorder import qa_demo_launch_extras
from scripts.qa_demo_android_recorder import record_live_screen_demo
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


def test_execute_scenario_relaunches_android_app_with_release_context_extras(monkeypatch) -> None:
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
            objective="Prove release context survives relaunch",
            capture_target="android",
            steps=[QaStep(action="relaunch_app", value="preserve")],
        ),
        launch_extras={"QA_DEMO_API_BASE_URL": "https://api.preview.example"},
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
            "--es",
            "QA_DEMO_API_BASE_URL",
            "https://api.preview.example",
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


def test_launch_app_injects_release_context_extras(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        calls.append(list(args))

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr("scripts.qa_demo_android_recorder._run", _fake_run)

    launch_app(
        device_id="device-1",
        launch_activity="com.example.app/.MainActivity",
        launch_extras={
            "QA_DEMO_API_BASE_URL": "https://api.preview.example",
            "MB_QA_DEMO_RELEASE_COMMIT_SHA": "b" * 40,
        },
    )

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
            "--es",
            "MB_QA_DEMO_RELEASE_COMMIT_SHA",
            "b" * 40,
            "--es",
            "QA_DEMO_API_BASE_URL",
            "https://api.preview.example",
        ]
    ]


def test_qa_demo_launch_extras_maps_release_context() -> None:
    launch_extras = qa_demo_launch_extras(
        {
            "release_commit_sha": "b" * 40,
            "release_api_base_url": "https://api.preview.example",
            "release_browser_url": "https://web.preview.example",
            "release_service_urls": [
                {"service_kind": "api", "url": "https://api.preview.example"},
            ],
        }
    )

    assert launch_extras["MB_QA_DEMO_RELEASE_COMMIT_SHA"] == "b" * 40
    assert launch_extras["MB_QA_DEMO_RELEASE_API_BASE_URL"] == "https://api.preview.example"
    assert launch_extras["QA_DEMO_API_BASE_URL"] == "https://api.preview.example"
    assert launch_extras["QA_DEMO_BROWSER_URL"] == "https://web.preview.example"
    assert (
        launch_extras["MB_QA_DEMO_RELEASE_SERVICE_URLS_JSON"]
        == '[{"service_kind":"api","url":"https://api.preview.example"}]'
    )


def test_build_debug_apk_discovers_android_app_in_monorepo_subdirectory(monkeypatch, tmp_path) -> None:
    android_dir = tmp_path / "apps" / "android"
    android_dir.mkdir(parents=True)
    (android_dir / "gradlew").write_text("#!/bin/sh\n", encoding="utf-8")
    (android_dir / "settings.gradle").write_text("pluginManagement {}\n", encoding="utf-8")
    (android_dir / "build.gradle").write_text("plugins { id 'com.android.application' }\n", encoding="utf-8")
    built_apk = android_dir / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
    calls: list[tuple[list[str], Path | None]] = []

    def _fake_run(args, *, cwd=None, **_kwargs):  # noqa: ANN001
        calls.append((list(args), cwd))
        built_apk.parent.mkdir(parents=True)
        built_apk.write_bytes(b"apk")

        class _Result:
            stdout = ""

        return _Result()

    monkeypatch.setattr("scripts.qa_demo_android_recorder._run", _fake_run)

    assert build_debug_apk(repo_dir=tmp_path) == built_apk
    assert calls == [([str(android_dir / "gradlew"), "assembleDebug"], android_dir)]


def test_discover_android_project_dir_honors_relative_override(monkeypatch, tmp_path) -> None:
    android_dir = tmp_path / "mobile" / "android-app"
    android_dir.mkdir(parents=True)
    monkeypatch.setenv("QA_DEMO_ANDROID_PROJECT_DIR", "mobile/android-app")

    assert discover_android_project_dir(repo_dir=tmp_path) == android_dir.resolve()


def test_discover_android_project_dir_prefers_payload_source_paths(tmp_path) -> None:
    android_dir = tmp_path / "clients" / "android"
    android_dir.mkdir(parents=True)
    (android_dir / "build.gradle").write_text("plugins { id 'com.android.application' }\n", encoding="utf-8")
    other_dir = tmp_path / "samples" / "android"
    other_dir.mkdir(parents=True)
    (other_dir / "build.gradle").write_text("plugins { id 'com.android.application' }\n", encoding="utf-8")

    assert discover_android_project_dir(repo_dir=tmp_path, target_source_paths=["clients/android"]) == android_dir.resolve()


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


def test_record_live_screen_demo_uses_android_screenrecord_around_scenario(monkeypatch, tmp_path) -> None:
    calls: list[list[str]] = []
    recorder_running = False

    class _Recorder:
        returncode = None

        def __init__(self, args, **_kwargs):  # noqa: ANN001
            nonlocal recorder_running
            calls.append(list(args))
            recorder_running = True

        def poll(self):  # noqa: ANN201
            return self.returncode

        def send_signal(self, signal_value):  # noqa: ANN001
            calls.append(["send_signal", str(signal_value)])

        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            nonlocal recorder_running
            calls.append(["wait", str(timeout)])
            recorder_running = False
            self.returncode = 0
            return 0

    def _fake_run(args, **_kwargs):  # noqa: ANN001
        calls.append(list(args))
        if args[:4] == ["adb", "-s", "device-1", "pull"]:
            Path(args[-1]).write_bytes(b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x08moov")

        class _Result:
            stdout = ""

        return _Result()

    def _fake_execute_scenario(**_kwargs):  # noqa: ANN001
        assert recorder_running is True
        calls.append(["execute_scenario"])

    monkeypatch.setattr("scripts.qa_demo_android_recorder._run", _fake_run)
    monkeypatch.setattr("scripts.qa_demo_android_recorder.subprocess.Popen", _Recorder)
    monkeypatch.setattr("scripts.qa_demo_android_recorder.execute_scenario", _fake_execute_scenario)

    output_path = tmp_path / "demo.mp4"
    record_live_screen_demo(
        device_id="device-1",
        package_name="com.example.app",
        launch_activity="com.example.app/.MainActivity",
        scenario=QaScenario(
            name="Live Android walkthrough",
            objective="Prove live device recording",
            capture_target="android",
            steps=[QaStep(action="goto")],
        ),
        output_path=output_path,
    )

    assert calls[0][:5] == ["adb", "-s", "device-1", "shell", "rm"]
    assert calls[1][:5] == ["adb", "-s", "device-1", "shell", "screenrecord"]
    assert calls[2] == ["execute_scenario"]
    assert calls[3][0] == "send_signal"
    assert calls[5][:5] == ["adb", "-s", "device-1", "pull", "/sdcard/Download/master-builder-qa-demo-live-android-walkthrough.mp4"]
    assert not any("screencap" in " ".join(call) for call in calls)
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
