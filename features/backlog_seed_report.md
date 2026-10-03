# Backlog Seed Report

Generated: 2026-02-06

Historical seeding record only. This is not a current backlog or evidence of
outstanding issue status. Master Builder development is now tracked in GitHub
Issues; current Jira import remains pending source-project access.
Project: `MAB` (Multi Agent Builder)

## Summary
- Extracted backlog items from `features/v0.1.md` through `features/v0.6.md`.
- Recreated and backfilled the backlog in `MAB` after the initial seeding landed in the wrong project.
- All seeded issues now contain long-form descriptions aligned to the feature docs.
- No dedicated tracking epic was available; this file remains the Phase 0 summary artifact.

## Seeded / Updated Items
| Backlog Item | Jira Issue | Status |
|---|---|---|
| v0.1-001 - Core service scaffold and storage foundation | MAB-5 | updated |
| v0.1-002 - Tenant model and admin tenant CRUD | MAB-6 | updated |
| v0.1-003 - Jira MCP adapter and webhook ingestion | MAB-7 | updated |
| v0.1-004 - GitHub App auth, git isolation, and PR creation | MAB-8 | updated |
| v0.1-005 - Run queue, locking, and idempotency controls | MAB-9 | updated |
| v0.1-006 - Multi-agent workflow runner (PM -> Dev -> Test -> Review) | MAB-10 | updated |
| v0.1-007 - Guardrails: command policy, repo allowlist, and data safety | MAB-11 | updated |
| v0.1-008 - Admin dashboard and run observability APIs | MAB-12 | updated |
| v0.1-009 - Public endpoints, CLI entrypoints, and packaging docs | MAB-13 | updated |
| v0.2-001 - Repo bootstrap for .codex policy and skills | MAB-14 | updated |
| v0.2-002 - Discord channel lifecycle and signal notifications | MAB-15 | updated |
| v0.2-003 - Optional Discord to Jira issue creation flow | MAB-16 | updated |
| v0.2-004 - Stage-aligned Discord and Jira update flow | MAB-17 | updated |
| v0.3-001 - Human gate with Ready for Agent status configuration | MAB-18 | updated |
| v0.3-002 - Ready-status webhook triggering and idempotent enqueue | MAB-19 | updated |
| v0.3-003 - Ready-gate UX updates (admin preview and Discord guidance) | MAB-20 | updated |
| v0.4-001 - Execution gate: To Do executable, Backlog non-executable | MAB-22 | updated |
| v0.4-002 - Webhook transition rules for executable statuses | MAB-21 | updated |
| v0.4-003 - Backlog-only follow-up issues and trigger model separation | MAB-23 | updated |
| v0.5-001 - Discord command set with permissions and run controls | MAB-24 | updated |
| v0.5-002 - Engineering standards baseline and no-racy-sleeps enforcement | MAB-25 | updated |
| v0.5-003 - Policy pack loading and banned-pattern review checks | MAB-26 | updated |
| v0.5-004 - Reviewer quality gates and prompt injection updates | MAB-27 | updated |
| v0.6-001 - Good To Do validation and early blocking flow | MAB-4 | updated |
| v0.6-002 - Decision Gate workflow and stop-before-build enforcement | MAB-3 | updated |
| v0.6-003 - No-placeholders enforcement and tracked follow-up policy | MAB-2 | updated |
| v0.6-004 - Signal-only templates and backlog-only follow-up issue handling | MAB-1 | updated |

## Jira MCP / Project Capability Gaps
- **Backlog status is not available in this project workflow** through current transitions; issue creation defaults to `To Do`.
- **Issue components were not populated via MCP** for these recreated items.
- **Issue links (blocks/depends-on) cannot currently be written via the available MCP edit wrapper**.
  - Attempted update of `issuelinks` returned `Bad Request`.
  - Dependencies remain represented by stable version keys in summaries (for example `v0.1-001` -> `v0.1-002`).

## Post-Seed Normalization (2026-02-06)
- Applied canonical labels to all 27 issues:
  - Base: `master-builder`, `orchestrator`
  - Version: one of `v0.1`..`v0.6`
  - Component fallback labels: `comp-api`, `comp-storage`, `comp-discord`, `comp-github`, `comp-jira`, `comp-admin-ui`
- Verification checks:
  - `project = MAB AND (labels is EMPTY OR labels not in (master-builder))` -> `0 issues`
  - `project = MAB AND (labels is EMPTY OR labels not in (orchestrator))` -> `0 issues`
  - Version counts: `v0.1=9`, `v0.2=4`, `v0.3=3`, `v0.4=3`, `v0.5=4`, `v0.6=4` (total `27`)

## Known Deliberate Deviation
- Title prefix `[MASTER-BUILDER]` was removed from summaries by explicit user direction. Version-based keys (`v0.x-###`) remain in each title for stable lookup.

## Phase 1 Gate
- There are executable issues in `To Do` for v0.1.
- Execution has resumed in roadmap order, starting with `MAB-7` (`v0.1-003`) after validating existing scaffold coverage.
