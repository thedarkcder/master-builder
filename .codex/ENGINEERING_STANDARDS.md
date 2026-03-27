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
- Core logic must be testable without external services.
- Keep side effects at the edges (DB/HTTP/queues/etc.).

## 2) Execution gate: “Good To Do” requirement

Agents must only work on items that are “Good To Do”. Before coding, confirm:
- Objective is clear in 1–2 sentences.
- Acceptance criteria exists (or is proposed and confirmed).
- Component/repo is clear.
- “How to test” is clear or proposed.
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