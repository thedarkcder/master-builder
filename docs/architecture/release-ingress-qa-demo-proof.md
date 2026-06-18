# Release Ingress Strategy for QA Demo Proof

## Objective

Define the release ingress contract Master Builder should use so QA demo recording can prove real browser, iOS, and Android releases without relying on public DNS resolution from the worker environment.

## Problem

QA demo proof needs to record against the real release created by Master Builder. The release may have a generated public hostname, but workers and mobile simulators do not always resolve or reach that hostname from their runtime context. DNS convenience must not be treated as proof that the release is reachable.

## Recommendation

Use an owned ingress layer, such as Caddy or the current MB-controlled proxy, as the mandatory runtime contract for release proof.

- Generated public hostnames are still allowed for human-facing links.
- QA workers should connect to a known ingress URL and send the generated release hostname as the `Host` header.
- Browser, iOS, and Android recorders should receive both the public evidence URL and the worker-reachable recording URL.
- The public URL remains the evidence identity attached to PRs.
- The recording URL is worker execution plumbing and must not change the meaning of the release being proved.

## Scope In

- Define Caddy/owned ingress as the source of truth for QA demo route execution.
- Keep generated release hostnames stable and deterministic.
- Support browser QA recording through ingress URL plus `Host` header.
- Extend the same ingress contract to iOS and Android recorders.
- Preserve public release URLs in PR evidence and release metadata.
- Add tests that prove recorders use the internal ingress path while validating the public release identity.

## Scope Out

- Desktop recording support. This remains a later computer-use based implementation.
- Replacing all existing preview-domain generation in one step.
- Public TLS/certificate automation for shared production ingress unless explicitly included in a separate deployment hardening ticket.

## Design Notes

`sslip.io` is DNS convenience only. It maps names like `feature.192-168-1-7.sslip.io` to an IP address, but it does not own routing, route health, headers, TLS, or worker reachability.

Caddy or another MB-owned ingress is the better release proof contract because MB controls routing, health checks, route activation, and how workers reach the release.

Recommended shape:

- Local/dev: generated hostnames under an MB-owned local namespace, routed through Caddy/owned ingress.
- Shared environments: wildcard DNS for an owned domain pointing at the ingress/load balancer.
- QA recorders: connect to the known ingress endpoint and send the generated hostname as `Host`.

## Acceptance Criteria

- Given a release has a generated public hostname and an internal ingress URL, QA demo health checks use the internal URL with the public `Host` header.
- Given browser demo recording starts, Playwright navigates through the worker-reachable ingress URL while enforcing the public preview origin as the allowed release identity.
- Given iOS and Android demo recording starts, native recorders receive release context containing public evidence URLs and worker-reachable ingress URLs.
- Given demo evidence is attached to a PR, links and metadata reference the real release identity and commit SHA.
- Given the generated public hostname is not directly resolvable from a worker, QA demo recording still succeeds through owned ingress.
- Given the owned ingress is unavailable, QA demo recording fails hard with a clear error and does not attach fake or partial proof.

## How to Test

- Add unit tests for release service URL payloads containing public URL, recording URL, and recording host header.
- Add integration tests for QA demo readiness checks using `internal_url + Host`.
- Add browser recorder tests proving navigation uses `recording_url` and rejects navigation outside the approved release origin.
- Add iOS and Android recorder tests proving launch context includes public and recording URLs.
- Run a real MB run against a release with QA demo enabled and verify browser, iOS, and Android recordings are uploaded and attached to the PR.

## Jira Backlog Draft

### Parent Feature

**Summary**
- Standardize release ingress for QA demo proof across browser, iOS, and Android

**Objective**
- Make Master Builder release proof deterministic by using an owned ingress contract instead of depending on public DNS reachability from worker environments.

**User / Business Value**
- Every ticket can produce real demo evidence against the actual release.
- QA evidence is reliable across browser and mobile workers.
- Reviewers can trust that demo links prove the delivered feature, not a fake or manually assembled page.

**Recommendation**
- Make Caddy/owned ingress the mandatory release proof path.
- Treat generated public hostnames as release identity and human-facing links.
- Treat internal ingress URLs plus `Host` header as worker execution plumbing.

**Scope In**
- Browser, iOS, and Android QA demo recording.
- Release readiness checks through owned ingress.
- PR evidence links that preserve public release identity.
- Hard failures when ingress proof cannot be established.

**Scope Out**
- Desktop recording.
- Full public TLS rollout.
- Replacing all environment naming conventions outside release proof.

**Acceptance Criteria**
- QA demo proof does not depend on direct `sslip.io` or public DNS reachability from workers.
- Browser, iOS, and Android recorders can run against the same release ingress contract.
- PR evidence includes playable recording links and release commit metadata.
- Failure to reach owned ingress blocks proof instead of producing partial/fake evidence.

**Success Outcomes**
- MB can record demos for browser, iOS, and Android from a real release.
- Generated public names remain stable for humans.
- Workers use a deterministic route path owned by MB.

**Dependencies / Risks**
- Caddy/owned ingress route generation must be deterministic.
- Mobile simulators may need platform-specific host mapping.
- Shared environments may still need wildcard DNS for external human access.

**Open Questions**
- What owned domain should shared preview environments use?
- Should local/dev use `.test`, `.localhost`, or a configured MB namespace?
- Should Caddy replace the current proxy immediately or be introduced behind the same contract?

**Good To Do checklist**
- Objective is clear.
- Acceptance criteria are defined.
- Browser, iOS, and Android test expectations are defined.
- Desktop is explicitly out of scope.
- Risks and open questions are captured.

**Sync Status**
- sync-current

**Notes / Links**
- Source spec: `docs/architecture/release-ingress-qa-demo-proof.md`

### Engineering Child

**Summary**
- Implement owned ingress contract for QA demo recording

**Technical Objective**
- Ensure QA demo readiness checks and browser/iOS/Android recorders use MB-owned ingress URLs with generated release host headers, while preserving public release URLs for evidence.

**Parent Feature Link**
- Link to parent feature once created.

**Behavior Slice**
- A QA demo run can record against a real release even when the generated public hostname is not directly reachable from the worker.

**Implementation Plan**
- Normalize release service URLs into public evidence URL and recording execution URL.
- Use internal ingress URL plus `Host` header for readiness checks.
- Pass recording URL context to browser, iOS, and Android recorders.
- Keep PR evidence metadata tied to public release URL and commit SHA.
- Add tests for health checks, recorder context, and hard-failure behavior.

**How to Test**
- Run targeted QA demo service tests.
- Run browser recorder tests.
- Run iOS and Android recorder context tests.
- Run a real MB QA demo run and confirm uploaded recordings are attached to the PR.

**Done Criteria**
- Browser, iOS, and Android demo proof use owned ingress.
- PR evidence links are playable.
- Failures do not produce fake or partial proof.
- Tests cover the real failure mode and the success path.

**Technical Dependencies / Risks**
- Worker and simulator network paths differ by platform.
- Current preview route proxy may need Caddy-specific replacement later.
- Existing generated hostnames must remain stable for current PR evidence consumers.

**Synced From Parent Revision**
- `docs/architecture/release-ingress-qa-demo-proof.md`

**Notes / Links**
- Keep issue in Backlog until the parent feature is reviewed and promoted.
