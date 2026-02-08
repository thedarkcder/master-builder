---
name: codex-jira-backlog
description: Use when creating, seeding, deduplicating, or maintaining Jira backlog issues from specs/markdown. Keeps backlog issues accurate and non-executable until promoted.
---

## When to use
- User asks to seed issues from docs/markdown.
- User asks to recreate missing backlog items.
- User asks to dedupe or sync Jira issues to latest spec.
- User asks to keep backlog organized before execution starts.

## Inputs (must obtain)
- Project key from runtime tenant context (`tenant.jira.project_keys`).
- Source spec(s) or markdown content.
- Label/component conventions (if any).
- Desired issue type (default `Task`, use `Bug` for defects).

## Backlog rules
- Backlog seeding is planning, not execution.
- Issues created/updated during seeding stay in `Backlog`/`To Do` (do not move to `In Progress`).
- If work starts immediately on an issue, then switch to `jira-feature-flow` and move that issue to `In Progress`.

## Workflow
1) Parse source spec into candidate issue list.
2) For each candidate, search Jira first to find existing issue by stable key/title pattern.
3) If issue exists:
- Update summary/description/acceptance criteria/labels/components to match spec.
4) If issue does not exist:
- Create new issue in target project with correct type and structured description.
5) Keep scope clean:
- No placeholders in acceptance criteria.
- No guessed requirements; if ambiguous, add a decision note and mark blocked risk.
6) Maintain links:
- Add dependency links (`blocks`/`is blocked by`) when sequencing is explicit in spec.
7) Report result:
- For each item include Jira key + action (`created` or `updated`) + missing capability notes.

## Required description structure
- Objective
- Scope (in / out)
- Acceptance Criteria
- Tags / labels
- Good To Do checklist
- Decision Gate triggers
- Notes / Links

## Guardrails
- Always dedupe before creating.
- Do not silently skip failed creates/updates; return a clear per-item failure list.
- Do not force repo-specific labels unless explicitly requested.
- Do not hardcode project keys; use runtime tenant project keys only.
- Keep Jira comments concise and operational.
