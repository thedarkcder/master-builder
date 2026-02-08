---
name: codex-assets-versioning
description: Use when changing codex policy assets so the codex assets version and dependency pin are updated automatically in the same commit.
---

## Trigger
Use this skill whenever a change touches any packaged codex asset:
- `.codex/POLICY.md`
- `.codex/ENGINEERING_STANDARDS.md`
- `.codex/OPERATING.md`
- `.codex/DECISION_GATE_TEMPLATE.md`
- `.codex/PR_READY_TEMPLATES.md`
- `.codex/policy_pack*.json`

## Workflow
1. Bump codex assets base version and sync dependency pin:
   - `python3 scripts/bump_codex_assets_version.py`
2. Validate guard before commit:
   - `scripts/validate_codex_assets_version.sh origin/staging`
3. Keep manifest + `pyproject.toml` version pin in the same commit as codex asset changes.

## Notes
- The publish workflow derives final channel version from branch:
  - `staging` -> beta/pre-release (`<assets_version>b<run_number>`)
  - `main` -> stable (`<assets_version>`)
- `assets_version` in manifest must stay stable semantic version (`X.Y.Z`).
