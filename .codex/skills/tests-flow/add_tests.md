# Skill: Add/Update Tests

## Goal
Ensure behavior changes are covered by tests.

## Approach
- Prefer unit tests for logic.
- Use integration tests when behavior spans components/services.
- For React, use React Testing Library and test user-visible behavior.

## React specifics
- Use `getByRole` / `findByRole` and `waitFor` for async.
- Never use `setTimeout`/sleep to wait for UI state.
- Mock network calls using MSW if present.

## Output
- List new/updated tests.
- Explain what behavior is covered.
