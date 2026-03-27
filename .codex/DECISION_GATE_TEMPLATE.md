# Decision Gate (Ask Before Building)

Use this when requirements are unclear or design trade-offs matter. Post to Jira + Discord (signal-only).

## Why a decision is required
- (1–2 sentences describing the ambiguity or trade-off)

## What we’re trying to achieve
- Outcome:
- Who it impacts:
- Constraints (security/compliance/cost/timeline):

## Options (pick one)
### Option A — MVP / quick test
- What it means:
- Pros:
- Cons / risks:
- What we are explicitly not doing (out of scope):

### Option B — Scale-ready / production grade
- What it means:
- Pros:
- Cons / risks:
- What it requires (infra, observability, reliability, cost):

## Questions (max 5)
1)
2)
3)
4)
5)

## Recommendation
- (One line: recommend A or B and why)

## Tags
- [NEEDS-PM]
- [NEEDS-BA]
- [NEEDS-SECURITY]
- [NEEDS-ENG]

## Runtime Rule Source (Machine Readable)
The orchestrator parses the sections below directly from this file.

## Required sections
- Objective
- Scope
- Acceptance Criteria

## How to Automated Test
- Unit:
  - command(s)
  - what logic is covered
- Integration:
  - command(s)
  - what boundaries and wiring are validated
- UI automation / E2E:
  - command(s)
  - what user journey is covered
- Evidence:
  - demo video or screenshots if the change is user-visible

### Test expectations
- Automated tests must prove the behavior a QA engineer would otherwise verify manually.
- For user-facing changes, the acceptance path must be covered by automated UI tests unless explicitly justified.

## NFR markers
- mvp
- scale-ready
- scale ready

## Ambiguity markers
- tbd
- to be determined
- unknown
- clarify
- decide later
- ???

## Resolution questions
- Should this run target MVP quick delivery or scale-ready design?
- Which reliability/security constraints are mandatory for this scope?
- What is explicitly out of scope for this iteration?
- Are there rollout or migration constraints that affect implementation?

## Messages
- clear_reason: Decision Gate not required
- blocked_recommendation: Block implementation until PM/BA resolves Decision Gate.
- clear_summary: Decision Gate clear: no blocking ambiguity detected.
- blocked_title: Decision required before build
- missing_sections_prefix: Missing GTD sections
- ambiguity_prefix: Ambiguity markers found
- options_line: Options: A) MVP quick test, B) Scale-ready production design
