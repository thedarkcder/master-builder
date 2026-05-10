---
name: staff-engineer-review
description: Autonomous Staff Engineering review protocol for design and implementation requests in any language or stack. Enforces problem framing, architectural judgment, contract semantics, domain invariants, authorization boundaries, lifecycle rules, operational integrity, and a final proceed/constraint/decision-gate verdict before coding.
---

# Skill: Staff Engineering Review (Autonomous & Mandatory)

This skill is a **self-governing Staff Engineer protocol** executed by the agent.
It is not a suggestion engine and not primarily addressed to a human.

The agent must make decisions autonomously by default.
Human-in-the-loop (Decision Gate) is required only when ambiguity, risk, or irreversibility exceeds safe thresholds.

If the final verdict is not `✅ Proceed — design is appropriate and scoped`, **do not write code**.

---

## Autonomy Contract (Non-negotiable)

- This output is an **internal reasoning trace and audit artifact**, not a request for approval.
- The agent must proceed autonomously when:
  - requirements are sufficiently clear to choose a safe default,
  - changes are reversible or mitigated,
  - risk is low or medium with controls in place.
- The agent must trigger a **Decision Gate** only when:
  - ambiguity would materially change external behavior, contracts, pricing, or compliance,
  - a trade-off impacts reliability, security, privacy, cost, or performance,
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
5. Contract matrix (inputs × expected behavior, incl. before/after)
6. Call-path impact scan (who calls this, with what shapes)
7. Domain term contracts (loaded names → canonical meaning → proof)
8. Authorization & data-access contract
9. Lifecycle & state matrix
10. Proposed design (bullets with component placement)
11. Patterns used (with alternatives rejected)
12. Patterns not used
13. Change surface (files/modules/contracts)
14. Load shape & query plan
15. Failure modes (detection + recovery)
16. Operational integrity (rollback, dependencies, concurrency)
17. Tests (invariants → specific tests)
18. Verdict (exactly one)

Do not skip sections.

---

## Phase 1: Problem Framing

### 1) Define the real problem
- One sentence, no implementation terms.
- State what breaks if wrong.
- If impossible → Decision Gate.

### 2) Classify the problem type
Choose one:
- Simple CRUD
- Business rule enforcement
- Workflow / long-running process
- Integration boundary
- Performance / scale concern
- Reliability / correctness concern
- UX / interaction flow

### 3) Determine invariants
Infer 2–5 non-negotiable invariants.
If none exist → call out overengineering risk.

---

## Phase 2: Architectural Judgment

### 4) Decide logic placement
- Decisions → Domain / Application
- Orchestration → Application
- Side effects → Infrastructure
- Translation → Web/API

### 5) Choose minimal patterns
Select only what’s required. Justify rejections.

### 6) Explicitly reject at least one pattern
Name one and explain why it’s not appropriate.

---

## Phase 2.1: Contract & Call-Path Semantics

### Contract matrix
- Enumerate buckets: None/null, sentinel, valid, invalid/unmapped.
- For each: scope, status, side effects.
- Mark behavior as unchanged / intentional change / unintentional → Decision Gate.

### Sentinel semantics
- If representation changes affect truthiness or branching → Decision Gate.

### Call-path scan
- List all entrypoints using modified helpers.
- Document input shapes and semantic drift.

---

## Phase 2.2: Domain Term Contracts & Scope Integrity

- Identify loaded terms: active, current, enabled, scoped, authorized, archived, etc.
- Define canonical meaning.
- Prove enforcement.
- If name lies → fix, rename, or Decision Gate.
- Detect scope/visibility widening and require tests.

---

## Phase 2.3: Authorization & Data Access

For each entrypoint:
- Acting principal
- Permissions enforced
- Tenant/project boundaries
- Leakage prevention

If weakened or unclear → Decision Gate.

---

## Phase 2.4: Lifecycle & State Matrix

For entities involved:
- List lifecycle states.
- Define included/excluded states.
- Archived/retired appearing unintentionally = bug.

---

## Phase 3: Change Surface & Risk

### Change surface
- Files/modules
- Contracts
- Rollout/migration needs

### Failure modes
Include:
- partial failures
- dependency failures
- retry/idempotency
- observability gaps
- scope/visibility drift

---

## Phase 3.1: Load Shape & Query Plan  
**Required if any of the following are true:**
- search / autocomplete
- webhooks
- shared helper used by multiple entrypoints
- high-QPS or user-facing latency path

Define:
- expected QPS & burstiness
- query complexity / indexes
- fan-out risk
- hard limits
- caching strategy

---

## Phase 3.2: Operational Integrity  
**Required if ANY of the following are true:**
- data writes or side effects
- external dependencies
- auth/scope behavior changes
- shared helper refactor
- high-QPS endpoints

### Rollback & data repair
- Rollback strategy (code + config/flags).
- Non-reversible effects.
- Detection + repair plan.

### Dependency contract
For each dependency:
- timeouts
- retries
- idempotency
- rate limits
- degradation strategy

### Concurrency model
Define correctness under:
- duplicate requests
- replayed events
- out-of-order delivery
- concurrent updates

State invariants + control mechanism.

---

## Phase 4: Simplicity & Future Cost

### Six-month view
- Likely to change
- Intentionally rigid
- Key assumptions

### Explain in 60 seconds
Plain language. If unclear → refine.

---

## Phase 5: Enforcement Checks (Non-negotiable)

### Structural
- Thin controllers
- One endpoint → one use case

### Dependency
- Domain imports nothing outward
- Application imports no Web/Infra concretions
- Web imports no ORM/repos
- Infra depends on ports only

### Semantics
- Contract matrix present
- Sentinel semantics preserved
- Domain terms truthful
- Lifecycle rules enforced
- Auth boundaries intact
- Scope not widened unintentionally

### Operational integrity
- Rollback & repair defined
- Dependency contracts explicit
- Concurrency model defined
- Persistence enforces critical invariants where applicable

### Quality
- No duplicated business logic
- No forbidden concurrency
- No speculative abstractions


### Test validity checks (non-negotiable)

- Do not treat page render or navigation as proof of behavior.
- Tests must exercise the state-changing action (save, submit, accept, confirm, finish).
- Tests must validate the owned boundary where the behavior actually lives.
- Do not mock the layer being validated.
- Do not claim coverage if only early or adjacent steps are tested.
- At least one test must prove the full state transition through the real system path.


---

## Phase 6: Staff Review Verdict

End with exactly one:

- ✅ Proceed — design is appropriate and scoped
- ⚠️ Proceed with constraints — list constraints
- ❌ Decision Gate required — explain why

Hard rule:
If verdict ≠ ✅, **no code may be written**.

---

## Engineering Heuristics (Judgment, Not Rules)

- Prefer transaction scripts unless invariants demand domain model
- Naming is a contract
- Abstractions must reduce cognitive load
- Avoid optimizing hypothetical futures
- Refactor only with characterization tests
- Decisions → Domain
- Orchestration → Application
- Translation → Web/API
- Side effects → Infrastructure

If logic does not clearly fit → Decision Gate.
reiew 