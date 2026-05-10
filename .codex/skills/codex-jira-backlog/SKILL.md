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
- Seed one PM-owned parent issue plus one or more engineering child tickets.
- The parent issue is the canonical product brief and is not executable.
- Engineering child tickets are the technical execution units.

## Workflow
1) Parse the source spec into:
- one PM parent feature
- one or more engineering child tickets
2) Search Jira for the parent feature first by stable key/title pattern.
3) If the parent exists:
- Update the parent summary/description/labels to match the latest product brief.
4) If the parent does not exist:
- Create the parent issue in the target project with the correct non-technical product brief.
5) For each engineering child:
- Search under the matched parent first.
- Update existing child tickets where possible.
- Create missing child tickets only after the parent is current.
6) Keep scope clean:
- No placeholders in acceptance criteria.
- No guessed requirements; if ambiguous, add a decision note and mark blocked risk.
7) Maintain hierarchy and sync:
- Parent must be created or updated before children.
- Child tickets must link back to the parent and carry parent sync metadata.
- If the parent changes materially, refresh only the impacted children.
8) Maintain links:
- Add dependency links (`blocks`/`is blocked by`) when sequencing is explicit in spec.
9) Report result:
- Include the parent Jira key + action.
- Include child Jira keys + action (`created` or `updated`) + missing capability notes.

## Required description structure
- Use Markdown with **bold section titles** and `-` list bullets.
- Parent issue must use product-focused sections:
- **Objective**
- **User / Business Value**
- **Recommendation**
- **Scope In**
- **Scope Out**
- **Acceptance Criteria**
- **UI / Design / References**
- **Success Outcomes**
- **Dependencies / Risks**
- **Open Questions**
- **Good To Do checklist**
- **Sync Status**
- **Notes / Links**
- Child issue must use engineering-focused sections:
- **Technical Objective**
- **Parent Feature Link**
- **Behavior Slice**
- **Implementation Plan**
- **How to Test**
- **Done Criteria**
- **Technical Dependencies / Risks**
- **Synced From Parent Revision**
- **Notes / Links**
- Do **not** put tags/labels inside the description body.
- Put labels only in Jira issue metadata (`labels` field).

## Guardrails
- Always dedupe before creating.
- Do not silently skip failed creates/updates; return a clear per-item failure list.
- Do not force repo-specific labels unless explicitly requested.
- Do not hardcode project keys; use runtime tenant project keys only.
- Keep Jira comments concise and operational.
- Description formatting is mandatory: bold section headers + list bullets for readability and consistency.
- Use the implemented parent/child labels consistently:
- `pm-parent`
- `engineering-child`
- `sync-current`
- `sync-stale`
- `sync-blocked`
