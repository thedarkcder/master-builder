from __future__ import annotations

from pathlib import Path

from orchestrator.core.workflow.runner import QaScenario, QaStep

from orchestrator.core.qa.mobile_xcuitest_recorder import (
    discover_xcode_project,
    discover_xcuitest_file,
    generated_test_method_name,
    launch_arguments_for_scenario,
    preferred_simulator_udid,
    render_xcuitest_source,
    simulator_test_build_flags,
)


def test_launch_arguments_for_scenario_defaults_to_fresh_install() -> None:
    scenario = QaScenario(
        name="Fresh install",
        objective="Show onboarding",
        capture_target="ios",
    )

    assert launch_arguments_for_scenario(scenario) == ["-uiTesting", "-resetOnboarding"]


def test_launch_arguments_for_scenario_preserves_state_for_returning_hint() -> None:
    scenario = QaScenario(
        name="Returning user",
        objective="Show returning CTA",
        capture_target="ios",
        start_path="app://returning-user",
    )

    assert launch_arguments_for_scenario(scenario) == ["-uiTesting"]


def test_generated_test_method_name_sanitizes_input() -> None:
    assert generated_test_method_name("Happy path / first run") == "testHappyPathFirstRun"


def test_render_xcuitest_source_maps_mobile_steps_to_swift() -> None:
    source = render_xcuitest_source(
        test_class_name="GirlPowerUITests",
        scenarios=[
            QaScenario(
                name="Happy path / first run",
                objective="Show onboarding and CTA",
                capture_target="ios",
                start_path="app://fresh-install",
                steps=[
                    QaStep(action="assert_visible", selector="text=Next"),
                    QaStep(action="click", selector="onboarding_primary_button"),
                    QaStep(action="wait_for_text", value="Continue"),
                    QaStep(action="relaunch_app", value="preserve"),
                    QaStep(action="assert_visible", selector="start_demo_button"),
                    QaStep(action="assert_text", selector="start_demo_button", value="Start Free Demo"),
                ],
            )
        ],
    )

    assert "func testHappyPathFirstRun()" in source
    assert 'launchApp(arguments: ["-uiTesting", "-resetOnboarding"])' in source
    assert 'assertVisible(selector: "text=Next")' in source
    assert 'tap(selector: "onboarding_primary_button")' in source
    assert 'waitForText("Continue")' in source
    assert 'relaunchApp(arguments: ["-uiTesting"])' in source
    assert 'assertText(selector: "start_demo_button", expected: "Start Free Demo")' in source
    assert 'if selector.hasPrefix("text=")' in source
    assert 'let button = app.buttons[text]' in source
    assert 'let staticText = app.staticTexts[text]' in source
    assert 'let button = app.buttons[identifier]' in source
    assert 'let staticText = app.staticTexts[identifier]' in source
    assert 'let otherElement = app.otherElements[identifier]' in source
    assert "XCTNSPredicateExpectation" in source
    assert "currentText(for: target)" in source


def test_render_xcuitest_source_resolves_bare_native_selectors_by_text_or_identifier() -> None:
    source = render_xcuitest_source(
        test_class_name="GirlPowerUITests",
        scenarios=[
            QaScenario(
                name="Bare selector support",
                objective="Allow canonicalized and legacy native selectors",
                capture_target="ios",
                steps=[
                    QaStep(action="assert_visible", selector="Start Free Demo"),
                    QaStep(action="click", selector="onboarding_primary_button"),
                ],
            )
        ],
    )

    assert 'let button = app.buttons[selector]' in source
    assert 'let staticText = app.staticTexts[selector]' in source
    assert 'format: "identifier == %@ OR label == %@ OR value == %@"' in source
    assert 'assertVisible(selector: "Start Free Demo")' in source
    assert 'tap(selector: "onboarding_primary_button")' in source


def test_render_xcuitest_source_prefers_controls_for_identifier_selectors() -> None:
    source = render_xcuitest_source(
        test_class_name="GirlPowerUITests",
        scenarios=[
            QaScenario(
                name="Identifier precedence",
                objective="Tap a concrete control instead of its container wrapper",
                capture_target="ios",
                steps=[
                    QaStep(action="click", selector="id=demo_toolbar_back_button"),
                ],
            )
        ],
    )

    button_index = source.index('let button = app.buttons[identifier]')
    other_element_index = source.index('let otherElement = app.otherElements[identifier]')
    fallback_index = source.index('return app.descendants(matching: .any)[identifier]')

    assert button_index < other_element_index < fallback_index


def test_render_xcuitest_source_maps_arrow_presses_to_swipes() -> None:
    source = render_xcuitest_source(
        test_class_name="GirlPowerUITests",
        scenarios=[
            QaScenario(
                name="Carousel navigation",
                objective="Navigate onboarding carousel one slide at a time",
                capture_target="ios",
                steps=[
                    QaStep(action="press", selector="onboarding_tabview", value="ArrowLeft"),
                    QaStep(action="press", selector="onboarding_tabview", value="ArrowRight"),
                ],
            )
        ],
    )

    assert 'swipe(selector: "onboarding_tabview", direction: "right")' in source
    assert 'swipe(selector: "onboarding_tabview", direction: "left")' in source


def test_render_xcuitest_source_does_not_use_returning_user_shortcut_for_goto() -> None:
    source = render_xcuitest_source(
        test_class_name="GirlPowerUITests",
        scenarios=[
            QaScenario(
                name="Returning user after relaunch",
                objective="Prove persisted onboarding state",
                capture_target="ios",
                steps=[
                    QaStep(action="goto", value="app://returning-user"),
                    QaStep(action="assert_visible", selector="start_demo_button"),
                ],
            )
        ],
    )

    assert '-returningUser' not in source
    assert 'launchApp(arguments: ["-uiTesting"])' in source


def test_discover_xcode_project_prefers_repo_root_project() -> None:
    repo_dir = Path("/tmp/repo")
    paths = [
        repo_dir / "Nested" / "Other.xcodeproj",
        repo_dir / "GirlPower.xcodeproj",
    ]

    assert discover_xcode_project(repo_dir=repo_dir, project_paths=paths) == repo_dir / "GirlPower.xcodeproj"


def test_discover_xcuitest_file_prefers_conventional_file_name() -> None:
    repo_dir = Path("/tmp/repo")
    paths = [
        repo_dir / "GirlPowerUITests" / "Helpers.swift",
        repo_dir / "GirlPowerUITests" / "GirlPowerUITests.swift",
    ]

    assert discover_xcuitest_file(repo_dir=repo_dir, swift_files=paths) == repo_dir / "GirlPowerUITests" / "GirlPowerUITests.swift"


def test_preferred_simulator_udid_chooses_first_iphone_16() -> None:
    simctl_output = """
== Devices ==
-- iOS 18.0 --
    iPhone 16 Pro (C07B4EE4-13BB-4E5B-9A71-1EA0F9B313B5) (Shutdown)
    iPhone 16 (8A77B676-BBF7-4335-8B22-75A08A5D064E) (Shutdown)
"""

    assert preferred_simulator_udid(simctl_output) == "8A77B676-BBF7-4335-8B22-75A08A5D064E"


def test_simulator_test_build_flags_disable_codesigning() -> None:
    assert simulator_test_build_flags() == [
        "CODE_SIGNING_ALLOWED=NO",
        "CODE_SIGNING_REQUIRED=NO",
        "CODE_SIGN_IDENTITY=",
    ]
