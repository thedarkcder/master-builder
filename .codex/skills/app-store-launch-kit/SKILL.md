---
name: app-store-launch-kit
description: Orchestrate end-to-end App Store and Google Play launch-kit generation from real mobile app builds. Use when users ask for full launch assets, including screenshot capture, Figma editorial slides, ASO copy, and packaged export files.
---

# App Store Launch Kit

Run this workflow to turn a real iOS and/or Android app into a complete, store-ready launch kit.

## Required Inputs
- App name
- Audience
- Core promise (one line)
- Top benefits (3-5)
- Differentiators
- Tone
- Keywords
- Compliance constraints
- iOS build and/or Android build that can run automated UI tests
- Optional brand colors and font guidance

## Workflow
1. Capture real screens with `app-store-screenshots`.
2. Select the top 8 screens using the defined scoring heuristics.
3. Create cinematic store slides in Figma with `app-store-figma-editorial`.
4. Generate listing copy and headline variants with `app-store-copy`.
5. Package all outputs with `app-store-asset-pack`.

Keep the narrative fixed across all apps:
- `1`: Hero promise
- `2`: How it works
- `3`: Benefit 1
- `4`: Benefit 2
- `5`: Benefit 3
- `6`: Differentiator
- `7`: Trust/privacy/control
- `8`: All benefits summary

## Non-Negotiables
- Screens must be real app UI.
- Copy must match visible product behavior.
- Messaging must be benefit-led, not feature-dump text.
- Claims must be compliant with platform and domain constraints.
- Output must be deterministic and repeatable for CI usage.

## Quality Gate
Mark the run failed if any condition is true:
- Fake UI or fabricated flows are used.
- Headline or copy claims are unsupported.
- Text is illegible or layout is cluttered.
- Test capture is flaky or non-deterministic.
- Required export files are missing.

## Expected Deliverable
Return a launch kit with this structure:

```text
/screenshots
  /ios
    /6.7
    /6.5
  /android
/copy
  appstore.md
  playstore.md
  headline_variants.csv
/figma
  figma_file_link.txt
/manifest.json
```

## Output Summary Format
At completion, summarize:
- What was captured
- Why each selected screen made the final 8
- What copy assets were generated
- Where final files were written
- Any compliance or review caveats
