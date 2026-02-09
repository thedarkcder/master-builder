---
name: app-store-screenshots
description: Capture deterministic, truthful iOS and Android screenshots for store listings. Use when building App Store or Google Play visual assets from XCUITest and Espresso/UI Automator test runs.
---

# App Store Screenshots

Capture real UI moments from automated test flows, then pick the strongest 8 screens for store narrative use.

## Capture Principles
- Use real app screens only.
- Launch app in known stable state.
- Disable animations unless intentionally freezing a hero frame.
- Prefer outcome screens over setup flows.
- Use seeded demo data when available.

## iOS Capture Guidance (XCUITest)
Use launch arguments and environment flags for deterministic runs:

```swift
app.launchArguments += ["--ui-testing", "--disable-animations", "--use-demo-data"]
app.launchEnvironment = [
  "UITEST_MODE": "1",
  "FIXED_DATE": "2026-01-01",
  "FIXED_LOCALE": "en_GB"
]
```

Recommended suite layout:

```text
UITests/
  ScreenshotTests.swift
  Navigation/
    OnboardingFlow.swift
    CoreFlow.swift
    TrustFlow.swift
  Helpers/
    SnapshotHelper.swift
    LaunchConfig.swift
    AnimationControl.swift
```

Hero frame rule:
- If slide 1 uses motion in-app, freeze the strongest visual frame.
- Do not rely on animation to convey meaning.

## Android Capture Guidance (Espresso + UI Automator)
- Disable device animations at test setup.
- Seed test data before navigation.
- Use deterministic navigation helpers.
- Capture screenshot names with fixed numeric order.

Recommended suite layout:

```text
androidTest/
  ScreenshotTest.kt
  flows/
    CoreFlow.kt
    TrustFlow.kt
  helpers/
    ScreenshotHelper.kt
    TestLaunchConfig.kt
    DeviceConfig.kt
```

## Narrative Targets (8 Frames)
Map candidate screens to:
- `1`: Hero promise
- `2`: How it works
- `3`: Benefit 1
- `4`: Benefit 2
- `5`: Benefit 3
- `6`: Differentiator
- `7`: Trust/privacy/control
- `8`: All benefits summary

## Selection Heuristics
Score each candidate by:
- Benefit alignment
- Outcome visibility
- Visual clarity
- Emotional signal (confidence, relief, control)
- Uniqueness vs other selected screens

Reject screens that are:
- Dense or text-heavy
- Pure navigation with no value proof
- Visually redundant with higher-ranked candidates

## Output Requirements
Produce:
- Raw captures (all viable candidates)
- Selected set of 8 final source screens
- Selection rationale for each chosen frame
- Deterministic screenshot naming (`01_hero`, `02_home`, ... `08_all_benefits`)

## Automatic Fail Conditions
- Flaky tests without recovery
- Missing known-state setup
- Any mocked or fabricated UI
- Capture flow that cannot reproduce the same set on rerun
