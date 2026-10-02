# Open-source attribution follow-up

Review date: 2 October 2026. This review addresses copied guidance/templates and
material accompanying source, binary and container distributions. It does not
change third-party licenses or certify redistribution rights.

## 0. Reality model

**Facts:** Upstream authors own their copied portions. Maintainers have attested to
rights in original project contributions. Imported templates and skills were locally
modified; historical import revisions were not recorded. Dependency locks identify
package versions, not every native constituent or artifact redistribution right.
**Events:** MIT notices were restored during release preparation. **Intentions:**
publish usable AGPL-3.0-only project source and preserve upstream rights. **Decisions:**
record observed source evidence, retain immutable notices, and gather distribution
material from the actual artifact. **Artifacts:** source references, hashes, locks,
license files and exported image filesystems. **Observations:** no checked-in binary
assets were found in the 1,530-file source candidate at this review's start.

Product truth: a recipient needs the actual licenses and source/build material
applicable to their copy. A registry label or similarity score cannot establish that.
Upstream source and license files own upstream facts; maintainers own the final
artifact and organizational rights assessment. No revision, right or compliance
status is inferred from the project license. Collection has explicit states:
uncollected -> material collected -> reviewed for the particular release. Collection
never performs the final transition. Duplicate collections require a fresh output
location; no existing material is silently overwritten. Root cause: ownership failure
in provenance records and incomplete distribution evidence. Anti-patching verdict:
**Model understood — proceed.**

## 0.5 Design search

Considered (1) declaring every dependency compatible from package metadata,
(2) replacing all copied material, (3) collecting notices inside every Docker stage,
and (4) retaining immutable source evidence and collecting from the exact exported
artifact. The first invents rights; the second changes useful behavior without a
need; the third requires additional tooling in unrelated runtimes and still misses
final artifact differences. Choose the fourth. It dominates for scope, evidence and
runtime neutrality. Greenfield practice would retain import receipts from day one;
the current compromise records verified references without pretending they are
historical receipts. The model deletion test retains only source evidence, actual
artifact material and explicit unresolved obligations. Evidence of a different
upstream origin, a changed artifact or legal ownership agreement reopens the review.

## 1. Problem

Recipients lack sufficiently precise provenance and the material needed to assess
licenses of the exact source or binary copy they receive.

## 2. Type

Integration boundary.

## 3. Invariants

- First-party AGPL-3.0-only declarations never replace third-party terms.
- A verified upstream reference is distinguished from an unknown original import.
- Collection reads an explicit artifact without executing it or importing credentials.
- Missing, malformed or unsafe input fails clearly; collection is never clearance.
- Third-party notice bytes are preserved and their hashes are recorded.

## 4. Assumptions

The current supported project distribution is a source checkout. Existing attestations
cover first-party changes; upstream licenses provide evidence only for their scope.
An exported image supplied to the collector is intentionally being reviewed for
release. The collector neither inspects live containers nor reads runtime workspaces.
Binary releases may follow only after artifact-specific obligations are resolved.

## 5. Contract matrix

| Input | Before | After |
| --- | --- | --- |
| Byte-identical imported source | Broad upstream notice | Exact immutable source/license reference and matching hashes |
| Modified imported source | Revision unknown | Reviewed reference, local hash and explicit modifications; original import remains unknown |
| Exported Debian/Alpine image | Manual inventory | Actual package/source versions and retained notice bytes |
| Python/npm packages in image | Lock metadata | Installed versions and notice-file evidence |
| Missing notice, SDK/model or unidentified native work | Cannot infer rights | Recorded unresolved release obligation; never marked legally cleared |
| Invalid archive/path/link or existing output | No owned collector | Explicit error without extraction, execution or overwrite |

## 6. Call-path impact scan

Source releases and full `.codex` directory copies retain notices and provenance.
The minimal bootstrap emits four first-party generic helper skills, and carries its
existing scoped license/notice without claiming to include the imported skill subset.
Wheels retain their scoped license files.
Release maintainers can inspect Docker exports from backend, Android, voice,
admin UI and helper images. The collector is an offline release tool, not an API,
application startup hook or tenant feature. Existing runtime behavior is unchanged.

## 7. Domain term contracts

**Verified reference** means retrieved immutable source plus its observed hash.
**Original import revision** means historical provenance, not a guessed match.
**Collected** means actual material was captured. **Cleared** requires a separate
artifact-specific licensing decision. JSON inventories and tests enforce these
separate meanings.

## 8. Authorization and data-access contract

This is an authorized direct maintainer task, not a private Jira ticket. Upstream
public sources may be read; local source, package locks and an explicitly supplied
archive may be inspected. No Git mutation, live database access, image execution,
authentication export or user workspace cleanup is required. Only license-related
files and package facts are emitted; environment files and credentials are excluded.

## 9. Lifecycle and state matrix

| State | Permitted action | Owner |
| --- | --- | --- |
| Source provenance unknown | Record uncertainty and investigate public evidence | Maintainer/reviewer |
| Immutable reference verified | Retain source/license hashes and modification scope | Reviewer |
| Artifact material absent | Collect from its exact exported filesystem | Release maintainer |
| Material collected | Review missing notices, bundled works and source obligations | Release maintainer/legal reviewer |
| Release obligations resolved | Publish required notices and source alongside artifact | Release maintainer |

## 10. Proposed design

- Store immutable import evidence and exact upstream licenses in source-controlled
  attribution material, with a smaller guidance-only manifest under `.codex`.
- Keep legal explanation in the notices and distribution guide.
- Use a standalone standard-library Python collector over a tar archive. Package
  parsing and path resolution are pure; filesystem reads/writes occur at the edge.
- Emit source retrieval plans and unresolved evidence instead of guessing rights.

## 11. Patterns used

Evidence manifests and a transaction script with validated archive paths. This is
simpler than a licensing service or a registry of runtime exceptions.

## 12. Patterns not used

No domain framework, compatibility layout search, dependency installation fallback,
license inference engine or automatic acceptance of restricted terms.

## 13. Change surface

Attribution manifests/notices, immutable license files, source-retrieval guidance,
collector and focused tests; strict native installer and Docker notice preservation.
No persisted product data, schema, authentication or API changes.

## 14. Load shape and query plan

Offline linear archive metadata scan plus reads of license/package metadata files.
No server query, network fan-out or runtime latency impact. Notice reads are bounded;
oversized material fails explicitly. Large image/source acquisition is a maintainer
release step and must respect available disk capacity.

## 15. Failure modes

Unknown provenance remains unknown. Invalid archive members, escaping/cyclic links,
malformed package facts, missing target files and duplicate output fail explicitly.
Missing notice evidence remains in the inventory. Native constituents, source
patches, restricted SDK terms, model access terms and source-retention obligations
require independent release review even when collection succeeds.

## 16. Operational integrity

Rollback removes the optional release tool and its generated output; runtime state
is unchanged. Collection is offline and does not run image commands. Hashes bind
copied notice bytes to the inspected material. Releases must retain the source,
patches/build scripts and upstream source material for their actual obligations;
public URLs alone are not presumed to satisfy every license. No concurrent writer
is permitted to the chosen fresh output directory.

## 17. Tests and evidence

Verify archive collection using real synthetic tar contents, including Debian/Alpine
source versions, Python and npm notice files, safe links, malicious paths/links,
malformed metadata, missing evidence, immutable source hashes and overwrite refusal.
The collector's owned parser/resolver is exercised directly. UI automation is
unnecessary: this changes release evidence, not a user interface or application
workflow. Regression tests first rejected the absent collector/installer/manifest,
then exposed missing CREDITS, unsafe installation-directory symlinks and malformed
package/receipt JSON; each failed before its corresponding fix.

The strict installer was separately run against both official Linux amd64 and ARM64
archives. SHA256 verification succeeded; each installation retained six required
files and all four original native licenses, matching the committed evidence.
Disposable verification prefixes were removed. This tested download/install/notice
preservation, not native linking, audio functionality or license compatibility.
No complete final image or legally complete corresponding-source bundle was produced.

Final bounded verification: **71 tests passed** using:

```bash
.venv/bin/python -m pytest tests/test_distribution_material.py tests/test_libdave_distribution.py tests/test_imported_material_manifest.py tests/test_docker_dependency_contract.py tests/test_bootstrap_licensing.py tests/test_source_layout.py tests/test_docker_build_contracts.py -q
.venv/bin/ruff check orchestrator tests scripts
.venv/bin/ruff format --check orchestrator tests scripts
git diff --check
```

Root lint and formatting passed (1,100 Python files checked); diff checking passed.
Both release-tool `--help` commands succeeded. Retained project/tool/guidance license
directories are now exercised in actual tar fixtures. A disposable real Go module
reproduced full-module-graph rejection after vendoring; the voice recipe now records
that graph before vendoring, and a red/green recipe regression enforces this order.
The recipe retains Go runtime/module notices and source/build material; the complete
voice image and native function remain unbuilt/unverified in this follow-up.

## 18. Verdict

**✅ Proceed — design is appropriate and scoped.** Publication of binaries/images
remains a separate artifact-specific decision; collection does not resolve unknown
rights or substitute for corresponding-source obligations.
