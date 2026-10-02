---
name: staff-engineer-review
description: Autonomous Staff Engineering protocol for reality modeling, design-space exploration, architectural review, implementation guidance, and outcome verification. Enforces ownership boundaries, lifecycle correctness, contract semantics, operational integrity, design selection, and proof of user outcomes before and after implementation.
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

0. Reality model
   - Facts / Events / Intentions / Decisions / Artifacts / Observations
   - Product truth model
   - Ownership & evidence
   - State machine, if applicable
   - Root cause classification
   - Anti-patching verdict
0.5 Design search
   - Candidate models explored
   - Candidate diversity proof
   - Rejected models + reasons
   - Design Search Exhaustion Gate result
   - Greenfield comparison
   - Model Deletion Test result
   - Chosen model + trade-off accepted
   - Evidence that would change the decision
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


## Phase 0: Reality Modeling & Anti-Patching Gate

This phase must run before problem framing, design, or code.

The agent must model the system reality before proposing implementation.

### 0.1 Reality Inventory

Identify each item as one of:

- **Fact** — something durably true in the business/domain
- **Event** — something that happened
- **Intention** — something someone tried or wanted to do
- **Decision** — a business conclusion the system is allowed to make
- **Artifact** — transient workflow/process data
- **Observation** — evidence from logs, errors, traces, UI, or provider responses

Rules:

- Never treat an Event as a Fact.
- Never treat an Intention as a Decision.
- Never treat an Artifact as source of truth.
- Never treat an Observation as root cause.

---

### 0.2 Product Truth Model

Define:

- What the user believes is true
- What the business believes is true
- What the system currently stores
- What external systems/providers believe is true, if any
- Which system owns the canonical truth
- How mismatches are reconciled

If product truth and stored truth differ, design must explain the reconciliation path.

---

### 0.3 Ownership & Evidence

For every business fact involved, state:

- Owner
- Source of truth
- Evidence required
- Legal producers of that evidence
- Legal consumers of that fact
- What must not infer this fact

If ownership is unclear, return `❌ Decision Gate required`.

---

### 0.4 State Machine Requirement

Required for any workflow, lifecycle, async process, approval, provisioning, integration, auth, billing, onboarding, or recovery path.

Define:

- Entity
- States
- Legal transitions
- Triggering evidence
- Owner of each transition
- Idempotency rule
- Duplicate event behavior
- Out-of-order event behavior
- Missing event behavior
- Terminal states
- Recovery states

If the state machine cannot be described without implementation terms, return to modeling.

---

### 0.5 Root Cause Classification

Before proposing a fix, classify the problem as exactly one:

- Domain model failure
- Ownership failure
- Lifecycle/state failure
- Contract/API failure
- Authorization/scope failure
- Operational/reliability failure
- Data integrity failure
- UX/product flow failure
- Implementation defect

`Implementation defect` is allowed only after the other categories have been considered and rejected.

---

### 0.6 Forbidden Jump Detection

The agent must not propose implementation details before completing Phase 0.

Forbidden early terms include:

- endpoint
- API
- database
- table
- column
- enum
- repository
- service
- controller
- cache
- queue
- retry
- fallback
- null check
- webhook
- cron
- migration
- framework/library-specific fix

If any appear before facts, ownership, evidence, and lifecycle are defined:

Stop and restart from Phase 0.

---

### 0.7 Symptom Patch Detection

If the proposed change includes:

- adding a flag
- adding a fallback
- adding a conditional
- adding a retry
- adding a null check
- adding another status
- adding a workaround
- silently accepting an invalid state
- preserving compatibility with a model known to be wrong

Then answer:

> What incorrect model would this preserve?

If the answer is unclear, the change is not allowed.

---

### 0.8 Whiteboard Test

Before implementation, explain the system using only:

- actors
- facts
- decisions
- states
- transitions
- ownership
- evidence

Do not mention:

- code
- APIs
- databases
- classes
- frameworks
- providers
- libraries

If this explanation is unclear, return to modeling.

---

### 0.9 Reality Before Code Questions

Before any design or code, answer:

1. What business/domain fact changes?
2. Who owns that fact?
3. What evidence proves it?
4. What lifecycle governs it?
5. What state transition is legal?
6. What happens if evidence never arrives?
7. What happens if evidence arrives twice?
8. What happens if evidence arrives out of order?
9. What must not infer this fact?
10. Is the proposed change fixing the model or compensating for it?

If any answer is materially unclear, return `❌ Decision Gate required`.

---

### 0.10 Anti-Patching Verdict

End Phase 0 with one of:

- `✅ Model understood — proceed to Phase 1`
- `⚠️ Model incomplete — proceed only with constraints`
- `❌ Model unclear — Decision Gate required`

If Phase 0 verdict is not `✅`, no code may be written unless the final Staff Review verdict is also `⚠️ Proceed with constraints` and the constraints explicitly prevent model damage.

### 0.11 Product Truth vs Implementation Truth

For every externally visible contract:

- API response
- UI model
- Event contract
- Public DTO
- GraphQL schema
- SDK contract

Define:

#### Product Truth

What outcome the user/business cares about.

#### Implementation Truth

Internal details used to achieve that outcome.

Rules:

- Implementation truth must not leak into public contracts.
- Provider identifiers must not leak unless they are user meaningful.
- Internal enums must not become API contracts.
- Workflow states must not become user-facing states unless intentionally designed.
- Infrastructure concerns must not become product concerns.

If a consumer must understand:

- provider references
- internal IDs
- internal lifecycle states
- retry status
- storage structure
- implementation enums

then the design is leaking.

The backend must translate implementation truth into product truth.


## Phase 0.5: Alternative Model Search (Mandatory)

The agent must not commit to the first viable design.

Before proposing architecture, identify multiple plausible domain models and evaluate them.

### 0.5.1 Generate Candidate Models

Generate materially different candidate models.

Continue exploration until the Design Search Exhaustion Gate is satisfied.

Candidate models must differ in at least one of:

- source of truth
- ownership boundary
- lifecycle model
- public contract
- orchestration model
- persistence model
- recovery model

Implementation variations of the same model do not count.

For each model define:

#### Model Name

#### Core Business Fact

What fact is being represented?

#### Ownership Model

Who owns the fact?

#### Lifecycle Model

How does the fact evolve?

#### Strengths

#### Weaknesses

#### Operational Complexity

#### Future Flexibility

---

### 0.5.1.1 Design Search Exhaustion Gate

The agent must continue exploring models until one of the following is true:

1. Dominance Found
   - One model is clearly superior across the primary decision criteria.

2. Trade-off Frontier Reached
   - Remaining models only improve one dimension by worsening another.

3. Constraint Limit Reached
   - Additional exploration requires missing business information.
   - Return ❌ Decision Gate required.

4. Diminishing Returns Reached
   - Two consecutive additional models introduce no materially different:
     - ownership model
     - source of truth
     - lifecycle model
     - public contract
     - orchestration model
     - persistence model

The agent must document why exploration stopped.

---

### 0.5.1.2 No First Viable Solution Rule

The first viable solution is not eligible for selection.

A model may only be selected after:

- alternative models have been explored,
- rejected models have documented reasons,
- the chosen model survives challenge review,
- the Design Search Exhaustion Gate is satisfied.

The burden of proof is on the chosen model.

The agent must assume that the first viable solution is probably not the best solution until proven otherwise.

---

### 0.5.2 Compare Models

For each model evaluate:

| Criteria | Model A | Model B | Model C |
|-----------|-----------|-----------|-----------|
| Ownership clarity | | | |
| Lifecycle clarity | | | |
| Contract simplicity | | | |
| Operational resilience | | | |
| Testability | | | |
| Recovery behavior | | | |
| User-facing simplicity | | | |
| Long-term maintainability | | | |

---

### 0.5.3 Challenge the Chosen Model

For the selected model answer:

- Why is this model better?
- What assumptions make it valid?
- What future change is most likely to break it?
- What is the strongest argument against it?

### What Would Change My Mind?

Before final model selection answer:

- What evidence would make me choose a different model?
- What assumption is most likely to be wrong?
- What future requirement would invalidate this design?
- What is the strongest alternative that almost won?
- Under what conditions would I replace the selected model?

If no credible answer exists:

The design has not been challenged sufficiently.
If no meaningful argument exists against the chosen model:

The design space was not explored deeply enough.

---

### 0.5.3.1 Greenfield Test

Ignore all existing implementation details.

Assume:

- no existing code
- no existing database
- no existing APIs
- no existing providers
- no existing migrations
- no compatibility requirements

Answer:

1. What design would be chosen if building the system today from scratch?
2. Why?
3. Is the selected design identical to the greenfield design?

If no:

- identify the constraint forcing the compromise,
- explain why the compromise is justified.

If the only reason is:

- existing implementation,
- historical behavior,
- compatibility inertia,

then the compromise must be challenged before acceptance.

### 0.5.4 Simpler Model Test

Before selecting a model ask:

> Could this be solved with fewer concepts, fewer states, fewer entities, or fewer ownership boundaries?

If yes:

The more complex model is rejected.

---

### 0.5.4.1 Model Deletion Test

For each major concept in the proposed design ask:

- Can this entity be removed?
- Can this lifecycle be removed?
- Can this ownership boundary be removed?
- Can this persistence model be removed?
- Can this public contract be removed?
- Can this workflow step be removed?
- Can this state be removed?

For every retained concept answer:

- Why must it exist?
- What invariant requires it?
- What breaks if it is removed?

Prefer removal over refinement when correctness is preserved.

A concept that exists only because:

- the current implementation uses it,
- a previous design used it,
- it might be useful later,
- it simplifies implementation,

is not automatically justified.

---

### 0.5.5 Future Burden Test

For each model answer:

> What permanent complexity is being introduced?

Examples:

- additional state
- additional ownership boundary
- additional lifecycle
- additional persistence
- additional public contract
- additional migration burden

Prefer the model that introduces the least permanent complexity while preserving correctness.

---

### 0.5.6 Design Selection Verdict

End with:

Chosen Model:
<name>

Reason Rejected:
- Model A
- Model B
- Model C

Key Trade-off Accepted:
<single sentence>

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

### Consumer Knowledge Test

For each contract:

What must the consumer understand?

Allowed:

- business concepts
- product concepts
- permissions
- outcomes

Forbidden:

- storage details
- provider details
- implementation lifecycle
- orchestration mechanics
- internal state transitions

If consumer understanding requires forbidden knowledge:

⚠️ Contract leak detected.


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



## Phase 3.5: Outcome Verification (Mandatory)

The system is not considered complete until the user outcome is proven.

### User Outcome

Define:

What capability should the user gain?

Not:

- endpoint works
- webhook received
- row created
- event emitted

But:

- tenant has access
- user completed onboarding
- order was fulfilled
- approval was granted

---

### Journey Proof

List every step required for the outcome.

Example:

User intent
↓
Action initiated
↓
External systems respond
↓
State transition occurs
↓
Persistence updated
↓
Permissions updated
↓
UI reflects outcome
↓
User receives expected capability

---

### Proof Required

For each step define evidence:

- logs
- screenshots
- traces
- database state
- API responses
- browser behavior
- external provider evidence

---

### Happy Path Proof

Demonstrate:

End-to-end outcome succeeds.

---

### Failure Path Proof

Demonstrate:

- duplicate requests
- retries
- abandoned workflows
- timeouts
- out-of-order events
- partial failures

do not violate invariants.

---

### UAT Validity Check

Verify test data can actually prove the behavior.

Reject:

- already configured accounts
- pre-existing permissions
- pre-populated state
- fixtures that bypass the real flow

---

### Outcome Completion Rule

The feature is not complete when:

- code compiles
- tests pass
- deployment succeeds

The feature is complete when:

The intended user capability is proven through the real journey.

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

### Design Quality

- First viable solution was not selected automatically
- Alternative models were explored
- Candidate diversity was demonstrated
- Rejected models have documented reasons
- Design Search Exhaustion Gate was satisfied
- Greenfield Test was performed
- Model Deletion Test was performed
- Chosen model was justified against alternatives
- Chosen model states what evidence would change the decision

If any fail:

Return to Phase 0.5 before implementation.

### Tests
For each invariant:
`Invariant → Test → Assertion`

If any fail → refactor before feature work.

---

## Phase 6: Staff Review Verdict

End with exactly one:

- ✅ Proceed — design is appropriate and scoped
- ⚠️ Proceed with constraints — list constraints
- ❌ Decision Gate required — explain why

Hard rule:

If final verdict is `❌ Decision Gate required`, do not write code.

If final verdict is `⚠️ Proceed with constraints`, code may be written only when:
- constraints are explicit,
- model damage is prevented,
- rollback is defined,
- tests enforce the stated invariants.

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