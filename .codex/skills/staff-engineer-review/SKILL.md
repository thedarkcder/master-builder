---
name: staff-engineer-review
description: Autonomous Staff Engineering review protocol for design and implementation requests in any language or stack. Enforces problem framing, architectural judgment, risk analysis, simplicity checks, and a final proceed/constraint/decision-gate verdict before coding.
---

# Skill: Staff Engineering Review (Autonomous & Mandatory)

This skill is a **self-governing Staff Engineer protocol** executed by the agent.
It is not a suggestion engine and not primarily addressed to a human.

The agent must make decisions autonomously by default.
Human-in-the-loop (Decision Gate) is required only when risk, ambiguity, or irreversibility exceeds safe thresholds.

If the final verdict is not `✅ Proceed — design is appropriate and scoped`, **do not write code**.

---

## Autonomy Contract (Non-negotiable)

- This output is an **internal reasoning trace and audit artifact**, not a request for approval.
- The agent must proceed autonomously when:
  - requirements are sufficiently clear to choose a safe default,
  - changes are reversible or mitigated,
  - risk is low or medium with controls in place.
- The agent must trigger a **Decision Gate** only when:
  - ambiguity would materially change external behavior, contracts, or pricing,
  - a trade-off impacts reliability, security, privacy, compliance, cost, or performance,
  - the change is hard to roll back (data migration, contract break, data loss),
  - required intent cannot be inferred safely without guessing.
- If proceeding with assumptions, list them explicitly and keep them minimal.

---

## Required Output Format (Exact Order)

Produce output using these sections in this exact order:

1. Problem (1 sentence)
2. Type (one choice)
3. Invariants (2–5 bullets)
4. Assumptions (explicit defaults + why safe)
5. Proposed design (bullets with component placement)
6. Patterns used (with alternatives rejected)
7. Patterns not used
8. Change surface (files/modules/contracts)
9. Failure modes (detection + recovery)
10. Tests (invariants -> specific tests)
11. Verdict (exactly one)

Do not skip sections. Keep each section concise and complete.

---

## Phase 1: Problem Framing (Staff Mindset)

### 1) Define the real problem
- Determine the problem in one sentence **without implementation terms**.
- Identify what breaks if implemented incorrectly.
- If the problem cannot be stated without code concepts, trigger Decision Gate.

### 2) Classify the problem type
Choose exactly one dominant type:
- Simple CRUD
- Business rule enforcement
- Workflow / long-running process
- Integration boundary
- Performance / scale concern
- Reliability / correctness concern
- UX / interaction flow

If multiple types apply, select the dominant one and justify internally.

### 3) Determine non-negotiable invariants
Infer 2–5 invariants (business, technical, or compliance).
Examples:
- A customer is never charged twice.
- A lender decision is traceable.
- State transitions are monotonic.

If no clear invariants exist, call out overengineering risk explicitly.

---

## Phase 2: Architectural Judgment

### 4) Decide core logic placement
Determine where each concern lives:
- Decision logic → Domain or Application
- Orchestration → Application
- Side effects → Infrastructure
- Translation → Web/API layer

If placement cannot be decided confidently, trigger Decision Gate.

### 5) Choose minimal justified patterns
Select the minimum required from:
- Transaction script
- Domain model
- State machine
- Event-driven
- Request/response
- Batch / async
- Cache-aside
- Retry-with-idempotency

For each selected pattern:
- Why it is necessary for this problem.
- Why alternatives were rejected.

“Consistency” or “used elsewhere” is not valid justification.

### 6) Explicitly reject at least one pattern
Name at least one pattern **not used** and why it would be harmful or unnecessary here.

This enforces architectural restraint.

---

## Phase 3: Change Surface & Risk

### 7) Determine change surface
List:
- Files/modules touched
- External contracts affected
- Migration or rollout implications

If surface is large, justify why or propose a smaller first step.

If an external contract changes (HTTP/event/schema), also include:
- Before vs after contract
- Compatibility strategy (versioning, defaults, migration)
- Rollout plan if needed

### 8) Analyze production failure scenarios
For each risk, determine:
- Failure mode
- Detection mechanism
- Recovery or mitigation

Include:
- Partial failures
- Retry/idempotency risks
- Observability gaps

“Unlikely” is not a reason to skip analysis.

---

## Phase 4: Simplicity & Future Cost

### 9) Project six-month evolution
Determine:
- Most likely part to change
- Intentionally rigid part
- Key assumptions

If everything is flexible → call out vagueness risk.  
If everything is rigid → call out brittleness risk.

### 10) Explain in 60 seconds
Produce a 3–5 sentence explanation:
- Plain language
- No acronyms unless unavoidable
- No framework names unless essential

If explanation is not clear, refine design before coding.

---

## Phase 5: Enforcement Checks (Non-negotiable)

Before marking PR-ready, enforce all of the following:

### Structural rules
- Controllers are thin (translation only).
- Default: one endpoint → one use case.
- Exception: if multiple use cases are required, create a dedicated orchestration use case; controller still calls one use case.

### Dependency rules
- Domain must not import Application, Web/API, or Infrastructure packages.
- Application must not import Web framework types or Infrastructure concretions.
- Web/API layer must not import ORM or repository implementations.
- Infrastructure may depend on Application ports and external libraries only.

### Logic & quality rules
- No duplicated business logic.
- No forbidden concurrency patterns.
- No speculative abstractions.

### Test proof requirement
For each invariant, list at least one proving test in this format:
`Invariant -> Test name -> What it asserts`

If any enforcement check fails, refactor **before** feature work.

---

## Phase 6: Staff Review Verdict (Operational)

End with exactly one verdict:

- ✅ **Proceed — design is appropriate and scoped**
- ⚠️ **Proceed with constraints** — list constraints (e.g. feature flag, staged rollout)
- ❌ **Decision Gate required** — explain why and list questions

Hard rule:
- If verdict is not `✅ Proceed — design is appropriate and scoped`, the only allowed next action is Decision Gate output. No code may be written.

---

## Engineering Heuristics (Judgment, Not Hard Rules)

If a heuristic is violated, explain why.

### When to use a domain model
Use when:
- Invariants must never be broken.
- The same rule appears in 2+ use cases.
- Incorrect behavior has real business or compliance cost.

Otherwise prefer transaction scripts.

### When to introduce a new abstraction
Use when:
- It removes duplication **and** reduces cognitive load.
- It has one stable responsibility.
- It can be named clearly in one sentence.

If naming is hard, treat abstraction as premature.

### When not to add a pattern
Avoid when:
- It exists only for consistency.
- The team cannot explain it in two sentences.
- It optimizes a hypothetical future.

### When to refactor
Refactor when:
- Change friction is visible now.
- The same bug appears twice.
- Test setup dominates test intent.

Refactor protocol:
- Add characterization tests first for preserved behavior.
- Refactor in small, verifiable steps.
- Avoid feature + large structural refactor in one change unless trivial.

### Logic placement reminder
- Decisions → Domain
- Orchestration → Application
- Translation → Web/API
- Side effects → Infrastructure

If logic does not clearly fit, trigger Decision Gate.
