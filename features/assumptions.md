1. GTD is keyword-based

  - orchestrator/core/gtd.py
  - “Good To Do” passes/fails by substring checks (objective, scope, test, mvp, etc.), not semantic understanding.

  2. Decision Gate is rules + marker matching

  - orchestrator/core/decision_gate.py
  - Required sections + ambiguity markers (including unknown) are string/rule driven, so wording can retrigger gate.

  3. Blocked is text-prefix driven

  - orchestrator/core/workflow/runner.py
  - Any stage output line starting with Blocked: hard-fails that stage/run.

  4. Webhook run start is gated to To Do

  - orchestrator/api/webhooks/jira_ingress.py
  - If Jira status isn’t treated as To Do, webhook path won’t enqueue.

  5. Decision-gate cooldown still exists on webhook ingress
  - It injects/updates a decision-gate clarification block and may add fallback text like “Provided in thread reply.”

  10. Workflow runtime cap still exists (separate from Codex command timeout)
