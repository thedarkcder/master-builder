# Public setup and quality follow-up review

## 0. Reality model
Facts: maintainers want seven publication recommendations addressed. Installers own their local configuration; credentials must be independently generated. A successful real sign-in proves access; mocked responses and a health response do not. Observations: host and container addressing differ; container UI currently inherits a host port and public backend address. Artifacts: isolated validation environments are disposable and never production truth. Lifecycle: unconfigured → configured → migrated → ready → authenticated → signed out; each transition requires actual service/browser evidence. Missing evidence leaves a check unverified; repeated validation uses a new isolated environment. Real evidence also found authorization ownership lost between transactions. An authenticated actor remains the same actor throughout one owned operation; committing durable work must not erase that fact. Root cause: contract/API failure. Anti-patching verdict: ✅ Model understood — proceed to Phase 1.

## 0.5 Design search
A: retain public build-time backend address for server requests; rejected because container reachability is a different fact. B: explicit runtime server address and independently public browser configuration; chosen. C: rewrite UI architecture to eliminate all proxies; rejected as unnecessary permanent change. B dominates ownership, testability and rollout simplicity. For transaction identity, per-route resets were rejected because they duplicate ownership across every commit; connection-global settings were rejected because they leak across pooled consumers. Session-owned claims reapplied transaction-locally preserve one operation owner and fail closed for new sessions. The project session factory uses an owned Session class: closing, resetting or invalidating clears its claims, including same-object reuse; commits and rollbacks retain the operation owner. Greenfield uses explicit service addressing. Deletion test retains the server boundary because browser and service networking differ. Missing configuration must fail clearly. Evidence that would change this choice: all consumers become browser-only.

## 1. Problem
A new contributor cannot yet prove secure sign-in using the documented installation contracts.

## 2. Type
Reliability / correctness concern.

## 3. Invariants
- No existing configuration, data, worker authentication or shared container is altered by validation.
- Real UI authentication reaches the actual migrated PostgreSQL-backed API.
- Missing server addressing fails clearly; no legacy environment fallback.
- Formatting changes preserve behavior and existing human edits.

## 4. Assumptions
The source checkout is the initial distribution contract. Local disposable infrastructure and anonymous synthetic test users are authorized by this review. Optional hardware/provider capabilities require independent evidence.

## 5. Contract matrix
Host UI uses configured loopback server address; container UI uses configured internal address. Missing/invalid address fails configuration validation. Browser/public URLs stay separately configured. Formatting has no intended behavioral change.

## 6. Call-path impact scan
Auth.js credentials login, BFF and public registration/invitation/reset proxy routes share the server address. Both general and workflow engine session factories use the same operation-owned identity lifecycle. Browser clients retain public configuration. Compose runtime port must match published container port.

## 7. Domain term contracts
Ready means migrated service responds; authenticated means browser session has actual permissions. Supported means specifically verified platform/capability, not merely present code.

## 8. Authorization & data-access contract
Real generated administrator and synthetic tenant accounts; no prepopulated session cookies. PostgreSQL proof uses a non-superuser/non-BYPASSRLS role separate from migration ownership. No production data.

## 9. Lifecycle & state matrix
Invalid configuration → explicit failure. Disposable services terminate and their owned volumes/configuration are removed after verification. Failed evidence is retained without secrets.

## 10. Proposed design
Explicit server runtime URL; container port/address wiring; real browser journey; separately documented formatting baseline and evidence.

## 11. Patterns used
Configuration validation and existing proxy boundaries. Reject automatic URL inference because it hides network ownership.

## 12. Patterns not used
No compatibility shim, new authentication model or architecture rewrite.

## 13. Change surface
UI auth/proxy configuration, Compose UI wiring, templates, validation tools/tests and public setup documentation. No persisted application schema changes for the address or session-lifecycle correction. Session identity is ephemeral operation evidence, not a new stored credential.

## 14. Load shape & query plan
Existing login and proxy requests remain one backend request per operation; validation traffic is bounded and synthetic.

## 15. Failure modes
Missing setting, unreachable service, migration or browser failure produce explicit check failures. No fallback URL or swallowed validation error.

## 16. Operational integrity
Source changes are reversible; deployed environments must set the new server variable explicitly. Ephemeral service names/ports and volumes are isolated. No global Docker prune or secret output.

## 17. Tests
Address validation tests; real browser administrator sign-in and invalid password; PostgreSQL RLS contract, tenant separation, commit/rollback identity and pooled-connection leakage regression; formatting/lint, build and available regression suites.

Fresh outcome verification (2026-10-02): frozen Python/npm installs from a public candidate snapshot passed; generated private configuration, actual owner/runtime provisioning and migration0136 passed; runtime role reports NOSUPERUSER/NOBYPASSRLS and can read newly migrated tables through owner default privileges. Real API health,17UIunit tests/types/build and6actual browser login/reset/registration/invite journeys passed with zero retries. Final isolated PostgreSQL runner:5passed,0failed,0skipped, including workflow factory, same-session close/reset/invalidate and actual altered-search-path ownership rejection. Full Python suite still fails at missing implicit default-project expectation (1failed,6passed); complete backend clearance remains blocked. Owned generated credentials/logs/browser snapshots and service containers were removed; non-sensitive aggregates retained. Host migration command explicitly selects owner credentials. Core app-runtime-base image compilation reached layer export but export was canceled for insufficient disk headroom; complete API/UI container startup remains unverified. Only attributable unshared build cache IDs were removed. No production migration or remote mutation occurred.

## 18. Verdict
✅ Proceed — design is appropriate and scoped
