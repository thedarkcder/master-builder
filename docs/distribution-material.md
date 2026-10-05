# Attribution and corresponding-source material

The first release's supported installation is a source checkout. A source release
and a container/binary release have different contents and obligations. The tools
below collect evidence; they do not authorize redistribution or replace any license.

## Imported guidance and component templates

[The provenance manifest](../third_party/imported-materials.json) binds **15 files**
to their current SHA256, an immutable reviewed upstream source, its SHA256, and an
exact upstream MIT license. The two architecture skill files match their reviewed
licensed source byte for byte. The other 13 files are locally modified references.
The original historical import commit was not recorded and remains `null`; a
matching reference is not represented as that missing historical receipt.

The guidance subset, [.codex/imported-materials.json](../.codex/imported-materials.json),
and its exact MIT license files travel with full `.codex` directory copies. The
minimal bootstrap emits the project's own four generic helper skills, rather than
all six imported files; its existing licensing notice remains appropriate. Retain
the complete notice and scope of any subset actually distributed. The source
manifest does not claim a downstream subset contains every listed source file.

`sidebar`, `status-badge`, `theme-toggle` and `toast-provider` contain project-specific
UI behavior and are not asserted to be verbatim upstream imports. Radix, Lucide,
React and other packages retain their own terms. Organizational ownership of original
changes relies on the maintainer attestation; contracts were not independently read.
A historical Apache-licensed frontend skill was already deleted before preparation;
it was not restored, stripped of notices or relicensed. Historical distributions
containing it must preserve its original license and establish its upstream origin.

Run the local manifest checks after changing imported files:

```bash
uv run --frozen pytest tests/test_imported_material_manifest.py
```

Update a changed local hash only after reviewing the change and its upstream scope.
For new imports record the actual immutable import commit, original license/NOTICE,
local modifications and artifact hashes at import time.

## Distribution surfaces

| Surface | Material to retain and review |
| --- | --- |
| Source checkout/archive | Exact project LICENSE, THIRD_PARTY_NOTICES, imported-material manifests, upstream notices, source/build scripts and modifications |
| Python wheel/sdist | Packaged project license/notices; its dependencies are separately installed artifacts, not automatically covered by the project declaration |
| Backend `app-runtime-base` | Debian packages/fonts, Python native wheels, Node tooling, Codex, Playwright/Chromium, uv and first-party source/build material |
| Static `public-site/out` Vercel website artifact | First-party AGPL source, public npm client components, Manrope OFL/copyright notices and actual built asset provenance; no server, dashboard, private environment or container files |
| `admin-ui` image or emitted web bundle | Node/Alpine packages, npm code/data, Sharp/libvips, client template/library attribution and covered source; container notices alone may not accompany a separately served/downloaded bundle |
| Android worker image | Everything in the backend plus JDK/Maven/Android packages and separately licensed Google SDK components; public image redistribution remains blocked until component rights are established |
| Voice worker/transport binary | Go modules, pinned OpenSSL 3 libdave/native constituents, FFmpeg/Opus, Python wheels including Torch/CUDA, models and voice samples; exact final artifact rights/interoperability remain to verify |
| SSH helper image | Docker/Alpine packages and scripts; APK package/source/notice evidence is separate from the project's AGPL declaration |
| Referenced service images | pgvector/PostgreSQL, Mailpit, SeaweedFS, ClickHouse, Temporal, telemetry collector, Tempo, Prometheus, Grafana, Tailscale, curl and llama.cpp images retain their own licenses; mirroring/repackaging them creates separate artifact obligations |
| Runtime model/voice caches | Not present in a clean source checkout or necessarily an image export; record exact repository revision, filenames, hashes, license/model card and any gate/voice terms if redistributed |

Image tags, apt/APK package sets and downloaded model revisions can change. Record
the actual immutable image digest, final OS architecture, installed versions and
artifact hashes for **each** release. An upstream image's license does not classify
all software in that image. Separate services and SDK/model files are not relicensed.

## Collect material from an exact image

`scripts/collect_distribution_material.py` reads a **Docker export tar** directly.
It does not execute the image or extract the filesystem. It collects license/NOTICE/
copyright files from actual OS package documentation, Python and npm distributions,
Chromium caches, native notice directories and project notices. It records actual
Debian source-package versions or Alpine origin/aports revisions, and checks a managed
libdave receipt against the exported library/header/notice bytes. It excludes unrelated
files such as `.env` and authentication state. Never export a live application container
with customer data or credentials to obtain release evidence.

Use a disposable **unstarted** container from the exact release image. Budget disk
for the export and required source archives before starting. For a local image,
record its content-addressed image ID; for a published image, use its manifest digest.

```bash
image=master-builder:review
immutable_ref="local@$(docker image inspect --format '{{.Id}}' "$image")"
work="$(mktemp -d)"
container="$(docker create --network none --entrypoint /bin/true "$image")"
docker export --output "$work/image.tar" "$container"
docker rm "$container"
python3 scripts/collect_distribution_material.py \
  --archive "$work/image.tar" \
  --artifact-reference "$immutable_ref" \
  --output "$work/material"
```

Review `inventory.json`, copied `notices/`, and `source-retrieval-plan.json`. The status
is deliberately `collected-not-cleared`. Missing notices and unresolved artifact/source
requirements remain visible. An SPDX/package label is not proof of bundled constituents.
The collector supports Debian and Alpine package databases explicitly; unknown/ambiguous
systems, malformed data, unsafe/cyclic links, duplicate paths, receipt mismatches and
existing output directories fail. Individual notice/metadata reads over 16 MiB fail
for separate review. A write error can leave incomplete output; preserve the error,
discard only that owned output, and collect to a fresh directory. Do not publish partial
output. Docker exports omit mounted volumes, so model/voice caches require separate review.

## Retrieve and retain actual corresponding source

The generated plan is **not** a source bundle. Package-manager downloads, upstream
web links and a project Git archive alone are not presumed sufficient for every
license. Select the compliance method allowed by the actual license, keep source
available as required, and publish recipient-facing source acquisition instructions
with the binary/network release. No additional use restriction or CLA is introduced.

- **Project AGPL:** retain the exact release source, modifications, dependency and
  tool locks, build/install/run scripts and required installation information where
  applicable. Provide the required corresponding source under the actual AGPL terms;
  review section 13 for a modified version used over a network. Maintainers must choose
  a real, maintained source URL/offer before deploying such a release.
- **Debian:** the plan identifies exact source package names and Debian source versions,
  including Debian revisions. In an isolated Debian environment with the matching
  public release/security/snapshot `deb-src` repositories enabled, run
  `apt-get source --download-only 'SOURCE_PACKAGE=SOURCE_VERSION'` for every relevant
  row. Retain the `.dsc`, upstream archive, Debian patch archive and checksums, and the
  matching build rules. Use a matching Debian snapshot if the release repository no
  longer carries that exact version. Verify `.dsc` signatures/checksums before use.
  Upstream source without Debian patches is not the exact packaged source.
- **Alpine:** use each origin and aports Git revision, fetch that immutable APKBUILD
  and its source/patch files, verify declared checksums, and retain the exact APK source
  package/build evidence. If the revision is absent, recover it from the exact package
  repository metadata; do not replace it with current `master` or claim provenance.
- **Python/npm:** retrieve exact version artifacts using registry metadata and verify
  their recorded lock/registry hashes. Retain original licenses and source archives
  where required. An sdist/npm tarball can still omit native library source; inventory
  wheel/Sharp/libvips/OpenBLAS/libpq/FFmpeg/CUDA constituents and their own source,
  exceptions, patches and build configuration separately. `psycopg-binary` source
  requirements are not necessarily satisfied by an unrelated current libpq release.
- **LGPL/MPL:** review actual linkage, replacement/relinking and source obligations.
  Dynamic library packaging alone is not proof of compliance; preserve notices and
  sufficient source/build/relinking material where required. For MPL-covered files,
  preserve file-level terms and make the covered source available as required.
- **Browser and CLI binaries:** retain exact upstream source releases, dependency locks,
  native/resource notices and platform-specific material. Codex 0.160.0's npm wrapper
  and inspected Darwin package omit root Apache LICENSE/NOTICE; those files and the
  Ratatui notices identified by its exact Cargo.lock are now retained under
  `third_party/licenses/codex-0.160.0/`. This is not a complete license audit of every
  compiled Rust crate, resource or Linux package. Playwright's Apache license does not
  cover every Chromium constituent; retain the actual browser credits and source.

Authoritative terms: [GNU AGPLv3](https://www.gnu.org/licenses/agpl-3.0.en.html),
[GNU LGPLv3](https://www.gnu.org/licenses/lgpl-3.0.en.html),
[MPL 2.0](https://www.mozilla.org/en-US/MPL/2.0/),
[Debian copyright policy](https://www.debian.org/doc/debian-policy/ch-docs.html#copyright-information)
and [Alpine APKBUILD source fields](https://wiki.alpinelinux.org/wiki/APKBUILD_Reference).
These instructions help obtain evidence; the actual licenses control obligations.

## Current OpenSSL 3 native source build

The primary installer builds libdave **v1.1.0/cpp** source revision
`d6874165b9a7c8d2cc59712c7aceaa8dffb189b4`, MLSPP
`1cc50a124a3bc4e143a787ec934280dc70c1034d`, and nlohmann-json 3.11.3
revision `9cca280a4d0ccf0c08f47a99aa71d1b0e52f8d03`. All three official
source archives have fixed SHA256 checksums. It uses Debian trixie OpenSSL 3
shared libraries, records exact package/source versions, and preserves their
Debian copyright text. BoringSSL, legacy OpenSSL, mixed sonames and other
crypto implementations are rejected; no prebuilt binary or vcpkg path remains.

Build only in a matching Linux amd64/arm64 Debian trixie environment with CMake,
C++ build tools, binutils, pkg-config, Python 3 and `libssl-dev` installed:

```bash
python3 scripts/install_libdave.py --architecture arm64 --prefix /root/.local \
  --notices third_party/licenses/libdave-openssl3 --parallel 1
```

Use a fresh absolute prefix. Source hashes and archive paths are validated before
extraction; unsupported hosts/versions, BoringSSL headers, failed configuration,
missing compile macros, unexpected linkage or existing installation stop the build.
Static PIC MLSPP feeds shared libdave; actual compilation must select `WITH_OPENSSL3`,
and dynamic inspection must find only OpenSSL 3 crypto sonames with no unresolved
libraries. Runtime images explicitly install `libssl3t64`.

The prefix retains original source archives, installer/build/link/package evidence
under `share/master-builder/libdave-source/`, original notices in `licenses/`, and a
receipt binding installed library/header/notice bytes to verified source revisions.
It includes libdave MIT, MLSPP BSD-2-Clause, nlohmann-json MIT, MPark.Variant's
original copyright header plus the full Boost 1.0 license, and actual Debian OpenSSL
copyright terms. Preserve all original archive notices, including nlohmann-json's
additional source/test license files; the enclosing AGPL declaration does not
relicense them. Go runtime/module notices and vendored/original transport sources
are separately retained by the voice Docker builder.

OpenSSL [documents Apache-2.0 for version 3 and later](https://openssl-library.org/source/license/).
This removes the selected BoringSSL legacy-terms implementation from the primary
build, without adding a linking exception or licensing restriction. Final binary
license/source review, native crypto behavior and Discord interoperability are
separate gates. Actual build/link proof and its platform scope are recorded in
[the OpenSSL 3 review](openssl3-libdave-review.md). The observed arm64
OpenSSL 3.5.7 library and actual CGo transport resolve `libcrypto.so.3`; all 18
native-tagged Go tests passed offline, including transient-key/signing smoke.
The final recipe's new build-time Go test gate/export is still unverified after a
host-storage I/O failure. This does not establish amd64, the complete voice runtime,
live Discord audio, provider/FIPS claims or full final-artifact licensing clearance.

The earlier official x64/ARM64 BoringSSL archive hashes and original licenses remain
**historical review evidence** in [distribution-evidence.json](../third_party/distribution-evidence.json).
They are never selected by the current installer. Their legacy OpenSSL/SSLeay
combination remains [GPL-incompatible](https://www.gnu.org/licenses/license-list.html#OpenSSL)
for the previously reviewed linked binary; a new source build does not clear or
retroactively relicense those earlier artifacts. No complete final voice runtime
image, source/relinking bundle or live Discord session has yet been established.

## SeaweedFS development storage

The [local storage guide](../ops/seaweedfs/README.md) selects the published
`chrislusf/seaweedfs:4.48` image at index digest
`sha256:4e61d15fd35994cb1e43e1e553dff106794841fd9a99ade2fc8c8bfce4d7872d`.
Its observed Linux arm64 binary reports source revision
`530be3e37337488ecc34d58441e0bc476e121c93`. The exact upstream root
[Apache-2.0 license](../third_party/licenses/seaweedfs-4.48/LICENSE) and its
immutable retrieval URL/hash are preserved in the distribution manifest.
The project's AGPL declaration does not relicense this server or its constituents.

The obsolete MinIO server/client source recipes have been removed from the local
stack. The Python `minio` package remains an Apache-2.0 S3 client, not a bundled
MinIO server. The separate initializer's locked SDK dependency closure and official
Python base have their own notices and artifact obligations. Existing MinIO data
requires the explicit, operator-controlled migration described in the storage guide.

Referencing the publisher's image is not proof of every embedded dependency license
or source availability. Before mirroring or repackaging this image or initializer,
collect their exact platform artifacts, original notices and component-level
license inventory, and provide any material required by the actual component
licenses. The recorded root Apache license alone does not clear a binary release.

## Release decisions still required

- Require actual OpenSSL 3 compile/link proof and full final voice artifact licensing/source review before native publication; old BoringSSL artifacts remain uncleared.
- Establish component-level Google Android SDK redistribution rights; do not infer
  permission from a local build recipe or automatically accepted SDK terms.
- Review exact proprietary NVIDIA runtime redistribution terms and wheel constituents.
- Record and clear each model revision and voice sample. Pocket TTS's code/model
  license does not license every voice; some upstream samples carry non-commercial
  or unknown terms. Do not accept access gates on another user's behalf.
- Capture the final release images/web/native artifacts, resolve missing notices and
  obtain all required source/patch/build/relinking material. No final complete images
  or legally complete corresponding-source bundles were produced by this review.
- Retain ownership attestations and recover historical import receipts if available.
  Verified references and copyright notices now exist; historical import provenance
  is still not invented.
