2026-03-23

- After a locator failure reports multiple matches in Playwright, prefer role/table/scoped locators instead of `getByText` to avoid strictness failures.
- When editing repository files in response to user request, use `apply_patch` for edits and do not use shell overwrite patterns like `cat > file`.

- When the user explicitly switches from planning to implementation, verify the active collaboration mode first and move into execution if it is allowed. Do not repeat stale mode blockers after the mode has already changed.

- When investigating config regressions, do not keep pushing environment-variable explanations after the user says the value is stored in the UI/secret manager. Verify the exact read path against the exact write path first.
- For Discord config specifically, distinguish platform-scoped secrets from tenant-scoped secrets and tenant `discord_config`. A value existing in `tenant/<tenant_id>/DISCORD_GUILD_ID` or `tenant.discord_config.guild_id` does not help if runtime code only reads `platform/DISCORD_GUILD_ID`.
- Decision Gate lifecycle rule: once a gate has been answered and cleared, it must never reopen for that issue. Do not reintroduce fingerprint-based reopening logic just because Jira summary/description/labels changed.
- Do not let worker execution enforce Decision Gate through a separate evaluator. Worker gate enforcement must consume the same canonical clarification service and persisted closure state as Discord and Jira.
- Do not thread route-specific booleans like `require_ask_confirmation` or `allow_plain_ask` through transport wrappers as business semantics. Command interpretation policy must be resolved centrally from ingress type so transports cannot drift.
- When the bug is prompt ambiguity, fix the prompt contract directly. Define internal evidence labels like `decision_state` in plain language, bind each label to its authoritative tool, and forbid the model from emitting the label without first calling that tool.
- When persisting metadata into JSON columns or JSONB fields, never store raw `datetime` objects. Serialize timestamps to ISO-8601 at the boundary and add a regression test on the real persistence path.
- If an async HTTP ingress path does heavy synchronous work, move that work off the event loop or slash commands and health checks will time out under webhook bursts. Add a regression test that keeps `/health` responsive while the expensive path is in flight.
- Do not claim a follow-up routing redesign is complete while any transport path still infers reply meaning from Discord config, thread ids, or path-local rewrites. The cutover is only done when gateway, interactions, and followup execution all resolve the same shared follow-up context and the exact live `run -> thread -> plain-text reply` regression is covered by tests.
- Shared follow-up context only works if every ingress path forwards the most precise identifier it has. For Discord gateway messages, native replies in the parent channel must pass `message_reference.message_id`/`referenced_message.id` as `root_message_id`, or parent-channel follow-up replies will misroute even when the context model is correct.
- Decision Gate contract: it runs when a ticket enters the board in To Do, not when `!run` is invoked. The gate must gather missing information using live issue state, project knowledge, and KB auto-answering; if unresolved, it asks the user and updates Jira with the open questions. Once all answers are gathered, `agent_ready` is applied and the gate is finished permanently. `!run` must never reopen or create a new Decision Gate; it can only refuse execution when the issue is not `agent_ready`.
- Do not use a business table like `tenants` as a worker concurrency mutex. If worker coordination needs a lock object, give it a worker-owned table or mechanism so unrelated tenant-scoped writes such as knowledge sync cannot block run claims.
- Jira issue webhooks are triggers, not authoritative execution decisions. Queue issue events, refresh the latest live Jira snapshot in worker context, and reconcile one `(tenant_id, issue_key)` at a time so intermediate webhook deliveries cannot enqueue stale runs.
- When one cleanup helper commits internally and the next mutates additional shared state, do not assume the later mutations will persist automatically. If a webhook path updates shared follow-up rows after an earlier helper commit, explicitly commit the second phase or the response can claim cleanup succeeded while the database state stays stale.
- When a race/replay fix depends on serialized webhook processing, do not stop at the first transport that showed the bug. Audit every webhook ingress path and either move all of them onto the same queue contract or document why a path is intentionally exempt.
- When optimizing shared build/runtime infrastructure, do not assume a heavyweight toolchain is unused just because the common code path does not reference it. Check the actual service contract for each compose target before removing Android, voice, or platform tooling from a worker image.
- Do not downgrade Jira reporting, cleanup, or follow-on reporting to optional side effects once the user says they are part of the run contract. If a step is mandatory, keep it on the critical path, log it explicitly, and make terminal status depend on it.
- Keep transport layers thin. Do not put business logic like follow-up scope recovery, ask-history pruning, or decision behavior into transport/runtime wrappers; keep that logic in the brain/application layer and pass transports only simple wiring.
- Do not tell the user a large implementation plan is complete until you have checked each original plan section against the code and called out any remaining unverified or unimplemented items explicitly.
- When queueing webhook work, do not smuggle command-routing policy through transport-specific context flags. Queue the raw payload and let the worker/application layer derive behaviors like `issues seed` deferral from the canonical command parser.
- Do not restore legacy config-backed runtime state just to make a failing flow pass. If the canonical store is `FollowupContext`, finish the cutover there and migrate the tests/runtime to that model instead of reintroducing `tenant.discord_config` fallback reads.
- GitHub PR remediation must have one explicit command path. Do not let generic `pull_request_review_comment` events auto-trigger remediation in parallel with manual-fix parsing; require the same explicit manual-fix command parse for review comments and issue comments, and keep that decision in the shared policy layer.
- When removing a product behavior, delete the now-unreachable transport actions, helpers, and tests in the same change. Do not stop after removing the production caller and leave dead code behind.
- When the bug is caused by a shared lifecycle pattern like "enqueue then patch", do not band-aid one caller. Rewrite the shared creation contract so the row is fully initialized before visibility, then migrate every caller off the unsafe pattern in the same change.
- Do not defer critical stage artifacts like PM plans, dev results, test feedback, or review output to end-of-run finalization. Persist each stage checkpoint as soon as that stage completes so reruns and diagnostics survive later completion/finalization failures.
- If a scheduled automation is intended to run only when work exists, enforce that contract explicitly with a fast precheck before it takes a constrained runner. Do not rely on the main job to no-op after it has already been queued.
- When replacing a status surface like a GitHub comment with reactions, verify the replacement exists end-to-end for the exact target object. Do not remove the old path until the new PR/comment reaction path is implemented, ordered correctly, and covered by production-path tests.
- When a user says the problem is stage handoff, do not jump to tool restriction theories. Trace the orchestrator boundary first: confirm whether the parent worker stayed alive long enough to persist the stage result and launch the next stage.
- Admin UI changes need a real behavior test harness, not just lint/type checks. If run-state rendering or stage telemetry behavior is in scope, add executable UI behavior coverage before claiming the fix is complete.
- Auth.js, onboarding, registration, and tenant-settings UI changes need executable browser coverage before PR creation. Do not treat lint/type checks or backend tests as substitutes for user-flow verification.
- Do not leave `ANN001` or `ARG001` debt in touched files. Add the missing types or rename/remove intentionally unused arguments before calling the work done.
- If a file shows an unexpected unrelated diff after a merge, inspect why before "restoring" it. Do not reintroduce duplicate or invalid config just to make the diff disappear.
- When a user calls out business logic still living in a webhook or transport module, do not stop at defending the current wrapper thickness. Finish the extraction into a core/application service and leave the transport file as dependency wiring only.

2026-03-27

- When a user narrows config scope from tenant-level to platform-wide admin control, stop planning tenant policy/UI work immediately. Verify the existing platform admin surface first, and align the design to that control plane before expanding the schema.
- When a merge exposes a missing compatibility seam, do not default to restoring the seam if it weakens the architecture. First check whether the behavior should instead flow through the canonical shared policy path, then update tests/helpers to use that path.
- In a multi-worktree repo, verify the active git branch before editing. If the user says the work belongs on a specific branch, switch to that exact worktree first instead of starting implementation in the current cwd and moving it later.
- When a user points out that context matching is being "drip fed" across weaker fallbacks, treat that as an architectural bug, not just a resolver bug. Replace staged channel/message fallback with one composite lookup that evaluates every available identifier together and refuses ambiguous matches safely.
2026-03-28

- When running Playwright against a Next app in a worktree, execute the test from the app directory or pass the explicit config file. Relative `page.goto()` calls only work if the runner actually loaded the correct `playwright.config.ts` and baseURL.
- Before merging or rebasing another branch (for example `origin/staging`) into a feature branch, **commit WIP on the feature branch first**, even if the message is `WIP:` or `chore: checkpoint`. Do not treat `git stash` as the primary way to preserve work before a merge: stash is for short-lived context switches, not as a substitute for commits. Committed WIP stays in history, is easy to diff and restore, and is not confused with throwaway state.
- Migration fixes are not verified by SQLite-only Alembic tests alone. If the runtime path is Postgres via Docker startup, add regression checks for legacy version-table normalization and boolean/default DDL compatibility, then prove the real startup script reaches service readiness and `worker_started`.
- When restructuring an admin access surface, verify all critical account-lifecycle actions remain available: invite, deactivate, role change, and password reset. Do not call the UI complete until those operator actions are present or explicitly deferred.
- Do not surface raw permission keys, enum values, or internal identifiers in enterprise UI. Translate access models into plain-language labels and use real selectors for team assignment instead of asking users for IDs.
- Do not duplicate personal preference controls inside admin management flows. If a setting belongs to a user profile, keep it there instead of surfacing a second copy in invite or member-management forms.
- Do not treat mocked UI success as proof that an auth or invite flow works. Exercise the real backend persistence path for registration, invite acceptance, and login before calling the flow done.
- When a browser flow reports `Failed to fetch`, verify the real browser network path and prefer same-origin app proxies for public flows like registration and invite acceptance instead of relying on cross-origin direct fetches.
- Do not treat mocked Playwright routes as sufficient proof for registration, invite acceptance, or login. Critical UI workflows must also have real-backend browser tests, including at least one visible failure path.
- Enterprise onboarding should read like a guided workflow, not a marketing landing page. Prefer explicit steps, short utility copy, and one action per stage over summary panels like “What happens next”.
- In multi-step onboarding, do not reset the current step off a generic membership/profile refresh. Initialize the wizard from stable identity changes such as membership id, and keep preference saves from bouncing users back to step one.
- Do not claim invite-to-onboarding coverage unless the live browser test completes the state-changing finish action and exits the wizard. Reaching the onboarding page or an intermediate step is not sufficient proof.
- In a wizard step, keep navigation unambiguous: one clear back action and one clear forward action. Any integration-specific action inside the step must be singular and clearly secondary.
- Do not show an integration step in onboarding when the tenant has not configured that integration. Optional setup belongs in the flow only when the prerequisite tenant configuration actually exists.
- For onboarding and tenant-selection flows, prove the post-action destination page is usable, not just that the URL changed. The browser test must click through, wait for the destination workspace, and assert there is no auth/permission error banner on that page.
- Keep auth browser tests aligned to the current auth contract. When Auth.js/BFF replaces legacy localStorage auth, rewrite the tests to cover real login and protected-route redirects instead of preserving stale token-storage assertions.
- Do not place read-only role or state fields inside an editable settings form. If a user cannot act on a value, move it into summary context or remove it from the form surface entirely.
- Keep account settings and security settings as separate tabs. Password changes should not share the same form surface as profile and preference editing.
- When a user narrows a bug to a specific persona or workflow, reproduce that exact path before diagnosing. Do not inspect the tenant-owner flow when the reported failure is in the invited-user flow.
- Setup wizards need the same design bar as the core product. Do not ship a generic bordered card flow for enterprise setup when the user is asking for deliberate, premium workspace UI.
- Do not expose tenant navigation links to users who cannot open the target page. If a route is permission-gated, the sidebar and tab navigation must be gated by the same permission check so invited users do not land in avoidable 403 screens.
- When an integration action depends on platform-level configuration, expose that availability explicitly in the API and hide the action in the UI when unavailable. Do not render a live button that can only fail with a backend configuration error.
- Do not use principal type as a shortcut for capability. Platform super admin is an access bypass, not a restricted persona; tenant navigation and security actions must stay available unless the product explicitly says otherwise.
- All accounts in this product are password-backed. Do not invent account classes that cannot change passwords, and do not gate password-management UI on principal type.
- When testing password reset or other email flows from FastAPI routes, patch the symbol actually imported by the route module. Patching the original helper module is not enough once the function has been imported into the endpoint file.
- After adding new Next app routes or auth flow files, restart the live admin UI before treating a `404` as a code bug. A stale dev server can invalidate browser conclusions.
- A shared login screen must not prefill platform-admin credentials by default. Keep the identifier blank and let tests or fixtures supply admin input explicitly.
- Do not surface platform-only actions or language inside tenant onboarding. Workspace setup for a newly registered tenant owner must stay tenant-scoped and never suggest platform secrets or super-admin concepts.
- Do not double-constrain setup flows with both a narrow route wrapper and a narrow inner content shell. Major onboarding/workspace setup surfaces need room, and step rails must not repeat the same title and explanatory copy already shown in the main work area.
- Team permissions must map to real, user-visible capabilities. Do not expose permission choices that unlock nothing, and do not hide basic workspace/project visibility behind elevated team permissions.
- Keep tenant team permissions enterprise-simple. Prefer a small set of clear capability bundles such as workspace, people, projects, and technical access instead of long lists of low-level internal permission keys.
- For destructive or state-changing UI actions such as archive, the browser test must click the real button and assert both the reaction and the destination page. Verifying only API payloads or local state is not enough.
- When a selector or dashboard is meant to show active entities, do not assume the backend already filtered them. Add an explicit UI regression that proves archived/disabled records disappear from the rendered active list after the real archive action.
- For destructive tenant-level actions, do not jump straight to an automatic redirect. Show a confirmation state with one explicit exit CTA, and make the browser test click that CTA before asserting the final destination.
- If the action invalidates the current workspace shell, the confirmation must live on a standalone route outside that shell. Do not keep stale tenant navigation visible after archiving the workspace it belongs to.
- When an entity is archived, do not make it disappear entirely if the operator still needs a recovery path. Move it into a clearly labeled archived section with the correct next action instead of pretending it no longer exists.
- Long selector surfaces should page or window their lists. Do not let admin selectors grow into unbounded vertical dumps once test data or production data accumulates.
- For tenant nav links, add at least one real-browser click test for the actual route transition. Mocked page assertions are not enough to prove sidebar navigation works in the live app.
- Destructive admin actions must live in a dedicated danger zone, not in shared page headers. If archiving removes access to a tenant or project, require typed-name confirmation and test the real browser click path through the confirmation state.
- When replacing a route contract, do not leave legacy redirects behind unless the requirement explicitly calls for them. Update every live caller and test to the new URL structure and delete the stale route tree in the same change.
- For permission bugs, do not fork API contracts with temporary parallel endpoints. Keep one canonical endpoint and fix authorization rules there so UI routing and tests stay consistent.
- When the product says API permissions are role/team-linked, do not stop at raw membership existence checks. Express the contract through a shared authorization helper tied to the membership model, then reuse that helper across every endpoint in that surface.

- When a user asks for hosted service status, build a dedicated platform status surface. Do not reuse tenant settings or integration diagnostics for platform operations.
- When a user asks to remove a URL segment and rejects legacy redirects, make it a real route-tree cutover: move the route files, update helpers/nav/tests together, and delete alias redirect behavior instead of hiding the old contract behind redirects.

- When a bug report says the persona-specific live click path is still broken, do not rely on a nearby automated flow as proof. Reproduce that exact user journey in-browser, including the actual click target and expected visible data, before claiming the fix.
- When adding new runtime providers or provider capabilities, verify the capability matrix explicitly. Do not assume `reasoning_effort`, model catalogs, or profile validation match user expectations just because the runtime kind was added elsewhere.
- For admin configuration surfaces, API tests are not enough. Verify the real save path in-browser for create and update flows, including runtime-specific required fields and defaults.
- For admin forms with persisted overrides, save-path tests are still insufficient unless they also verify reload/edit-state hydration. If a control must reflect saved state, reload the page or re-open the saved record and assert the control value from API-backed data, not local draft state.
- When a feature needs runtime profile routing, do not assume the downstream Codex call honors it just because it eventually invokes Codex. Trace whether the call path uses `build_runtime_for_selector` with the correct selector and named-agent identity; otherwise the flow will silently ignore runtime-profile configuration.
- For shell-based feature flags, do not pass `0` into a downstream script that only checks for non-empty truthiness. Map falsey values to an unset variable or a branch that omits the flag entirely, otherwise you will accidentally force the expensive path anyway.

2026-03-30

- When integrating third-party APIs, never introduce new API keys as env vars; resolve keys from the tenant secret manager using a scoped secret ref (for Stitch: `tenant/<tenant_id>/STITCH_API_KEY`).

2026-04-07

- When a user provides a bare UUID and asks about "our run", do not assume an external tracker object just because the identifier looks like a Linear UUID. First search local run storage and repo databases for the ID or prefix, then only branch into external systems if the user explicitly says it is a tracker record.
- When the user narrows implementation scope to a specific slice or ownership boundary, immediately re-scope the plan and edits to that slice instead of continuing the broader refactor from prior context.
- When the user narrows implementation scope to a specific slice or file ownership boundary, stop the broader rollout immediately and restate the constrained scope before making more changes. Do not keep building adjacent slices just because the larger plan exists.
- When replacing a legacy execution model, do not preserve old fields or constructor shims just to keep existing tests green. Remove the legacy path, then update fixtures, services, and tests to create valid objects under the new model and use the full suite to close the remaining gaps.
- When a user says a production bug is still not fixed, trace the exact live runtime path before claiming coverage. Hidden fallbacks, especially implicit resume/session reuse, count as the bug still being open even if the surrounding architecture was refactored.
- When the user rejects lazy normalization and asks for an actual move, do not leave read-path repair in place. Replace it with a one-time data migration and remove the lazy mutation hook from normal reads.
- When diagnosing project-scoped config or secrets, do not answer in terms of the internal `project_id` alone. Always map it back to the visible project key/name the user is referring to before explaining the issue.
- Do not expose obsolete internal checkpoint kinds in admin restart/resume controls. If workflows actually rerun from `pm` and `execution`, remove `orchestrated` from the user-facing API/UI contract instead of leaving a 409-prone dead option.
- When a user says "start from the start", do not reinterpret that as "restart from the earliest available checkpoint". Treat it as a fully fresh run with no checkpoint resume unless the user explicitly asks to restart from a checkpoint.
- When a user says the contract belongs in the tool description rather than the prompt, stop editing prompts and move the change to the tool surface. Do not solve the right behavior in the wrong layer.
- When the user asks for workflow prompts to treat the tool base as part of diagnosis, add a general exploration rule for blockers and missing evidence. Do not turn that into a mandate for one specific tool unless they ask for that explicitly.
- Before claiming a tool-description fix will affect agent behavior, verify that the stage prompt actually receives the structured tool catalog rather than a flat name-only list. Description work is wasted if the renderer strips it out.
- When fixing secret or PII exposure in logs, trace every persistence boundary before coding. Raw runtime sinks, DB log normalizers, and telemetry/logger save paths all need the same redaction policy; patching only one path leaves the leak open elsewhere.
- When the user asks for a friendlier failure contract, fix the shared formatter and stage-event builder, not one specific run explanation. Operator guidance belongs in the reusable failure output surface.
- When the user says failure troubleshooting should be generated in the stage output rather than hard-coded in the notification layer, move the requirement into the stage prompts and keep the formatter generic.
- When diagnosing workflow tool bypass, distinguish installed plugins from MCP server access. The correct worker-runtime boundary is "plugins may remain available, direct MCP must be disabled"; do not propose disabling plugins when the user only wants MCP removed.
- Do not hide missing third-party dependencies with production import fallbacks when the user asks to fix the import. Make the dependency available in the test/runtime environment and keep the real import path intact.
- When the user broadens a failure-handling fix from one stage to all workflows, stop optimizing for the first stage-specific symptom. Patch the shared runtime or workflow contract so tool misses degrade gracefully everywhere instead of adding another PM-only rule.
- Do not auto-cancel workflow runs from Jira reconciliation when the user wants manual cancellation only. Webhook status churn should affect enqueue decisions and notifications, not terminate in-flight or queued runs implicitly.
- When the user asks to simplify a workflow/result contract and remove legacy behavior, delete the old compatibility shims in the parser/model at the same time. Do not keep `passed`/`approved`/`succeeded` compatibility paths alive just because they make existing tests easier to preserve.
- When the user asks for strong typed contracts, do not keep tolerant runtime normalization (lowercasing, trimming, alias mapping, or `str(...)` coercion) in decode paths. Keep runtime strict and move all compatibility handling into explicit one-time migration tooling.

2026-04-08

- When the user says "fix the issue, don’t add a fallback", correct the source contract at the producer boundary and remove downstream fallback logic instead of preserving dual-path behavior.
- For human-in-the-loop workflows, persist pause/resume state first and only then execute external side effects (Discord send, enqueue visibility). Resume must be one transaction that consumes the answered request and creates the resumed attempt together, or duplicate resumes will occur after crashes.
- OAuth install callbacks must treat provider cancel/error responses as first-class outcomes. Do not require success-only query params like `guild_id` in the route signature or cancellation will return framework 422s instead of a user-facing redirect state.
- Setup wizard steps must avoid mixing unrelated concerns (for example bot install and invite policy). Keep each step scoped to one decision and move secondary configuration to a dedicated later surface.
- When the user asks for a new wizard step, implement the concrete step and wiring (state, API calls, navigation), not explanatory placeholder copy.
- For step-based app routes, never hardcode allowed step keys in route guards. Derive them from the canonical step registry (`STEP_ORDER`) so newly added steps cannot 404.

2026-04-09

- When a worker parent decides whether to spawn child processors, never use a looser "any queued row exists" check than the child uses to claim work. Share one typed claimability contract between the supervisor preflight and the claim path or the runtime will churn, misreport idleness, and hide the real blocker.
- When the user says a live orchestration bug is still happening after a refactor, do not stop at passing regression tests and a rebuilt worker. Re-validate against the exact live run id and current queue state before claiming the runtime issue is resolved.
- When the user asks to fix all architecture findings in one pass, include contract-level tests for each seam and run the broad affected suite before closing; targeted green tests are not enough if neighboring lifecycle contracts can still regress.
- When the user asks to push a fix for a specific branch or PR, verify the current branch and target PR immediately before commit and push. Do not infer the destination from the most recently edited files or from the current cwd alone.
- When a stacked PR suddenly shows unrelated changes, verify the PR base branch before changing code. A wrong base can make scope look contaminated and can send CI down the wrong path.
- When the user says to remove source-based execution shortcuts, delete source-override fallback paths entirely and require persisted state at the decision boundary.
- When the user asks for strict canonical keys, do not normalize aliases/casing/punctuation in runtime parsing; accept only exact canonical values and test that non-canonical inputs are rejected.
- Worker execution must not block on GTD/decision state once execution readiness has been satisfied; worker-stage gating may only enforce live execution-readiness prerequisites, not PM clarification semantics.
- When the user asks for dead-code cleanup or shim removal policy, implement enforceable repository gates (CI scripts/tests + fail conditions) in the same change instead of only describing manual review steps.
- When a user asks for a boundary refactor, do not keep compatibility via module-level alias shims in the caller. Introduce a typed port/adapter and inject it through the wiring seam so command handlers depend on explicit contracts, not patched globals.

2026-04-10

- When the user says fallback behavior is not acceptable, remove fallback synthesis at the core contract boundary (not just in one transport), and add assertions that canonical fields must be present instead of silently deriving replacements.
- When the user asks to revert a specific post-commit change, isolate that exact delta before acting. Do not revert adjacent optimizations or earlier committed work that the user did not name.
