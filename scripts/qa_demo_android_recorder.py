#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from orchestrator.core.workflow.checkpoint_codec import decode_qa_result_payload  # noqa: E402
from orchestrator.core.workflow.runner import QaScenario, QaStep  # noqa: E402

DEFAULT_ADB_COMMAND_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class AndroidElement:
    text: str
    resource_id: str
    content_description: str
    bounds: tuple[int, int, int, int]

    @property
    def center(self) -> tuple[int, int]:
        left, top, right, bottom = self.bounds
        return ((left + right) // 2, (top + bottom) // 2)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        raise RuntimeError("usage: qa_demo_android_recorder.py <input.json> <output.json>")

    input_path = Path(argv[1]).resolve()
    output_path = Path(argv[2]).resolve()
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    repo_dir = Path(str(payload.get("execution_repo_dir") or "").strip()).resolve()
    if not repo_dir.exists():
        raise RuntimeError(f"execution_repo_dir does not exist: {repo_dir}")

    qa_result = decode_qa_result_payload(
        {
            "summary": ["android qa recorder"],
            "scenarios": payload.get("scenarios"),
            "recordings": [],
            "outcome": "continue",
        }
    )
    if qa_result is None or not qa_result.scenarios:
        raise RuntimeError("android qa recorder requires at least one valid scenario")

    output_dir = Path(str(payload.get("output_dir") or "").strip()).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    device_id = str(os.environ.get("QA_DEMO_ANDROID_DEVICE_ID") or "").strip() or preferred_adb_device(
        _run(["adb", "devices"], capture_output=True).stdout
    )
    apk_path = Path(str(os.environ.get("QA_DEMO_ANDROID_APK") or "").strip() or build_debug_apk(repo_dir=repo_dir))
    package_name = str(os.environ.get("QA_DEMO_ANDROID_PACKAGE") or "").strip() or resolve_package_name(apk_path=apk_path)
    install_apk(device_id=device_id, apk_path=apk_path)
    launch_activity = resolve_launch_activity(device_id=device_id, package_name=package_name)

    recordings: list[dict[str, str]] = []
    for scenario in qa_result.scenarios:
        sanitized_name = sanitize_recording_name(scenario.name)
        local_video_path = output_dir / f"{sanitized_name}.mp4"
        reset_app_state(device_id=device_id, package_name=package_name)
        launch_app(device_id=device_id, launch_activity=launch_activity)
        record_live_screen_demo(
            device_id=device_id,
            package_name=package_name,
            launch_activity=launch_activity,
            scenario=scenario,
            output_path=local_video_path,
        )
        recordings.append({"name": scenario.name, "path": str(local_video_path)})

    output_path.write_text(json.dumps({"recordings": recordings}, indent=2), encoding="utf-8")
    return 0


def preferred_adb_device(adb_devices_output: str) -> str:
    devices: list[str] = []
    for raw_line in adb_devices_output.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    if not devices:
        raise RuntimeError("No available Android emulator/device found via adb devices")
    return devices[0]


def build_debug_apk(*, repo_dir: Path) -> Path:
    android_project_dir = discover_android_project_dir(repo_dir=repo_dir)
    gradle = android_project_dir / "gradlew"
    command = [str(gradle), "assembleDebug"] if gradle.exists() else ["gradle", "assembleDebug"]
    _run(command, cwd=android_project_dir, capture_output=True)
    candidates = sorted(android_project_dir.glob("**/build/outputs/apk/**/*debug*.apk"))
    if not candidates:
        raise RuntimeError(f"No debug APK found under {android_project_dir}")
    return candidates[-1]


def discover_android_project_dir(*, repo_dir: Path) -> Path:
    configured = str(os.environ.get("QA_DEMO_ANDROID_PROJECT_DIR") or "").strip()
    if configured:
        configured_path = Path(configured)
        android_project_dir = configured_path if configured_path.is_absolute() else repo_dir / configured_path
        if not android_project_dir.exists():
            raise RuntimeError(f"Configured Android project directory does not exist: {android_project_dir}")
        return android_project_dir.resolve()
    candidates: list[Path] = []
    for build_file in [*repo_dir.rglob("build.gradle"), *repo_dir.rglob("build.gradle.kts")]:
        try:
            source = build_file.read_text(encoding="utf-8")
        except OSError:
            continue
        if _is_android_application_gradle_source(source):
            candidates.append(build_file.parent)
    if candidates:
        return sorted(candidates, key=lambda path: (len(path.relative_to(repo_dir).parts), str(path)))[0]
    if (repo_dir / "gradlew").exists() or (repo_dir / "settings.gradle").exists() or (repo_dir / "settings.gradle.kts").exists():
        return repo_dir
    raise RuntimeError(f"No Android Gradle application project found under {repo_dir}")


def _is_android_application_gradle_source(source: str) -> bool:
    return "com.android.application" in source


def resolve_package_name(*, apk_path: Path) -> str:
    aapt = os.environ.get("QA_DEMO_ANDROID_AAPT") or "aapt"
    result = _run([aapt, "dump", "badging", str(apk_path)], capture_output=True)
    match = re.search(r"package: name='([^']+)'", result.stdout)
    if match is None:
        raise RuntimeError(f"Unable to resolve Android package name from {apk_path}")
    return match.group(1)


def install_apk(*, device_id: str, apk_path: Path) -> None:
    _run(["adb", "-s", device_id, "install", "-r", str(apk_path)], capture_output=True)


def resolve_launch_activity(*, device_id: str, package_name: str) -> str:
    result = _run(
        ["adb", "-s", device_id, "shell", "cmd", "package", "resolve-activity", "--brief", package_name],
        capture_output=True,
    )
    for raw_line in reversed(result.stdout.splitlines()):
        line = raw_line.strip()
        if "/" in line and not line.startswith("priority="):
            return line
    raise RuntimeError(f"Unable to resolve Android launch activity for package {package_name}")


def reset_app_state(*, device_id: str, package_name: str) -> None:
    _run(["adb", "-s", device_id, "shell", "pm", "clear", package_name], capture_output=True, check=False)


def launch_app(*, device_id: str, launch_activity: str) -> None:
    _run(
        ["adb", "-s", device_id, "shell", "am", "start", "-n", launch_activity],
        capture_output=True,
    )


def execute_scenario(*, device_id: str, package_name: str, launch_activity: str, scenario: QaScenario) -> None:
    for step in scenario.steps:
        execute_step(device_id=device_id, package_name=package_name, launch_activity=launch_activity, step=step)


def record_live_screen_demo(
    *,
    device_id: str,
    package_name: str,
    launch_activity: str,
    scenario: QaScenario,
    output_path: Path,
) -> None:
    remote_path = f"/sdcard/Download/master-builder-qa-demo-{sanitize_recording_name(scenario.name)}.mp4"
    _run(["adb", "-s", device_id, "shell", "rm", "-f", remote_path], capture_output=True, check=False)
    recorder = subprocess.Popen(
        ["adb", "-s", device_id, "shell", "screenrecord", remote_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    primary_error: Exception | None = None
    stop_error: Exception | None = None
    try:
        execute_scenario(
            device_id=device_id,
            package_name=package_name,
            launch_activity=launch_activity,
            scenario=scenario,
        )
    except Exception as exc:  # noqa: BLE001
        primary_error = exc
    finally:
        try:
            stop_screenrecord(recorder)
        except Exception as exc:  # noqa: BLE001
            stop_error = exc
    combined_error = combined_recording_failure(primary_error=primary_error, stop_error=stop_error)
    if combined_error is not None:
        raise combined_error
    _run(["adb", "-s", device_id, "pull", remote_path, str(output_path)], capture_output=True)
    _run(["adb", "-s", device_id, "shell", "rm", "-f", remote_path], capture_output=True, check=False)
    if not output_path.exists():
        raise RuntimeError(f"Expected Android QA recording missing: {output_path}")
    validate_mp4_recording(output_path)


def execute_step(*, device_id: str, package_name: str, launch_activity: str, step: QaStep) -> None:
    if step.action == "goto":
        return
    if step.action == "relaunch_app":
        _run(["adb", "-s", device_id, "shell", "am", "force-stop", package_name], capture_output=True)
        launch_app(device_id=device_id, launch_activity=launch_activity)
        return
    if step.action == "click":
        element = find_element(device_id=device_id, selector=require_selector(step))
        x, y = element.center
        _run(["adb", "-s", device_id, "shell", "input", "tap", str(x), str(y)], capture_output=True)
        return
    if step.action == "fill":
        element = find_element(device_id=device_id, selector=require_selector(step))
        x, y = element.center
        _run(["adb", "-s", device_id, "shell", "input", "tap", str(x), str(y)], capture_output=True)
        text = str(step.value or "").replace(" ", "%s")
        _run(["adb", "-s", device_id, "shell", "input", "text", text], capture_output=True)
        return
    if step.action == "press":
        press_value = str(step.value or "").strip().lower()
        key_code = {
            "enter": "KEYCODE_ENTER",
            "tab": "KEYCODE_TAB",
            "back": "KEYCODE_BACK",
            "escape": "KEYCODE_BACK",
            "arrowleft": "KEYCODE_DPAD_LEFT",
            "left": "KEYCODE_DPAD_LEFT",
            "arrowright": "KEYCODE_DPAD_RIGHT",
            "right": "KEYCODE_DPAD_RIGHT",
            "arrowup": "KEYCODE_DPAD_UP",
            "up": "KEYCODE_DPAD_UP",
            "arrowdown": "KEYCODE_DPAD_DOWN",
            "down": "KEYCODE_DPAD_DOWN",
        }.get(press_value)
        if key_code is None:
            raise RuntimeError(f"Unsupported Android press value: {step.value}")
        _run(["adb", "-s", device_id, "shell", "input", "keyevent", key_code], capture_output=True)
        return
    if step.action in {"assert_visible", "assert_text"}:
        element = find_element(device_id=device_id, selector=require_selector(step))
        if step.action == "assert_text":
            expected = require_value(step)
            if expected not in element.text:
                raise RuntimeError(f"Expected {expected!r} in Android element text {element.text!r}")
        return
    if step.action == "wait_for_text":
        find_element(device_id=device_id, selector=f"text={require_value(step)}")
        return
    raise RuntimeError(f"Unsupported Android QA step action: {step.action}")


def find_element(*, device_id: str, selector: str) -> AndroidElement:
    elements = dump_ui_elements(device_id=device_id)
    if selector.startswith("text="):
        expected = selector.removeprefix("text=")
        for element in elements:
            if element.text == expected or element.content_description == expected:
                return element
    elif selector.startswith("id="):
        expected = selector.removeprefix("id=")
        for element in elements:
            if element.resource_id.endswith(f":id/{expected}") or element.resource_id == expected:
                return element
    else:
        for element in elements:
            if (
                element.text == selector
                or element.content_description == selector
                or element.resource_id.endswith(f":id/{selector}")
            ):
                return element
    raise RuntimeError(f"Missing Android element: {selector}")


def dump_ui_elements(*, device_id: str) -> list[AndroidElement]:
    _run(["adb", "-s", device_id, "shell", "uiautomator", "dump", "/sdcard/window.xml"], capture_output=True)
    xml_payload = _run(["adb", "-s", device_id, "exec-out", "cat", "/sdcard/window.xml"], capture_output=True).stdout
    root = ET.fromstring(xml_payload)
    elements: list[AndroidElement] = []
    for node in root.iter("node"):
        bounds = parse_bounds(str(node.attrib.get("bounds") or ""))
        if bounds is None:
            continue
        elements.append(
            AndroidElement(
                text=str(node.attrib.get("text") or ""),
                resource_id=str(node.attrib.get("resource-id") or ""),
                content_description=str(node.attrib.get("content-desc") or ""),
                bounds=bounds,
            )
        )
    return elements


def parse_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)]\[(\d+),(\d+)]", value.strip())
    if match is None:
        return None
    return tuple(int(group) for group in match.groups())  # type: ignore[return-value]


def validate_mp4_recording(path: Path) -> None:
    payload = path.read_bytes()
    if not payload:
        raise RuntimeError(f"Android QA recording is empty: {path}")
    if b"moov" not in payload:
        raise RuntimeError(f"Android QA recording is incomplete; missing moov atom: {path}")


def stop_screenrecord(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:  # pragma: no cover
            process.kill()
            process.wait(timeout=5)
            raise RuntimeError("Timed out stopping Android screen recording") from exc
    if process.returncode not in {0, -signal.SIGINT}:
        raise RuntimeError(f"Android screen recording failed with exit code {process.returncode}")


def combined_recording_failure(
    *,
    primary_error: Exception | None,
    stop_error: Exception | None,
) -> Exception | None:
    if primary_error is None:
        return stop_error
    if stop_error is None:
        return primary_error
    return RuntimeError(f"{primary_error}\nAndroid screen recording shutdown also failed: {stop_error}")


def sanitize_recording_name(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", str(name or "demo").strip().lower()).strip("-")
    return normalized or "demo"


def require_selector(step: QaStep) -> str:
    value = str(step.selector or "").strip()
    if not value:
        raise RuntimeError(f"Android QA step requires selector for action {step.action}")
    return value


def require_value(step: QaStep) -> str:
    value = str(step.value or "").strip()
    if not value:
        raise RuntimeError(f"Android QA step requires value for action {step.action}")
    return value


def _run(
    args: list[str],
    *,
    cwd: Path | None = None,
    capture_output: bool,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    timeout_seconds = _adb_command_timeout_seconds()
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            check=check,
            capture_output=capture_output,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Android QA recorder command timed out after {timeout_seconds} seconds: {' '.join(args)}"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stdout = str(exc.stdout or "").strip()
        stderr = str(exc.stderr or "").strip()
        details = "\n".join(part for part in (stdout, stderr) if part)
        suffix = f"\n{details}" if details else ""
        raise RuntimeError(
            f"Android QA recorder command failed with exit code {exc.returncode}: {' '.join(args)}{suffix}"
        ) from exc


def _adb_command_timeout_seconds() -> int:
    raw_value = str(os.environ.get("QA_DEMO_ANDROID_ADB_TIMEOUT_SECONDS") or "").strip()
    if not raw_value:
        return DEFAULT_ADB_COMMAND_TIMEOUT_SECONDS
    try:
        configured = int(raw_value)
    except ValueError as exc:
        raise RuntimeError("QA_DEMO_ANDROID_ADB_TIMEOUT_SECONDS must be an integer") from exc
    if configured < 1:
        raise RuntimeError("QA_DEMO_ANDROID_ADB_TIMEOUT_SECONDS must be greater than zero")
    return configured


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
