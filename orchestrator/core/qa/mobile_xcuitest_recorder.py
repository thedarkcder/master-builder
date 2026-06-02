from __future__ import annotations

import re
from pathlib import Path

from orchestrator.core.workflow.runner import QaScenario, QaStep


def generated_test_method_name(name: str) -> str:
    parts = re.findall(r"[A-Za-z0-9]+", str(name or "scenario"))
    normalized = "".join(part[:1].upper() + part[1:] for part in parts if part)
    return f"test{normalized or 'Scenario'}"


def discover_xcode_project(*, repo_dir: Path, project_paths: list[Path] | None = None) -> Path:
    candidates = list(project_paths or repo_dir.glob("*.xcodeproj"))
    if not candidates:
        raise ValueError(f"No Xcode project found under {repo_dir}")
    root_candidates = [path for path in candidates if path.parent == repo_dir]
    preferred = root_candidates or candidates
    return sorted(preferred, key=lambda path: (len(path.parts), str(path)))[0]


def discover_xcuitest_file(*, repo_dir: Path, swift_files: list[Path] | None = None) -> Path:
    candidates = list(swift_files or repo_dir.glob("*UITests/*.swift"))
    if not candidates:
        raise ValueError(f"No UI test Swift file found under {repo_dir}")
    conventional = [path for path in candidates if path.name.endswith("UITests.swift")]
    preferred = conventional or candidates
    return sorted(preferred, key=lambda path: (len(path.parts), str(path)))[0]


def preferred_simulator_udid(simctl_output: str) -> str:
    matches = re.findall(r"^\s+(iPhone [^(]+) \(([A-F0-9-]+)\) \(([^)]+)\)\s*$", simctl_output, re.MULTILINE)
    if not matches:
        raise ValueError("No available iPhone simulator found")
    prioritized: list[tuple[int, str]] = []
    for name, udid, _state in matches:
        score = 99
        if name == "iPhone 16":
            score = 0
        elif name.startswith("iPhone 16"):
            score = 1
        elif name.startswith("iPhone 15"):
            score = 2
        prioritized.append((score, udid))
    prioritized.sort(key=lambda item: item[0])
    return prioritized[0][1]


def launch_arguments_for_scenario(scenario: QaScenario) -> list[str]:
    start_path = str(scenario.start_path or "").strip().lower()
    arguments = ["-uiTesting"]
    if "returning" in start_path:
        return arguments
    else:
        arguments.append("-resetOnboarding")
    return arguments


def simulator_test_build_flags() -> list[str]:
    return [
        "CODE_SIGNING_ALLOWED=NO",
        "CODE_SIGNING_REQUIRED=NO",
        "CODE_SIGN_IDENTITY=",
    ]


def render_xcuitest_source(*, test_class_name: str, scenarios: list[QaScenario]) -> str:
    if not scenarios:
        raise ValueError("At least one mobile QA scenario is required")

    methods = "\n\n".join(_render_scenario_method(scenario) for scenario in scenarios)
    return f"""import XCTest

final class {test_class_name}: XCTestCase {{
    private var app: XCUIApplication!

    override func setUp() {{
        super.setUp()
        continueAfterFailure = false
    }}

{methods}

    private func launchApp(arguments: [String]) {{
        app = XCUIApplication()
        app.launchArguments = arguments
        app.launch()
    }}

    private func relaunchApp(arguments: [String]) {{
        app?.terminate()
        launchApp(arguments: arguments)
    }}

    private func element(for selector: String) -> XCUIElement {{
        if selector.hasPrefix("text=") {{
            let text = String(selector.dropFirst(5))
            let button = app.buttons[text]
            if button.exists {{
                return button
            }}
            let staticText = app.staticTexts[text]
            if staticText.exists {{
                return staticText
            }}
            let navigationBar = app.navigationBars[text]
            if navigationBar.exists {{
                return navigationBar
            }}
            let predicate = NSPredicate(format: "label == %@ OR value == %@", text, text)
            let preferredElements = app.descendants(matching: .any).matching(predicate)
            return preferredElements.firstMatch
        }}
        if selector.hasPrefix("id=") {{
            let identifier = String(selector.dropFirst(3))
            let button = app.buttons[identifier]
            if button.exists {{
                return button
            }}
            let staticText = app.staticTexts[identifier]
            if staticText.exists {{
                return staticText
            }}
            let textField = app.textFields[identifier]
            if textField.exists {{
                return textField
            }}
            let secureTextField = app.secureTextFields[identifier]
            if secureTextField.exists {{
                return secureTextField
            }}
            let toggle = app.switches[identifier]
            if toggle.exists {{
                return toggle
            }}
            let otherElement = app.otherElements[identifier]
            if otherElement.exists {{
                return otherElement
            }}
            return app.descendants(matching: .any)[identifier]
        }}
        let button = app.buttons[selector]
        if button.exists {{
            return button
        }}
        let staticText = app.staticTexts[selector]
        if staticText.exists {{
            return staticText
        }}
        let predicate = NSPredicate(
            format: "identifier == %@ OR label == %@ OR value == %@",
            selector,
            selector,
            selector
        )
        return app.descendants(matching: .any).matching(predicate).firstMatch
    }}

    private func tap(selector: String, timeout: TimeInterval = 12.0) {{
        let target = assertVisible(selector: selector, timeout: timeout)
        target.tap()
    }}

    @discardableResult
    private func assertVisible(selector: String, timeout: TimeInterval = 12.0) -> XCUIElement {{
        let target = element(for: selector)
        XCTAssertTrue(target.waitForExistence(timeout: timeout), "Missing visible element: \\(selector)")
        return target
    }}

    private func waitForText(_ text: String, timeout: TimeInterval = 12.0) {{
        let target = element(for: "text=\\(text)")
        XCTAssertTrue(target.waitForExistence(timeout: timeout), "Missing text: \\(text)")
    }}

    private func assertText(selector: String, expected: String, timeout: TimeInterval = 12.0) {{
        let target = assertVisible(selector: selector, timeout: timeout)
        let predicate = NSPredicate {{ _, _ in
            let actual = self.currentText(for: target)
            return actual.contains(expected)
        }}
        let expectation = XCTNSPredicateExpectation(predicate: predicate, object: nil)
        let result = XCTWaiter.wait(for: [expectation], timeout: timeout)
        let actual = currentText(for: target)
        XCTAssertEqual(result, .completed, "Expected \\(expected) in \\(actual)")
    }}

    private func fill(selector: String, value: String, timeout: TimeInterval = 12.0) {{
        let target = assertVisible(selector: selector, timeout: timeout)
        target.tap()
        target.typeText(value)
    }}

    private func swipe(selector: String? = nil, direction: String, timeout: TimeInterval = 12.0) {{
        let target: XCUIElement = selector.map {{ assertVisible(selector: $0, timeout: timeout) }} ?? app
        switch direction.lowercased() {{
        case "left":
            target.swipeLeft()
        case "right":
            target.swipeRight()
        case "up":
            target.swipeUp()
        case "down":
            target.swipeDown()
        default:
            XCTFail("Unsupported swipe direction: \\(direction)")
        }}
    }}

    private func currentText(for target: XCUIElement) -> String {{
        if !target.label.isEmpty {{
            return target.label
        }}
        if let value = target.value {{
            return String(describing: value)
        }}
        return ""
    }}
}}
"""


def _render_scenario_method(scenario: QaScenario) -> str:
    method_name = generated_test_method_name(scenario.name)
    launch_arguments = ", ".join(_swift_string(argument) for argument in launch_arguments_for_scenario(scenario))
    rendered_steps = "\n".join(f"        {_render_step(step)}" for step in scenario.steps)
    return f"""    func {method_name}() {{
        launchApp(arguments: [{launch_arguments}])
{rendered_steps}
    }}"""


def _render_step(step: QaStep) -> str:
    if step.action == "click":
        return f'tap(selector: {_swift_string(_require_selector(step))})'
    if step.action == "relaunch_app":
        mode = str(step.value or "preserve").strip().lower()
        if mode == "reset":
            return 'relaunchApp(arguments: ["-uiTesting", "-resetOnboarding"])'
        return 'relaunchApp(arguments: ["-uiTesting"])'
    if step.action == "fill":
        return f'fill(selector: {_swift_string(_require_selector(step))}, value: {_swift_string(step.value or "")})'
    if step.action == "assert_visible":
        return f'assertVisible(selector: {_swift_string(_require_selector(step))})'
    if step.action == "wait_for_text":
        return f'waitForText({_swift_string(_require_value(step))})'
    if step.action == "assert_text":
        return (
            f'assertText(selector: {_swift_string(_require_selector(step))}, '
            f'expected: {_swift_string(_require_value(step))})'
        )
    if step.action == "press":
        normalized_value = str(step.value or "").strip().lower()
        if normalized_value in {"arrowleft", "left"}:
            if step.selector:
                return f'swipe(selector: {_swift_string(step.selector)}, direction: "right")'
            return 'app.swipeRight()'
        if normalized_value in {"arrowright", "right"}:
            if step.selector:
                return f'swipe(selector: {_swift_string(step.selector)}, direction: "left")'
            return 'app.swipeLeft()'
        if step.selector:
            return f'fill(selector: {_swift_string(step.selector)}, value: {_swift_string(step.value or "")})'
        return f'app.typeText({_swift_string(_require_value(step))})'
    if step.action == "select_option":
        return f'tap(selector: {_swift_string(step.value or step.selector or "")})'
    if step.action == "goto":
        value = str(step.value or "").strip().lower()
        if "returning" in value:
            return 'launchApp(arguments: ["-uiTesting"])'
        return 'launchApp(arguments: ["-uiTesting", "-resetOnboarding"])'
    raise ValueError(f"Unsupported mobile QA step action: {step.action}")


def _require_selector(step: QaStep) -> str:
    selector = str(step.selector or "").strip()
    if not selector:
        raise ValueError(f"QA step {step.action} requires a selector")
    return selector


def _require_value(step: QaStep) -> str:
    value = str(step.value or "").strip()
    if not value:
        raise ValueError(f"QA step {step.action} requires a value")
    return value


def _swift_string(value: str) -> str:
    escaped = (
        str(value)
        .replace("\\", "\\\\")
        .replace("\"", "\\\"")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'
