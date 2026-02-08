---
name: ux-principles
description: Apply universal UX principles consistently across flows, forms, dashboards, and feedback states.
---

# Skill: Universal UX Principles

## When to use
- Any frontend or UX change.
- New flows, forms, dashboards, or navigation updates.
- Refactors that could affect layout, interaction, or accessibility.

## Required principles
1) Consistent structure:
- Maintain uniform layouts.
- Similar tasks should follow the same structural pattern (modular steps or single cohesive flows).

2) Clear navigation:
- Users should always know where they are.
- Use clear headings, breadcrumbs, or step indicators.

3) Predictable feedback:
- Alerts, errors, and confirmations should always appear in a consistent location.

4) Responsive design:
- Layouts adapt on all screen sizes.
- On mobile, use full-width or stacked components.

5) Consistent spacing and typographic hierarchy:
- Keep spacing scales and typography hierarchy consistent.

6) Accessible interactions:
- Keyboard accessible interactions are mandatory.
- Ensure visible focus states and sufficient contrast.

7) Context retention:
- Multi-step flows must retain user input and context.
- No unexpected resets between steps.

8) Guided user flow:
- Provide clear next steps and progress indicators.

9) Full-width for data-dense interfaces:
- Use full-width layouts for admin dashboards and data-heavy screens.

## Execution checklist
- Verify desktop and mobile layouts.
- Verify feedback location is consistent.
- Verify keyboard navigation and focus visibility.
- Verify step/flow data persists when navigating between steps.
- Verify data-heavy pages are full-width and readable.

## Output expectations
- Mention UX checks performed in PR summary.
- Include explicit mobile + keyboard-accessibility validation in How to test.
