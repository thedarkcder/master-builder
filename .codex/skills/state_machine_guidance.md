# Skill: Use State Machines (Avoid Racy Sleeps)

## When to use
- Multi-step workflows
- Async processes (waiting for external systems)
- UI flows with multiple states
- Any “wait then retry” logic

## Pattern
1) Define explicit states:
   - e.g. CREATED, VALIDATING, SUBMITTED, AWAITING_RESPONSE, COMPLETED, FAILED
2) Define events that cause transitions:
   - e.g. VALIDATION_OK, SUBMIT_OK, RESPONSE_RECEIVED, TIMEOUT, ERROR
3) Make transitions pure and deterministic:
   - next_state = transition(current_state, event)
4) Put side effects at the edges:
   - calling APIs, writing DB, emitting events
5) Persist state for long-running workflows.

## Anti-patterns
- Thread.sleep / time.sleep / setTimeout to “let it settle”
- polling loops without clear bounds/backoff
- hiding state in timers

## Output
- Document the state diagram in the PR description briefly.
- Add tests for transitions and key states.
