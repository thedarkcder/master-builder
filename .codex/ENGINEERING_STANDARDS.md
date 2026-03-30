# Engineering Standards

These are non-negotiable standards for agent and human changes in this repo. If a ticket conflicts with these rules, trigger a **Decision Gate** and stop before coding.

## 1) Core principles

### Clarity over cleverness
- Prefer straightforward, readable designs.
- Avoid “magic” abstractions unless they materially simplify the system.

### Minimize change surface
- Keep diffs tight and scoped to the ticket.
- Don’t reformat unrelated files.
- Don’t upgrade dependencies unless required by the ticket.

### Testable by default
- Every behavior change must include automated tests.
- Automated tests must prove the behavior a QA engineer would otherwise need to verify manually.
- For user-facing changes, the critical acceptance path must be covered by automated UI tests.
- Any omission requires explicit justification in the PR and is subject to review blocking.
- Use platform-standard UI automation:
  - Web: Playwright
  - Apple platforms: XCTest / XCUITest
  - Other platforms: equivalent standard automation tooling
- Tests must cover the appropriate layers:
  - unit tests for isolated logic
  - integration tests for boundaries and wiring
  - behavior / end-to-end tests for real workflows
  - UI automation for user-visible journeys
- Core logic must be testable without external services.
- Keep side effects at the edges (DB / HTTP / queues / filesystem / third-party APIs) to keep core logic clean and testable.
- Tests must validate the layer where the behavior actually lives.
- Never mock the layer you are trying to prove works; mock only beyond the system boundary.
- A page rendering is not proof of behavior.
- The proof point is the state-changing action.
- Don’t just prove the feature works; actively try to break it the way a real tester would.
- Never describe partial coverage as full coverage.
- For bug fixes, add a regression test that fails before the fix and passes after it.
- Implementation is not complete until the relevant automated tests are written and passing.
- For user facing applications, API tests are not enough. Verify the real operation path in browser, computer application, mobile app for all flows, including runtime-specific required fields and defaults.

## UI automation standard

For any change that affects user-visible behavior, evaluate whether the acceptance criteria should be proven through UI automation.

UI automation is required when:
- the feature is verified through screens, forms, navigation, or visible state changes
- the workflow is one a QA engineer would normally execute manually
- the change affects a critical user journey
- the bug being fixed was observed at the UI level
- the integration between frontend and backend is part of the value
- UI automation for real workflows must hit the real backend API.
- Mock only external systems outside the product boundary.
- For critical workflows, UI automation must cover both the main success path and at least one meaningful failure, misuse, or repeat-action scenario.

Preferred frameworks:
- Web: Playwright
- Apple platforms: XCTest / XCUITest
- Other platforms: standard platform automation tooling

Minimum expectation:
- coverage of the main success path
- coverage of at least one meaningful failure or validation path
- tests must assert visible user outcomes, not internal implementation details

Do not rely only on:
- shallow component/unit tests
- snapshots without behavioral assertions
- mocked UI assertions that do not prove real outcomes

If UI automation is not added for a user-facing change, the PR must explicitly justify why.

## Failure-path and boundary-testing standard

For bug fixes, incident fixes, and risky workflows:
- The primary test must reproduce the real failure mode.
- Happy-path-only tests are insufficient.
- If the defect is in an owned layer, tests must exercise that layer.
- Where a user-visible issue is caused by backend behavior, add:
  - a backend/API/integration regression test for the real failure path
  - UI automation for the user-visible workflow

## 2) Execution gate: “Good To Do” requirement

Agents must only work on items that are “Good To Do”. Before coding, confirm:
- Objective is clear in 1–2 sentences.
- Acceptance criteria exists (or is proposed and confirmed).
- Component/repo is clear.
- “How to automate test” is clear or proposed.
- Non-functional intent is explicit: **MVP quick test** vs **scale-ready**.
- Risks/dependencies identified.

If any are missing and could change design, trigger **Decision Gate** and stop.

## 3) Decision Gate (BA/PM mode)

Trigger a Decision Gate when:
- Requirements are ambiguous or conflicting.
- There’s a meaningful design trade-off (MVP vs scale-ready).
- A change likely impacts reliability, performance, security, privacy, cost, maintainability, or compliance.
- The agent would otherwise guess.

Decision Gate output must include:
- Options (MVP vs scale-ready), pros/cons, risks.
- A short recommendation.
- 3–5 specific questions.
- Who needs to answer (tag: [NEEDS-PM], [NEEDS-BA], [NEEDS-SECURITY], etc.).

**Hard rule:** If Decision Gate triggered, **do not start coding** until resolved.

## 4) Concurrency & timing (critical)

### Forbidden: time-based synchronization
Do NOT use:
- `Thread.sleep(...)`
- `time.sleep(...)`
- `setTimeout(...)` / `setInterval(...)`
- “wait 1s then try again”
- infinite loops (`while(true)` / `while True`)

These create flaky behavior and race conditions.

### Required alternatives
Use one of:
1) **State machines**
   - explicit states + transitions + events
   - persist state for long-running workflows
2) **Event-driven**
   - publish events; react to events
3) **Condition-based waits**
   - wait for a condition with a timeout and meaningful error
   - tests: `findBy*`, `waitFor`, explicit timeouts
4) **Deterministic time in tests**
   - fake timers / injected clock

Exceptions are rare and must be:
- explicitly justified in code comment
- bounded (timeouts)
- paired with a condition check
- approved during review

## 5) No placeholders rule

Forbidden unless explicitly approved:
- “TODO implement later” stubs
- placeholder methods that return dummy values in production paths
- UI placeholders without behavior where behavior is part of acceptance criteria

If partial work is unavoidable:
- Create a follow-up Jira issue in **Backlog** describing exactly what remains (file paths, behavior).
- Mark current run **blocked** and stop.

Don't leave dead code around
- If the code and is behaviour is no longer required remove it

Reviewer must block PRs with untracked placeholders.

## 6) Error handling & observability

- Don’t swallow exceptions.
- Use typed/structured errors where possible.
- Add correlation IDs to logs if applicable.
- Never log secrets, tokens, credentials, or PII.
- Prefer idempotent handlers for event processing.

## 7) Review quality gates

A PR should be blocked if it contains:
- any forbidden sleep/time-based sync
- missing tests for behavior changes (unless explicitly justified)
- placeholders without linked follow-up issue
- unclear “how to test”
- changes that imply major NFR impact without Decision Gate discussion
- missing UI automation for user-facing changes without justification
- missing demo evidence for UI-visible changes
- "how to test" steps that depend on manual exploration when the workflow is automatable
- tests only cover the happy path for workflows with meaningful failure or misuse paths
- tests mock the owned layer where the defect actually exists
- UI tests are used as the main proof of backend/API behavior without hitting the real backend
- no regression test reproduces the reported failure mode for a bug fix

## 7.1) Mandatory test execution gate (pre-commit and pre-PR)

Before commit:
- Run targeted tests that cover changed behavior.
- If targeted tests fail/skip/not-run: stop and do not commit.

Before PR open/update:
- Run the relevant full suite for the affected repo/service.
- If full suite fails/skip/not-run: stop and do not open/update PR.

Required evidence in PR/Jira update:
- Exact targeted command(s) and exact full-suite command(s).
- Pass/fail outcome for each command.
- Short mapping: changed behavior -> targeted test command.

Hard rule:
- “Partial tests passed” is not an acceptable completion state.
- If environment/tooling prevents full execution, mark blocked and state the blocker explicitly.

## 8) Output formatting (signal-only)

Discord “PR Ready” message:
- PR link
- 3 bullets: what changed
- 2 bullets: risk/impact
- 3 steps: how to test
- questions (only if Decision Gate items remain)

Jira final comment:
- PR link
- summary
- acceptance criteria ✅/⚠️/❌
- how to test (commands + steps)
- notes (rollout/migrations/monitoring)
- follow-ups created (Backlog)


## 9) Shared Logic Recognition & Reuse Standard

When designing new commands, workflows, or logic paths, the agent must assume that similar problems may already have been solved elsewhere in the system. Before implementing any new logic, the agent should actively evaluate whether an existing service, module, or function provides the same or similar behaviour. This reflects the principle that systems should evolve toward a **single implementation of shared behaviour**, rather than duplicating logic across multiple paths.

If a potential overlap is identified, the agent must not proceed with implementation immediately. Instead, it should pause and validate intent by asking:  
- “Does this new functionality rely on the same underlying logic as an existing capability?”  
- “Should this reuse or extend an existing service, or is this intentionally different?”  

This ensures alignment with product expectations and prevents divergence in behaviour across the system.

## 10)  Agent Design Behaviour (Engineering Mindset)

The agent must think like an experienced engineer: pattern recognition comes before implementation. When multiple inputs, commands, or workflows appear to produce similar outcomes, the agent should treat them as **clients of a shared capability**, not as independent implementations. If no shared abstraction exists yet, the agent should propose creating one before proceeding.

In ambiguous cases—especially where acceptance criteria are incomplete or unclear—the agent must surface the assumption explicitly. For example:  
- “This appears similar to existing behaviour in [X]. Should this follow the same logic?”  
- “If these paths are expected to behave consistently, I recommend extracting this into a shared service/module.”  

The default behaviour is **reuse and centralisation**, not duplication. Any deviation from shared logic must be intentional and explicitly confirmed.