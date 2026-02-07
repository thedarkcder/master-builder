# Decision Gate Rules

## Required sections
- Objective
- Scope
- Acceptance Criteria
- How to test

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
- Who is the final decision owner for unresolved trade-offs?

## Tags
- [NEEDS-PM]
- [NEEDS-BA]
- [NEEDS-SECURITY]
- [NEEDS-ENG]

## Messages
- clear_reason: Decision Gate not required
- blocked_recommendation: Block implementation until PM/BA resolves Decision Gate.
- clear_summary: Decision Gate clear: no blocking ambiguity detected.
- blocked_title: Decision required before build
- missing_sections_prefix: Missing GTD sections
- ambiguity_prefix: Ambiguity markers found
- options_line: Options: A) MVP quick test, B) Scale-ready production design
