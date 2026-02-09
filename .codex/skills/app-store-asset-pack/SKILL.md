---
name: app-store-asset-pack
description: Package App Store and Play launch outputs into deterministic file structures and naming conventions. Use when exporting final screenshot assets, copy files, Figma link references, and manifest metadata for release or CI pipelines.
---

# App Store Asset Pack

Assemble and validate the final launch-kit bundle so it is ready for manual upload or CI artifact publishing.

## Required Directory Layout
Emit this exact structure:

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

## Naming Rules
Use deterministic names:
- `ios_67_01.png` ... `ios_67_08.png`
- `ios_65_01.png` ... `ios_65_08.png`
- `android_01.png` ... `android_08.png`

## Image Format Rules
- PNG only
- sRGB color space
- No transparency unless platform requirements demand it
- Preserve legibility after export resizing

## Manifest Contract
Create `manifest.json`:

```json
{
  "app": "MyApp",
  "generated_at": "2026-02-08",
  "screens": 8,
  "variants_per_screen": 3,
  "ios_sizes": ["6.7", "6.5"],
  "android_size": "1080x1920"
}
```

`generated_at` must use ISO date (`YYYY-MM-DD`).

## Packaging Checklist
- All expected files exist.
- Exactly 8 slide exports per target size.
- Copy files include all required sections.
- Figma link file points to the final source file.
- Manifest values match actual bundle contents.

## Fail Conditions
- Missing or duplicate image names
- Incorrect dimensions or format
- Missing copy deliverables
- Manifest that does not reflect real outputs
