# Operating Guide (Codex / Agents)

This repo is worked on by automated agents and humans. Agents must follow this guide and `.codex/POLICY.md` at all times.

## 1) How work is triggered
- Execution work ONLY runs for Jira issues in **To Do** (Backlog is non-executable).
- Code review automation runs on GitHub PR events (opened/updated/CI failure).

## 2) Before coding (must do)
- Read:
  - `.codex/POLICY.md`
  - `.codex/ENGINEERING_STANDARDS.md`
  - `.codex/policy_pack.*.json` (the most relevant one for this repo)
- Resolve runtime context from tenant/system state before making Jira/GitHub decisions:
  - `tenant_id`
  - `jira.project_keys`
  - `github_repository`
  - current execution mode (`pm` | `dev` | `test`)
- Validate the Jira ticket is “Good To Do”.
  - If not, trigger Decision Gate and stop.

### Good To Do checklist
- Problem clarity (objective, in/out scope, acceptance criteria)
- Context (component/repo mapping, relevant links, business impact)
- Testability (how-to-test definition)
- NFR decision (MVP vs scale-ready)
- Dependencies/risks explicitly called out
- Testability:
  - how to automate test is clearly defined
  - expected test layers are identified (unit / integration / UI automation)
  - UI automation requirement is defined for user-facing changes
  - demo evidence requirement is clear where applicable

If GTD is incomplete, stop early, request clarification, and mark blocked.

### Runtime context hard rule
- Never hardcode Jira project keys, issue prefixes, or repository URLs in execution logic.
- Project/repo selection must come from runtime tenant context.
- If runtime context is missing, stop and request/derive it explicitly.

## 3) Running locally (fill in for this repo)
> Update these commands to match this repo. If unsure, search existing package files and CI config.

### Build / install
- Java: `./mvnw -q -DskipTests package` (or Gradle equivalent)
- Node: `pnpm i` / `npm i` / `yarn`
- Python: `python -m venv .venv && pip install -r requirements.txt`

### Tests
- Use the repo’s test command from the policy pack.
- Must run tests before opening PR unless explicitly impossible (and then explain why).

### Lint / format
- Use repo scripts if present (`lint`, `format`).

## 4) Branch & PR conventions
- Branch: `jira/<ISSUE_KEY>-<short-slug>`
- PR title: `<ISSUE_KEY>: <summary>`
- PR body must include:
  - Summary (3 bullets max)
  - How to test (commands + steps)
  - Risks/notes (2 bullets max)
  - Acceptance criteria checklist
  - Evidence:
    - For UI-visible PRs, include a short demo video or equivalent visual evidence
    - screenshots are acceptable for simple changes, video required for workflows
    - demo must reflect acceptance criteria and critical user path
    - demo evidence supports review and does not replace automated testing
  - Automated UI test coverage must align with the demonstrated user flow where applicable

## 5) Where to add tests
- Backend: unit tests near the module; integration/API tests for owned persistence, transaction, auth, and wiring behavior where relevant.
- React/Web: component tests for isolated behavior; Playwright for real user journeys.
- UI automation for real workflows should hit the real backend API and may mock only external systems outside the product boundary.
- Do not mock owned APIs when the purpose of the test is to validate backend, transaction, or persistence behavior.

## 6) How agents should work
- Keep diffs minimal; avoid unrelated refactors.
- Prefer explicit state machines/events over time-based waits.
- No placeholders unless explicitly approved and tracked.
- Design tests to validate the layer where the behavior actually lives.
- Don’t just prove the feature works; actively try to break it the way a real tester would.
- For bug fixes, start by reproducing the real failure mode.
- Do not use mocked UI tests to prove backend/API correctness.

## 6.1) Mode handling (`pm` / `dev` / `test`)
- `pm` mode:
  - Clarify scope, objective, acceptance criteria, risks, and dependencies.
  - Trigger Decision Gate when ambiguity or NFR trade-offs exist.
- `dev` mode:
  - Implement only accepted scope.
  - Keep changes small and aligned to acceptance criteria.
- `test` mode:
  - Run targeted + relevant full suite.
  - Report exact commands and outcomes before claiming completion.

## 7) If blocked
- If requirements are unclear or design trade-offs matter:
  - Use `.codex/DECISION_GATE_TEMPLATE.md`
  - Ask questions and STOP.
- If you must create a follow-up:
  - Create a Jira issue in Backlog with links and exact work remaining.
