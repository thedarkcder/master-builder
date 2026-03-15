---
name: app-store-copy
description: Generate App Store and Google Play ASO copy plus headline variants for launch screenshots using a deterministic, compliance-safe workflow. Use when creating or refreshing mobile store listings, launch kits, screenshot messaging, or A/B messaging angles from real app capabilities.
---

# App Store Copy

Produce conversion-focused, truth-based store copy that matches visible UI and platform constraints.

## Inputs
Collect or confirm this exact input contract before drafting copy:

```text
App: [NAME]
Audience: [WHO]
Core promise: [ONE LINE]
Top benefits: [B1, B2, B3...]
Differentiators: [D1, D2, D3...]
Tone: [TONE]
Keywords: [KEYWORDS]
Compliance constraints: [ANY]
```

If any field is missing, ask only for the missing field(s) and continue.

## Workflow
1. Map inputs to a simple 8-frame narrative arc so copy and screenshots tell one story:
- `1`: Hero promise
- `2`: How it works
- `3`: Benefit 1
- `4`: Benefit 2
- `5`: Benefit 3
- `6`: Differentiator
- `7`: Trust/privacy/control
- `8`: Summary of all benefits

2. Write copy that is editorial and benefit-led:
- Prioritize outcomes over feature mechanics.
- Keep claims specific to visible functionality.
- Keep language emotionally clear, not technical.

3. Generate all required outputs in one pass:
- iOS subtitle (`<=30` chars)
- iOS promotional text (`<=170` chars)
- iOS full description (`<=4000` chars)
- Google Play short description (`<=80` chars)
- Google Play full description (`<=4000` chars)
- 7 punchy feature bullets
- 3 distinct A/B messaging angles
- 3 headline variants for each of the 8 slides (`24` headlines total)

4. Run a compliance and quality gate before finalizing:
- Reject unsupported claims, guarantees, or regulated promises unless explicitly allowed.
- Verify every benefit statement maps to actual UI or known app behavior.
- Remove jargon, repetition, and multi-idea headlines.
- Confirm each headline communicates one idea only.

## Voice And Style
- Use calm, premium, intentional language.
- Prefer short, concrete sentences.
- Prefer confident verbs and plain nouns.
- Avoid hype, superlatives, and vague "AI magic" phrasing.
- Make trust explicit where relevant (privacy, control, transparency).

## Output Format
Return content under these headings in this order:

1. `iOS`
2. `Google Play`
3. `Feature Bullets`
4. `Screenshot Headlines (8 x 3 variants)`
5. `A/B Angles`
6. `Validation Checklist`

Use this checklist in the final output:
- All length limits satisfied
- Claims fully supported
- Tone consistent with requested voice
- Headlines aligned to the 8-frame narrative
- No misleading or non-compliant language

## Packaging Convention
When asked for launch-kit file outputs, emit:
- `copy/appstore.md`
- `copy/playstore.md`
- `copy/headline_variants.csv`

If manifest content is requested, include:

```json
{
  "app": "AppName",
  "generated_at": "YYYY-MM-DD",
  "screens": 8,
  "variants_per_screen": 3,
  "ios_sizes": ["6.7", "6.5"],
  "android_size": "1080x1920"
}
```
