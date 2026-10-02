# OpenSSL 3 native build review

## 0. Reality model

Facts: the previous native binary embeds BoringSSL with legacy OpenSSL/SSLeay terms.
Upstream libdave and MLSPP already contain an OpenSSL 3 implementation. Events: the
maintainer explicitly selected OpenSSL 3. Intent: build the same DAVE interface with
compatible native licensing. Decision: a single source-build contract. Artifacts:
immutable source archives, notices, build receipts and linked binaries. Observations:
actual crypto/interoperability behavior needs direct evidence beyond license labels.

Maintainers own the build choice, upstream authors own their portions, and the build
owns link evidence. A source name or build flag does not prove the linked result.
Lifecycle: verified sources -> configured OpenSSL 3 -> compiled -> link checked ->
installed with notices -> separately reviewed for release. Missing or duplicate
installation evidence fails; a fresh isolated prefix is required. Root cause:
contract/API failure in the licensed distribution boundary. Anti-patching verdict:
**✅ Model understood — proceed to Phase 1.**

## 0.5 Design search

Considered retaining the incompatible archive with an exception, building the full
upstream vcpkg stack, and building pinned source with Debian OpenSSL 3 and pinned
MLSPP/JSON dependencies. Exceptions change licensing intent; vcpkg adds significant
resource and source-resolution burden. Choose direct source compilation: upstream
already supports these system dependency interfaces, and actual compile/link checks
will challenge that assumption. Dominance is resource-bounded verification without
changing public DAVE semantics. Greenfield would also use one explicit supported
crypto implementation. Deletion test keeps only source verification, dependency
configuration, compilation/link checks and attribution. Evidence of unsupported
upstream dependency behavior or interoperability failure reopens the decision.

## 1. Problem

The native voice distribution must preserve its function without an incompatible
legacy crypto combination.

## 2. Type

Integration boundary.

## 3. Invariants

- Only checksum-pinned source is compiled; no binary fallback.
- OpenSSL major version 3 and actual dynamic linkage are required.
- Original native notices and exact source/build evidence accompany installation.
- Wrong architecture, unsafe source paths, incompatible crypto or existing output fail.

## 4. Assumptions

Primary builder/runtime: Linux Debian trixie, amd64 or arm64. OpenSSL 3 comes from
Debian packages; actual package/source versions are recorded. Serial C++ compilation is the primary default; Go compilation is bounded to two
workers. These build-time limits protect resources without changing release flags
or runtime protocol behavior. Native audio remains experimental until tested.

## 5. Contract matrix

| Input | Result |
| --- | --- |
| Supported pinned source and OpenSSL 3 | Build, verify linkage, install with original material |
| Other source version, host architecture or crypto | Clear failure before installation |
| Unsafe archive or incorrect checksum | Reject before extraction |
| Existing installation | Reject overwrite |
| BoringSSL build/download option | Removed; source build is the primary path |

## 6. Call-path impact scan

Voice Docker builder invokes the installer; the voice runtime receives its output.
Source hashes, receipts, attribution and distribution tests change. API, tenant data,
commands and DAVE wire protocol remain unchanged. No persisted-data migration applies.

## 7. Domain term contracts

Pinned means immutable revision plus verified archive checksum. OpenSSL 3 means
actual headers/version, compilation selection and linked `libcrypto.so.3`, not a label.
Verified build does not mean Discord interoperability or cryptographic certification.

## 8. Authorization and data-access contract

Direct maintainer authorization covers this native replacement. Only public upstream
sources and isolated build containers are accessed; no live credentials, databases,
user workspaces or shared-container cleanup is involved.

## 9. Lifecycle and state matrix

| State | Owner/action |
| --- | --- |
| Sources unverified | Builder checks hashes and archive members |
| Sources verified | Builder configures only OpenSSL 3 |
| Compiled | Builder verifies native architecture/linkage |
| Installed | Builder retains source, notices and receipt |
| Experimentally verified | Reviewer records actual link/CLI evidence |
| Ready for distribution | Maintainer resolves all final artifact obligations |

## 10. Proposed design

Replace the archive installer with a focused standard-library source/build script.
Keep subprocess and filesystem effects at its edges; validate sources and crypto
facts separately. Retain immutable native sources and original notices in the prefix.
Use upstream CMake interfaces; fail on unsupported primary dependency contracts.

## 11. Patterns used

Transaction script and explicit build evidence, with no new runtime service.

## 12. Patterns not used

No source/binary fallback, custom license exception, ABI shim or guessed provenance.

## 13. Change surface

Native installer, voice Docker builder/runtime dependencies, focused tests and native
attribution/distribution documentation. No application schema or authentication change.

## 14. Load shape and query plan

Offline release build, three bounded public source downloads, single-architecture
compilation with bounded parallelism. Check disk before isolated execution; do not
cross the two-GiB remaining-space floor or overlap other heavy builds.

## 15. Failure modes

Downloads/checksums/extraction/configuration/compilation/linkage can fail. Each stops
before publishing an installation; a failed build is not an alternative implementation.
Fresh work directories isolate partial output. Provider/audio behavior is a separate gate.

## 16. Operational integrity

Rebuild existing voice images from the new recipe; previous BoringSSL images are not
cleared by this change. Rollback stops native publication rather than silently selecting
the incompatible implementation. No live state is modified. Original historical notices
remain preserved and distinguished from active build material.

## 17. Tests and observed results

The complete owned Python command passed **59 tests** (including 15 native installer
checks):

```bash
.venv/bin/python -m pytest tests/test_libdave_distribution.py \
  tests/test_imported_material_manifest.py tests/test_docker_dependency_contract.py \
  tests/test_docker_build_contracts.py tests/test_distribution_material.py -q
```

Scoped Ruff lint/format and `git diff --check` passed. `gofmt -d` on the native Go
test produced no differences. Source/path/crypto guards started red. A real
`pkg-config` plus C-compiler header regression reproduced the CGo `dave.h` lookup
failure before the canonical include directory was corrected to `include/dave`.

A completed Linux arm64 builder produced libdave and the actual CGo transport.
The verified installed receipt records OpenSSL **3.5.7**, Debian `libssl-dev` and
`libssl3t64` **3.5.7-1~deb13u3**, and Go **1.25.14**. Eight installed library/header/
notice hashes and all three immutable source archives were checked. `ldd` resolves
both the library and transport to `libcrypto.so.3`, with no unresolved or older
crypto sonames. Transport startup with empty stdin and network disabled emitted
`transport_ready` and exited 0.

The full native-tagged Go suite passed **18 tests**, including a real native session
key-package test. The first smoke test incorrectly selected persisted-key storage;
pinned upstream `Session::InitLeafNode` and `golibdave.NewSession` established that
production uses empty context/authentication IDs and transient keys. The corrected
test uses that exact primary call contract and retains its nonempty, independent
signed-key-package assertions. It does not print generated material.

The final offline command used the inspectable native image plus a read-only current
test-file mount; the image's retained source archive also matched **all 19 current
Go source/lock files**:

```bash
docker run --rm --network none --memory=1536m --memory-swap=1536m --cpus=2 \
  -e GOMEMLIMIT=768MiB \
  -v "$PWD/discord_live_voice_transport/internal/transport/dave_libdave_test.go:/src/discord_live_voice_transport/internal/transport/dave_libdave_test.go:ro" \
  master-builder-libdave-openssl3-proof:local \
  go test -json -mod=vendor -tags=libdave ./...
```

Observed arm64 image identity is recorded with receipt hashes in
[distribution-evidence.json](../third_party/distribution-evidence.json). The final
Docker recipe now requires that native Go suite before producing the binary, but
**its final build-time test gate and image export have not completed successfully**.
The first parallel C++ attempt was canceled at the two-GiB disk floor; serial source
compilation and an earlier CGo image build subsequently completed. A later cached
Go rebuild encountered a snapshot-lease I/O error during export as host free space
again dropped sharply. No shared Docker cache or unrelated user artifacts were
pruned. The offline verified-image tests above are distinct from a successful final
Docker recipe build. Do not treat this evidence as a full voice runtime image,
amd64 proof, live Discord session or crypto certification.

## 18. Verdict

**✅ Proceed — design is appropriate and scoped.** Final binary rights and live Discord
interoperability remain separate evidence gates.
