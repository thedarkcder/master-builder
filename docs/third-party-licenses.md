# Third-party licensing review

Review snapshot: **2 October 2026**. Sources are the checked-in `uv.lock`,
`admin-ui/package-lock.json`, the Go module graph, exact-version PyPI metadata,
npm lockfile metadata, downloaded Go module license files, and the upstream
references below. License metadata is evidence, not proof that every bundled
file has been classified correctly. This is not a legal opinion or a
certification of redistribution rights.

## First-party scope and ownership

The project's declared license is **AGPL-3.0-only**. Commercial use, modification,
forking, redistribution, self-hosting, and charging for software or services
are permitted subject to the actual license. AGPL includes corresponding-source
requirements, including its section 13 condition for certain modified versions
used over a network. The [official license](https://www.gnu.org/licenses/agpl-3.0.en.html)
and [repository LICENSE](../LICENSE) control; this explanation does not replace them.
No CLA or additional use restriction has been introduced.

No conflicting first-party license was found in the tracked manifests or
available historical license files. The repository cannot establish employer
assignments, contractor agreements, customer ownership, contributor authority,
patent rights, or rights to every copied or generated snippet. The user confirmed
organizational redistribution and AGPL relicensing rights during this review;
this is a maintainer attestation, and the underlying contracts were not inspected.
The first-party declaration does not
relicense dependencies, copied templates, or historical Apache-licensed skills.

## Required attribution and distribution review

[Imported-material provenance](../third_party/imported-materials.json),
[exact distribution evidence](../third_party/distribution-evidence.json), and
[artifact/source retrieval guidance](distribution-material.md) now retain verified
immutable upstream references and original notices. They distinguish observed
references from unknown historical imports and do not certify final image rights.

**Current native contract:** a pinned-source OpenSSL 3 build replaces the reviewed
BoringSSL binary path. Actual compile/link evidence and final artifact obligations
are tracked in [the native build review](openssl3-libdave-review.md). Earlier
BoringSSL archives with GPL-incompatible legacy terms remain historical evidence,
and are not cleared or relicensed by the switch.

| Material | Evidence | Consequence |
| --- | --- | --- |
| shadcn/ui templates | `admin-ui/components.json` and basic component structure; upstream MIT license | Exact MIT notice and immutable reviewed reference/local hashes now retained; original historical import commit remains unknown. |
| Superpowers verification/TDD skills | Substantial local text matches obra/superpowers; upstream MIT license retrieved | Exact MIT notice, immutable reviewed references and local modifications recorded; original historical import revisions remain unknown. |
| Matt Pocock interview/architecture skills | Local skill/reference material matches mattpocock/skills; upstream MIT license retrieved | Exact MIT notice and immutable licensed reference recorded; two architecture files are byte-identical, original historical imports remain unknown. |
| Historical frontend skill | Git history includes `.codex/skills/frontend-skill/LICENSE.txt`, Apache-2.0 | Already deleted before this review; preserve original license if restored or included in a historical source release. Origin and any NOTICE requirements remain to verify. |
| Psycopg and its binary wheel | PyPI metadata: LGPL-3.0-only | Retain LGPL and bundled library notices; review corresponding source/replacement/relinking obligations for distributed artifacts. Not relicensed to AGPL. |
| Sharp/libvips binary packages | npm metadata: Apache-2.0 and LGPL-3.0-or-later, sometimes MIT | Retain all native dependency notices and meet LGPL obligations in frontend/container distributions. |
| NumPy/SciPy binary bundles | Package license metadata includes OpenBLAS/LAPACK/GCC runtime and other notices | Keep complete wheel license files, including runtime exceptions and LGPL notices where applicable; top-level BSD label alone is insufficient. |
| SentencePiece | Exact 0.2.1 source archive contains Apache-2.0 and separate Abseil/darts/esaxx/protobuf notices | Preserve the original source archive's license notices in binary/voice distributions; wheel metadata alone did not expose them. |
| axe-core | npm metadata: MPL-2.0 | Retain MPL notices and corresponding source of covered MPL files when distributed; preserve file-level terms. |
| caniuse-lite dataset | npm metadata: CC-BY-4.0 | Keep attribution and license information when distributed. |
| Discord libdave/Go bindings | libdave MIT; godave/disgo family Apache-2.0 | Compatible licensing appears available, subject to notices; generate a native dependency inventory for the exact built libdave binary. The separate DAVE protocol whitepaper has different terms and is not vendored here. |
| Android SDK in `python-android-base` | Docker recipe downloads Google command-line tools/platform and accepts licenses | Public Android worker image redistribution is blocked pending component rights review. SDK terms section 3.4 limits redistribution, with exceptions for OSS-licensed components. Local installation recipe can remain. |
| Optional Linux CUDA dependencies | Python all-extras lock includes proprietary NVIDIA CUDA packages | Separate redistribution review required for GPU/voice images; do not represent CUDA as AGPL-covered or freely redistributable. |
| Pocket TTS weights | Upstream model card: CC-BY-4.0; voice-cloning model has an access gate | Keep attribution; review the particular model and gate terms. Weights are not vendored. Do not accept gate terms on behalf of users. |
| Default embedding/Whisper model families | BAAI/bge-small-en-v1.5 and Systran/faster-whisper-small.en model metadata declare MIT | Preserve exact model notices if redistributed; confirm the configured model and revision. User-selected models can have different terms. |
| Kyutai voice samples/embeddings | Upstream voice repository lists CC0, CC-BY-4.0, CC-BY-NC-4.0, and uncertain historic-recording provenance | Select and document exact voice artifacts and terms before redistribution. Non-commercial samples cannot be included as unrestricted AGPL-covered project content. Custom voices require their own rights. |
| uv runtime installer | Official pinned 0.9.24 image, MIT OR Apache-2.0 | MIT grant and copyright preserved in THIRD_PARTY_NOTICES.md; inventory the exact executable's compiled dependencies for image redistribution. |
| Runtime tools, browsers, OS packages and fonts | Docker installs Codex, Playwright/Chromium, FFmpeg, fonts and Debian packages; services use public images | Generate an image SBOM and preserve package copyright/license files before distributing images. Exact container artifacts have not been built or exhaustively audited for licensing here. |
| Screenshots/private checkout artifacts | Tracked `tmp/wizard-review/` screenshots and untracked worker/project scans existed at review start | Remove from public release; historical copies need privacy and provenance review. AGPL declaration does not establish rights to customer or third-party project content. |

The permissive licenses in the dependency inventory generally allow inclusion
in an AGPL distribution while their original notices remain. LGPL and MPL
components retain their own terms. This compatibility assessment is an
inference from declared licenses, not a complete classification of every
transitive source file, binary bundle, asset, or dependency feature.

## Security audit snapshot

Before updates, npm reported 13 vulnerable package entries, including critical
Next.js/Auth.js findings. Targeted same-major updates and compatible transitive
fixes produced **0 npm audit findings** on this review date. Next.js is pinned
to 16.3.8, next-auth to 5.0.0-beta.32, and eslint-config-next to 16.3.8.
Auth.js remains prerelease software and needs real authentication journey tests.

The all-extras Python audit initially reported 177 findings in 17 packages;
marker variants and advisory aliases caused duplicate reports. Selected updates
and removal of the unused Hikari/Hikari Wave dependencies reduced this to
**3 reported findings in 2 packages (2 distinct advisory IDs)**. The base
dependency audit reports **0 known vulnerabilities** on this review date.
Cryptography was upgraded to 50.0.2 after inspecting the project's Fernet and
Ed25519 usage; 21 targeted secret/Discord/voice tests passed with that version.

| Package retained | Remaining finding | Required action |
| --- | --- | --- |
| torch 2.11.0, optional voice extra | PYSEC-2025-194 | Test the newer Torch release and its platform/GPU dependency changes. |
| setuptools 81.0.0, selected by optional Torch | PYSEC-2026-3447 | Torch 2.11 requires setuptools below 82; patched setuptools 83+ requires upgrading Torch. Isolated project build supports modern setuptools independently. |

The exact advisory applicability still requires triage against enabled runtime
paths. These unresolved findings remain publication blockers, not audit
exceptions. Audits must be rerun on the release commit; advisory databases and
package versions change.

Go `govulncheck` completed source scanning without reachable-symbol findings
for the build without native libdave. Its module inventory reported 18 matching
advisories before updates. Compress was updated to 1.18.7 and `go test ./...`
passed; the final scan retains 17 module-level matches in x/crypto 0.48.0.
A patched x/crypto version is 0.56.0, requiring a deliberate Go toolchain
update. The unmaintained
OpenPGP package has an advisory with no fix. Native libdave, system libraries,
container images, and downloaded model artifacts were not vulnerability-audited.

## Inventory sources and limits

- Python registry sources are public PyPI; npm resolved URLs use the public npm registry.
- All Go module paths are public; no Git submodules, vendored dependency trees,
  private package registries, or private package source URLs were found in the tracked dependency manifests.
- Exact versions are recorded by ecosystem lockfiles. Optional/platform-specific
  entries are included below even when not installed on the reviewer's computer.
- Python packages with incomplete license metadata are explicitly marked below.
  Three package wheel licenses and the exact SentencePiece source archive's
  licenses were additionally inspected; proprietary CUDA metadata still
  requires artifact-level confirmation.
- Build tools, apt packages, service images, optional tool downloads, generated
  output, and dynamically downloaded models are outside these lockfile inventories.
- Before publishing binaries/images, assemble their complete notices and required
  source material from the exact artifacts. This inventory alone does not satisfy
  every redistribution obligation.

Authoritative references:
[GNU AGPLv3](https://www.gnu.org/licenses/agpl-3.0.en.html),
[Android SDK terms](https://developer.android.com/studio/terms),
[Discord libdave MIT license](https://github.com/discord/libdave/blob/main/LICENSE),
[godave Apache license](https://github.com/disgoorg/godave/blob/v0.1.0/LICENSE),
[Pocket TTS code license](https://github.com/kyutai-labs/pocket-tts/blob/main/LICENSE),
[Pocket TTS model card](https://huggingface.co/kyutai/pocket-tts),
[Pocket TTS without voice cloning model card](https://huggingface.co/kyutai/pocket-tts-without-voice-cloning),
[Default embedding model](https://huggingface.co/BAAI/bge-small-en-v1.5),
[Whisper model family](https://huggingface.co/Systran/faster-whisper-small.en),
[Kyutai voice source licenses](https://huggingface.co/kyutai/tts-voices/blob/main/README.md),
[shadcn/ui MIT license](https://github.com/shadcn-ui/ui/blob/main/LICENSE),
[Superpowers MIT license](https://github.com/obra/superpowers/blob/main/LICENSE),
[Matt Pocock skills MIT license](https://github.com/mattpocock/skills/blob/main/LICENSE).

## Python lock inventory

License column records declared package metadata, except explicitly noted
wheel inspections and bundled-license summaries. Upstream metadata can omit
native code licenses; inspect actual release wheels before redistribution.

| Package | Locked version | Declared license/evidence |
| --- | --- | --- |
| aiohappyeyeballs | 2.6.1 | PSF-2.0 |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT |
| aiosignal | 1.4.0 | Apache 2.0 |
| alembic | 1.18.4 | MIT |
| annotated-doc | 0.0.4 | MIT |
| annotated-types | 0.7.0 | MIT License |
| anyio | 4.15.1 | MIT |
| argon2-cffi | 25.1.0 | MIT |
| argon2-cffi-bindings | 25.1.0 | MIT |
| asgiref | 3.11.1 | BSD-3-Clause |
| attrs | 25.4.0 | MIT |
| av | 17.0.0 | BSD-3-Clause |
| beartype | 0.22.9 | MIT License |
| certifi | 2026.2.25 | MPL-2.0 |
| cffi | 2.0.0 | MIT |
| charset-normalizer | 3.4.7 | MIT |
| click | 8.5.0 | BSD-3-Clause |
| colorama | 0.4.6 | BSD License |
| coverage | 7.13.5 | Apache-2.0 |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| ctranslate2 | 4.7.1 | MIT |
| cuda-bindings | 13.2.0 | LicenseRef-NVIDIA-SOFTWARE-LICENSE |
| cuda-pathfinder | 1.5.2 | Apache-2.0 |
| cuda-toolkit | 13.0.2 | Unresolved metadata; verify artifact |
| einops | 0.8.2 | MIT |
| fastapi | 0.135.3 | MIT |
| fastembed | 0.8.0 | Apache License |
| faster-whisper | 1.2.1 | MIT |
| filelock | 3.25.2 | MIT |
| flatbuffers | 25.12.19 | Apache 2.0 |
| frozenlist | 1.8.0 | Apache-2.0 |
| fsspec | 2026.3.0 | BSD-3-Clause |
| googleapis-common-protos | 1.75.0 | Apache 2.0 |
| greenlet | 3.4.0 | MIT AND PSF-2.0 |
| grpcio | 1.81.0 | Apache-2.0 |
| h11 | 0.16.0 | MIT |
| hf-xet | 1.4.3 | Apache Software License |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| huggingface-hub | 1.9.2 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.0 | MIT |
| jinja2 | 3.1.6 | BSD License |
| logguard | 2.0.0 | MIT |
| loguru | 0.7.3 | MIT License |
| lxml | 6.1.3 | BSD-3-Clause |
| mako | 1.4.3 | MIT |
| markdown-it-py | 4.0.0 | MIT License |
| markupsafe | 3.0.3 | BSD-3-Clause |
| mdurl | 0.1.2 | MIT License |
| minio | 7.2.20 | Apache-2.0 |
| mmh3 | 5.2.1 | MIT License |
| mpmath | 1.3.0 | BSD |
| multidict | 6.7.1 | Apache License 2.0 |
| networkx | 3.6.1 | BSD-3-Clause |
| nexus-rpc | 1.4.0 | MIT |
| numpy | 2.4.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| nvidia-cublas | 13.1.0.3 | Unresolved metadata; verify artifact |
| nvidia-cuda-cupti | 13.0.85 | LicenseRef-NVIDIA-Proprietary |
| nvidia-cuda-nvrtc | 13.0.88 | LicenseRef-NVIDIA-Proprietary |
| nvidia-cuda-runtime | 13.0.96 | Unresolved metadata; verify artifact |
| nvidia-cudnn-cu13 | 9.19.0.56 | Unresolved metadata; verify artifact |
| nvidia-cufft | 12.0.0.61 | LicenseRef-NVIDIA-Proprietary |
| nvidia-cufile | 1.15.1.6 | LicenseRef-NVIDIA-Proprietary |
| nvidia-curand | 10.4.0.35 | LicenseRef-NVIDIA-Proprietary |
| nvidia-cusolver | 12.0.4.66 | LicenseRef-NVIDIA-Proprietary |
| nvidia-cusparse | 12.6.3.3 | LicenseRef-NVIDIA-Proprietary |
| nvidia-cusparselt-cu13 | 0.8.0 | NVIDIA Proprietary Software |
| nvidia-nccl-cu13 | 2.28.9 | Unresolved metadata; verify artifact |
| nvidia-nvjitlink | 13.0.88 | LicenseRef-NVIDIA-Proprietary |
| nvidia-nvshmem-cu13 | 3.4.5 | Unresolved metadata; verify artifact |
| nvidia-nvtx | 13.0.85 | Apache 2.0 |
| onnxruntime | 1.24.4 | MIT License |
| opentelemetry-api | 1.42.1 | Apache-2.0 |
| opentelemetry-exporter-otlp | 1.42.1 | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-common | 1.42.1 | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-grpc | 1.42.1 | Apache-2.0 |
| opentelemetry-exporter-otlp-proto-http | 1.42.1 | Apache-2.0 |
| opentelemetry-instrumentation | 0.63b1 | Apache-2.0 |
| opentelemetry-instrumentation-asgi | 0.63b1 | Apache-2.0 |
| opentelemetry-instrumentation-fastapi | 0.63b1 | Apache-2.0 |
| opentelemetry-proto | 1.42.1 | Apache-2.0 |
| opentelemetry-sdk | 1.42.1 | Apache-2.0 |
| opentelemetry-semantic-conventions | 0.63b1 | Apache-2.0 |
| opentelemetry-util-http | 0.63b1 | Apache-2.0 |
| packaging | 26.0 | Apache-2.0 OR BSD-2-Clause |
| pillow | 12.3.0 | MIT-CMU |
| pluggy | 1.6.0 | MIT |
| pocket-tts | 1.1.1 | MIT (installed wheel LICENSE inspected) |
| propcache | 0.4.1 | Apache-2.0 |
| protobuf | 6.33.6 | 3-Clause BSD License |
| psycopg | 3.3.3 | LGPL-3.0-only |
| psycopg-binary | 3.3.3 | LGPL-3.0-only |
| py-cord | 2.7.1 | MIT |
| py-rust-stemmers | 0.1.5 | MIT (installed wheel LICENSE inspected) |
| pycparser | 3.0 | BSD-3-Clause |
| pycryptodome | 3.23.0 | BSD, Public Domain |
| pydantic | 2.12.5 | MIT |
| pydantic-core | 2.41.5 | MIT |
| pydantic-settings | 2.15.0 | MIT |
| pygments | 2.20.0 | BSD-2-Clause |
| pyjwt | 2.15.1 | MIT |
| pynacl | 1.6.2 | Apache-2.0 |
| pypdf | 6.19.0 | BSD-3-Clause |
| pytesseract | 0.3.13 | Apache License 2.0 |
| pytest | 9.0.3 | MIT |
| pytest-cov | 7.1.0 | MIT |
| python-docx | 1.2.0 | MIT |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pyyaml | 6.0.3 | MIT |
| requests | 2.33.1 | Apache-2.0 |
| rich | 14.3.3 | MIT |
| ruff | 0.15.9 | MIT |
| safetensors | 0.7.0 | Apache Software License |
| scipy | 1.17.1 | BSD-3-Clause; bundled native libraries have additional terms |
| sentencepiece | 0.2.1 | Apache-2.0 (exact source archive LICENSE inspected); bundled native notices also apply |
| sentry-sdk | 2.57.0 | MIT |
| setuptools | 81.0.0 | MIT |
| shellingham | 1.5.4 | ISC License |
| sqlalchemy | 2.0.49 | MIT |
| starlette | 1.7.0 | BSD-3-Clause |
| sympy | 1.14.0 | BSD |
| temporalio | 1.27.2 | MIT |
| tokenizers | 0.22.2 | Apache Software License |
| tomli | 2.4.1 | MIT |
| torch | 2.11.0 | BSD-3-Clause |
| tqdm | 4.67.3 | MPL-2.0 AND MIT |
| triton | 3.6.0 | MIT License |
| typer | 0.24.1 | MIT |
| types-protobuf | 6.32.1.20260221 | Apache-2.0 |
| typing-extensions | 4.16.0 | PSF-2.0 |
| typing-inspection | 0.4.2 | MIT |
| tzdata | 2026.1 | Apache-2.0 |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.44.0 | BSD-3-Clause |
| vulture | 2.16 | MIT License |
| websockets | 15.0.1 | BSD-3-Clause |
| win32-setctime | 1.2.0 | MIT license |
| wrapt | 2.2.1 | BSD-2-Clause |
| yarl | 1.23.0 | Apache-2.0 |


## Administration UI npm lock inventory

Optional/platform packages and development dependencies are included.

| Package installation path | Locked version | Declared license |
| --- | --- | --- |
| @alloc/quick-lru | 5.2.0 | MIT |
| @auth/core | 0.41.3 | ISC |
| @babel/code-frame | 7.29.7 | MIT |
| @babel/compat-data | 7.29.7 | MIT |
| @babel/core | 7.29.7 | MIT |
| @babel/core/node_modules/json5 | 2.2.3 | MIT |
| @babel/core/node_modules/semver | 6.3.1 | ISC |
| @babel/generator | 7.29.8 | MIT |
| @babel/helper-compilation-targets | 7.29.7 | MIT |
| @babel/helper-compilation-targets/node_modules/semver | 6.3.1 | ISC |
| @babel/helper-globals | 7.29.7 | MIT |
| @babel/helper-module-imports | 7.29.7 | MIT |
| @babel/helper-module-transforms | 7.29.7 | MIT |
| @babel/helper-string-parser | 7.29.7 | MIT |
| @babel/helper-validator-identifier | 7.29.7 | MIT |
| @babel/helper-validator-option | 7.29.7 | MIT |
| @babel/helpers | 7.29.7 | MIT |
| @babel/parser | 7.29.9 | MIT |
| @babel/runtime | 7.28.6 | MIT |
| @babel/template | 7.29.7 | MIT |
| @babel/traverse | 7.29.8 | MIT |
| @babel/types | 7.29.8 | MIT |
| @emnapi/core | 1.9.1 | MIT |
| @emnapi/runtime | 1.11.3 | MIT |
| @emnapi/wasi-threads | 1.2.0 | MIT |
| @eslint-community/eslint-utils | 4.9.1 | MIT |
| @eslint-community/eslint-utils/node_modules/eslint-visitor-keys | 3.4.3 | Apache-2.0 |
| @eslint-community/regexpp | 4.12.2 | MIT |
| @eslint/config-array | 0.21.1 | Apache-2.0 |
| @eslint/config-helpers | 0.4.2 | Apache-2.0 |
| @eslint/core | 0.17.0 | Apache-2.0 |
| @eslint/eslintrc | 3.3.4 | MIT |
| @eslint/js | 9.39.3 | MIT |
| @eslint/object-schema | 2.1.7 | Apache-2.0 |
| @eslint/plugin-kit | 0.4.1 | Apache-2.0 |
| @humanfs/core | 0.19.2 | Apache-2.0 |
| @humanfs/node | 0.16.8 | Apache-2.0 |
| @humanfs/types | 0.15.0 | Apache-2.0 |
| @humanwhocodes/module-importer | 1.0.1 | Apache-2.0 |
| @humanwhocodes/retry | 0.4.3 | Apache-2.0 |
| @img/colour | 1.1.0 | MIT |
| @img/sharp-darwin-arm64 | 0.35.5 | Apache-2.0 |
| @img/sharp-darwin-x64 | 0.35.5 | Apache-2.0 |
| @img/sharp-freebsd-wasm32 | 0.35.5 | Apache-2.0 |
| @img/sharp-libvips-darwin-arm64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-darwin-x64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-arm | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-arm64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-ppc64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-riscv64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-s390x | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-x64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linuxmusl-arm64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linuxmusl-x64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-linux-arm | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-arm64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-ppc64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-riscv64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-s390x | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-x64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linuxmusl-arm64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linuxmusl-x64 | 0.35.5 | Apache-2.0 |
| @img/sharp-wasm32 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later AND MIT |
| @img/sharp-webcontainers-wasm32 | 0.35.5 | Apache-2.0 |
| @img/sharp-win32-arm64 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later |
| @img/sharp-win32-ia32 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later |
| @img/sharp-win32-x64 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later |
| @jridgewell/gen-mapping | 0.3.13 | MIT |
| @jridgewell/remapping | 2.3.5 | MIT |
| @jridgewell/resolve-uri | 3.1.2 | MIT |
| @jridgewell/sourcemap-codec | 1.5.5 | MIT |
| @jridgewell/trace-mapping | 0.3.31 | MIT |
| @napi-rs/wasm-runtime | 0.2.12 | MIT |
| @next/env | 16.3.8 | MIT |
| @next/eslint-plugin-next | 16.3.8 | MIT |
| @next/eslint-plugin-next/node_modules/fast-glob | 3.3.1 | MIT |
| @next/eslint-plugin-next/node_modules/glob-parent | 5.1.2 | ISC |
| @next/swc-darwin-arm64 | 16.3.8 | MIT |
| @next/swc-darwin-x64 | 16.3.8 | MIT |
| @next/swc-linux-arm64-gnu | 16.3.8 | MIT |
| @next/swc-linux-arm64-musl | 16.3.8 | MIT |
| @next/swc-linux-x64-gnu | 16.3.8 | MIT |
| @next/swc-linux-x64-musl | 16.3.8 | MIT |
| @next/swc-win32-arm64-msvc | 16.3.8 | MIT |
| @next/swc-win32-x64-msvc | 16.3.8 | MIT |
| @nodelib/fs.scandir | 2.1.5 | MIT |
| @nodelib/fs.stat | 2.0.5 | MIT |
| @nodelib/fs.walk | 1.2.8 | MIT |
| @nolyfill/is-core-module | 1.0.39 | MIT |
| @panva/hkdf | 1.2.1 | MIT |
| @playwright/test | 1.58.2 | Apache-2.0 |
| @radix-ui/react-compose-refs | 1.1.2 | MIT |
| @radix-ui/react-label | 2.1.8 | MIT |
| @radix-ui/react-primitive | 2.1.4 | MIT |
| @radix-ui/react-slot | 1.2.4 | MIT |
| @rtsao/scc | 1.1.0 | MIT |
| @swc/helpers | 0.5.23 | Apache-2.0 |
| @tybys/wasm-util | 0.10.1 | MIT |
| @types/d3-array | 3.2.2 | MIT |
| @types/d3-color | 3.1.3 | MIT |
| @types/d3-ease | 3.0.2 | MIT |
| @types/d3-interpolate | 3.0.4 | MIT |
| @types/d3-path | 3.1.1 | MIT |
| @types/d3-scale | 4.0.9 | MIT |
| @types/d3-shape | 3.1.8 | MIT |
| @types/d3-time | 3.0.4 | MIT |
| @types/d3-timer | 3.0.2 | MIT |
| @types/estree | 1.0.8 | MIT |
| @types/json-schema | 7.0.15 | MIT |
| @types/json5 | 0.0.29 | MIT |
| @types/node | 22.19.10 | MIT |
| @types/prop-types | 15.7.15 | MIT |
| @types/react | 18.3.28 | MIT |
| @types/react-dom | 18.3.7 | MIT |
| @typescript-eslint/eslint-plugin | 8.57.2 | MIT |
| @typescript-eslint/eslint-plugin/node_modules/ignore | 7.0.5 | MIT |
| @typescript-eslint/parser | 8.57.2 | MIT |
| @typescript-eslint/project-service | 8.57.2 | MIT |
| @typescript-eslint/scope-manager | 8.57.2 | MIT |
| @typescript-eslint/tsconfig-utils | 8.57.2 | MIT |
| @typescript-eslint/type-utils | 8.57.2 | MIT |
| @typescript-eslint/types | 8.57.2 | MIT |
| @typescript-eslint/typescript-estree | 8.57.2 | MIT |
| @typescript-eslint/typescript-estree/node_modules/balanced-match | 4.0.4 | MIT |
| @typescript-eslint/typescript-estree/node_modules/brace-expansion | 5.0.12 | MIT |
| @typescript-eslint/typescript-estree/node_modules/minimatch | 10.2.4 | BlueOak-1.0.0 |
| @typescript-eslint/utils | 8.57.2 | MIT |
| @typescript-eslint/visitor-keys | 8.57.2 | MIT |
| @typescript-eslint/visitor-keys/node_modules/eslint-visitor-keys | 5.0.1 | Apache-2.0 |
| @unrs/resolver-binding-android-arm-eabi | 1.11.1 | MIT |
| @unrs/resolver-binding-android-arm64 | 1.11.1 | MIT |
| @unrs/resolver-binding-darwin-arm64 | 1.11.1 | MIT |
| @unrs/resolver-binding-darwin-x64 | 1.11.1 | MIT |
| @unrs/resolver-binding-freebsd-x64 | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-arm-gnueabihf | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-arm-musleabihf | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-arm64-gnu | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-arm64-musl | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-ppc64-gnu | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-riscv64-gnu | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-riscv64-musl | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-s390x-gnu | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-x64-gnu | 1.11.1 | MIT |
| @unrs/resolver-binding-linux-x64-musl | 1.11.1 | MIT |
| @unrs/resolver-binding-wasm32-wasi | 1.11.1 | MIT |
| @unrs/resolver-binding-win32-arm64-msvc | 1.11.1 | MIT |
| @unrs/resolver-binding-win32-ia32-msvc | 1.11.1 | MIT |
| @unrs/resolver-binding-win32-x64-msvc | 1.11.1 | MIT |
| acorn | 8.16.0 | MIT |
| acorn-jsx | 5.3.2 | MIT |
| ajv | 6.14.0 | MIT |
| ansi-styles | 4.3.0 | MIT |
| any-promise | 1.3.0 | MIT |
| anymatch | 3.1.3 | ISC |
| arg | 5.0.2 | MIT |
| argparse | 2.0.1 | Python-2.0 |
| aria-query | 5.3.2 | Apache-2.0 |
| array-buffer-byte-length | 1.0.2 | MIT |
| array-includes | 3.1.9 | MIT |
| array.prototype.findlast | 1.2.5 | MIT |
| array.prototype.findlastindex | 1.2.6 | MIT |
| array.prototype.flat | 1.3.3 | MIT |
| array.prototype.flatmap | 1.3.3 | MIT |
| array.prototype.tosorted | 1.1.4 | MIT |
| arraybuffer.prototype.slice | 1.0.4 | MIT |
| ast-types-flow | 0.0.8 | MIT |
| async-function | 1.0.0 | MIT |
| autoprefixer | 10.4.24 | MIT |
| available-typed-arrays | 1.0.7 | MIT |
| axe-core | 4.11.1 | MPL-2.0 |
| axobject-query | 4.1.0 | Apache-2.0 |
| balanced-match | 1.0.2 | MIT |
| baseline-browser-mapping | 2.11.27 | Apache-2.0 |
| binary-extensions | 2.3.0 | MIT |
| brace-expansion | 1.1.21 | MIT |
| braces | 3.0.3 | MIT |
| browserslist | 4.29.3 | MIT |
| call-bind | 1.0.8 | MIT |
| call-bind-apply-helpers | 1.0.2 | MIT |
| call-bound | 1.0.4 | MIT |
| callsites | 3.1.0 | MIT |
| camelcase-css | 2.0.1 | MIT |
| caniuse-lite | 1.0.30001814 | CC-BY-4.0 |
| chalk | 4.1.2 | MIT |
| chokidar | 3.6.0 | MIT |
| chokidar/node_modules/glob-parent | 5.1.2 | ISC |
| class-variance-authority | 0.7.1 | Apache-2.0 |
| client-only | 0.0.1 | MIT |
| clsx | 2.1.1 | MIT |
| color-convert | 2.0.1 | MIT |
| color-name | 1.1.4 | MIT |
| commander | 4.1.1 | MIT |
| concat-map | 0.0.1 | MIT |
| convert-source-map | 2.0.0 | MIT |
| cross-spawn | 7.0.6 | MIT |
| cssesc | 3.0.0 | MIT |
| csstype | 3.2.3 | MIT |
| d3-array | 3.2.4 | ISC |
| d3-color | 3.1.0 | ISC |
| d3-ease | 3.0.1 | BSD-3-Clause |
| d3-format | 3.1.2 | ISC |
| d3-interpolate | 3.0.1 | ISC |
| d3-path | 3.1.0 | ISC |
| d3-scale | 4.0.2 | ISC |
| d3-shape | 3.2.0 | ISC |
| d3-time | 3.1.0 | ISC |
| d3-time-format | 4.1.0 | ISC |
| d3-timer | 3.0.1 | ISC |
| damerau-levenshtein | 1.0.8 | BSD-2-Clause |
| data-view-buffer | 1.0.2 | MIT |
| data-view-byte-length | 1.0.2 | MIT |
| data-view-byte-offset | 1.0.1 | MIT |
| debug | 4.4.3 | MIT |
| decimal.js-light | 2.5.1 | MIT |
| deep-is | 0.1.4 | MIT |
| define-data-property | 1.1.4 | MIT |
| define-properties | 1.2.1 | MIT |
| detect-libc | 2.1.2 | Apache-2.0 |
| didyoumean | 1.2.2 | Apache-2.0 |
| dlv | 1.1.3 | MIT |
| doctrine | 2.1.0 | Apache-2.0 |
| dom-helpers | 5.2.1 | MIT |
| dunder-proto | 1.0.1 | MIT |
| electron-to-chromium | 1.5.444 | ISC |
| emoji-regex | 9.2.2 | MIT |
| es-abstract | 1.24.1 | MIT |
| es-define-property | 1.0.1 | MIT |
| es-errors | 1.3.0 | MIT |
| es-iterator-helpers | 1.2.2 | MIT |
| es-object-atoms | 1.1.1 | MIT |
| es-set-tostringtag | 2.1.0 | MIT |
| es-shim-unscopables | 1.1.0 | MIT |
| es-to-primitive | 1.3.0 | MIT |
| escalade | 3.2.0 | MIT |
| escape-string-regexp | 4.0.0 | MIT |
| eslint | 9.39.3 | MIT |
| eslint-config-next | 16.3.8 | MIT |
| eslint-config-next/node_modules/globals | 16.4.0 | MIT |
| eslint-import-resolver-node | 0.3.9 | MIT |
| eslint-import-resolver-node/node_modules/debug | 3.2.7 | MIT |
| eslint-import-resolver-typescript | 3.10.1 | ISC |
| eslint-module-utils | 2.12.1 | MIT |
| eslint-module-utils/node_modules/debug | 3.2.7 | MIT |
| eslint-plugin-import | 2.32.0 | MIT |
| eslint-plugin-import/node_modules/debug | 3.2.7 | MIT |
| eslint-plugin-import/node_modules/semver | 6.3.1 | ISC |
| eslint-plugin-jsx-a11y | 6.10.2 | MIT |
| eslint-plugin-react | 7.37.5 | MIT |
| eslint-plugin-react-hooks | 7.0.1 | MIT |
| eslint-plugin-react/node_modules/resolve | 2.0.0-next.6 | MIT |
| eslint-plugin-react/node_modules/semver | 6.3.1 | ISC |
| eslint-scope | 8.4.0 | BSD-2-Clause |
| eslint-visitor-keys | 4.2.1 | Apache-2.0 |
| espree | 10.4.0 | BSD-2-Clause |
| esquery | 1.7.0 | BSD-3-Clause |
| esrecurse | 4.3.0 | BSD-2-Clause |
| estraverse | 5.3.0 | BSD-2-Clause |
| esutils | 2.0.3 | BSD-2-Clause |
| eventemitter3 | 4.0.7 | MIT |
| fast-deep-equal | 3.1.3 | MIT |
| fast-equals | 5.4.0 | MIT |
| fast-glob | 3.3.3 | MIT |
| fast-glob/node_modules/glob-parent | 5.1.2 | ISC |
| fast-json-stable-stringify | 2.1.0 | MIT |
| fast-levenshtein | 2.0.6 | MIT |
| fastq | 1.20.1 | ISC |
| file-entry-cache | 8.0.0 | MIT |
| fill-range | 7.1.1 | MIT |
| find-up | 5.0.0 | MIT |
| flat-cache | 4.0.1 | MIT |
| flatted | 3.4.2 | ISC |
| for-each | 0.3.5 | MIT |
| fraction.js | 5.3.4 | MIT |
| fsevents | 2.3.3 | MIT |
| function-bind | 1.1.2 | MIT |
| function.prototype.name | 1.1.8 | MIT |
| functions-have-names | 1.2.3 | MIT |
| generator-function | 2.0.1 | MIT |
| gensync | 1.0.0-beta.2 | MIT |
| get-intrinsic | 1.3.0 | MIT |
| get-proto | 1.0.1 | MIT |
| get-symbol-description | 1.1.0 | MIT |
| get-tsconfig | 4.13.6 | MIT |
| glob-parent | 6.0.2 | ISC |
| globals | 14.0.0 | MIT |
| globalthis | 1.0.4 | MIT |
| gopd | 1.2.0 | MIT |
| has-bigints | 1.1.0 | MIT |
| has-flag | 4.0.0 | MIT |
| has-property-descriptors | 1.0.2 | MIT |
| has-proto | 1.2.0 | MIT |
| has-symbols | 1.1.0 | MIT |
| has-tostringtag | 1.0.2 | MIT |
| hasown | 2.0.2 | MIT |
| hermes-estree | 0.25.1 | MIT |
| hermes-parser | 0.25.1 | MIT |
| ignore | 5.3.2 | MIT |
| import-fresh | 3.3.1 | MIT |
| imurmurhash | 0.1.4 | MIT |
| internal-slot | 1.1.0 | MIT |
| internmap | 2.0.3 | ISC |
| is-array-buffer | 3.0.5 | MIT |
| is-async-function | 2.1.1 | MIT |
| is-bigint | 1.1.0 | MIT |
| is-binary-path | 2.1.0 | MIT |
| is-boolean-object | 1.2.2 | MIT |
| is-bun-module | 2.0.0 | MIT |
| is-callable | 1.2.7 | MIT |
| is-core-module | 2.16.1 | MIT |
| is-data-view | 1.0.2 | MIT |
| is-date-object | 1.1.0 | MIT |
| is-extglob | 2.1.1 | MIT |
| is-finalizationregistry | 1.1.1 | MIT |
| is-generator-function | 1.1.2 | MIT |
| is-glob | 4.0.3 | MIT |
| is-map | 2.0.3 | MIT |
| is-negative-zero | 2.0.3 | MIT |
| is-number | 7.0.0 | MIT |
| is-number-object | 1.1.1 | MIT |
| is-regex | 1.2.1 | MIT |
| is-set | 2.0.3 | MIT |
| is-shared-array-buffer | 1.0.4 | MIT |
| is-string | 1.1.1 | MIT |
| is-symbol | 1.1.1 | MIT |
| is-typed-array | 1.1.15 | MIT |
| is-weakmap | 2.0.2 | MIT |
| is-weakref | 1.1.1 | MIT |
| is-weakset | 2.0.4 | MIT |
| isarray | 2.0.5 | MIT |
| isexe | 2.0.0 | ISC |
| iterator.prototype | 1.1.5 | MIT |
| jiti | 1.21.7 | MIT |
| jose | 6.2.12 | MIT |
| js-tokens | 4.0.0 | MIT |
| js-yaml | 4.3.2 | MIT |
| jsesc | 3.1.0 | MIT |
| json-buffer | 3.0.1 | MIT |
| json-schema-traverse | 0.4.1 | MIT |
| json-stable-stringify-without-jsonify | 1.0.1 | MIT |
| json5 | 1.0.2 | MIT |
| jsx-ast-utils | 3.3.5 | MIT |
| keyv | 4.5.4 | MIT |
| language-subtag-registry | 0.3.23 | CC0-1.0 |
| language-tags | 1.0.9 | MIT |
| levn | 0.4.1 | MIT |
| lilconfig | 3.1.3 | MIT |
| lines-and-columns | 1.2.4 | MIT |
| locate-path | 6.0.0 | MIT |
| lodash | 4.18.1 | MIT |
| lodash.merge | 4.6.2 | MIT |
| loose-envify | 1.4.0 | MIT |
| lru-cache | 5.1.1 | ISC |
| lucide-react | 0.469.0 | ISC |
| math-intrinsics | 1.1.0 | MIT |
| merge2 | 1.4.1 | MIT |
| micromatch | 4.0.8 | MIT |
| minimatch | 3.1.5 | ISC |
| minimist | 1.2.8 | MIT |
| ms | 2.1.3 | MIT |
| mz | 2.7.0 | MIT |
| nanoid | 3.3.19 | MIT |
| napi-postinstall | 0.3.4 | MIT |
| natural-compare | 1.4.0 | MIT |
| next | 16.3.8 | MIT |
| next-auth | 5.0.0-beta.32 | ISC |
| next-themes | 0.4.6 | MIT |
| node-exports-info | 1.6.0 | MIT |
| node-exports-info/node_modules/semver | 6.3.1 | ISC |
| node-releases | 2.0.57 | MIT |
| normalize-path | 3.0.0 | MIT |
| oauth4webapi | 3.8.8 | MIT |
| object-assign | 4.1.1 | MIT |
| object-hash | 3.0.0 | MIT |
| object-inspect | 1.13.4 | MIT |
| object-keys | 1.1.1 | MIT |
| object.assign | 4.1.7 | MIT |
| object.entries | 1.1.9 | MIT |
| object.fromentries | 2.0.8 | MIT |
| object.groupby | 1.0.3 | MIT |
| object.values | 1.2.1 | MIT |
| optionator | 0.9.4 | MIT |
| own-keys | 1.0.1 | MIT |
| p-limit | 3.1.0 | MIT |
| p-locate | 5.0.0 | MIT |
| parent-module | 1.0.1 | MIT |
| path-exists | 4.0.0 | MIT |
| path-key | 3.1.1 | MIT |
| path-parse | 1.0.7 | MIT |
| picocolors | 1.1.1 | ISC |
| picomatch | 2.3.2 | MIT |
| pify | 2.3.0 | MIT |
| pirates | 4.0.7 | MIT |
| playwright | 1.58.2 | Apache-2.0 |
| playwright-core | 1.58.2 | Apache-2.0 |
| playwright/node_modules/fsevents | 2.3.2 | MIT |
| possible-typed-array-names | 1.1.0 | MIT |
| postcss | 8.5.23 | MIT |
| postcss-import | 15.1.0 | MIT |
| postcss-js | 4.1.0 | MIT |
| postcss-load-config | 6.0.1 | MIT |
| postcss-nested | 6.2.0 | MIT |
| postcss-selector-parser | 6.1.4 | MIT |
| postcss-value-parser | 4.2.0 | MIT |
| preact | 10.24.3 | MIT |
| preact-render-to-string | 6.5.11 | MIT |
| prelude-ls | 1.2.1 | MIT |
| prop-types | 15.8.1 | MIT |
| punycode | 2.3.1 | MIT |
| queue-microtask | 1.2.3 | MIT |
| react | 18.3.1 | MIT |
| react-dom | 18.3.1 | MIT |
| react-is | 16.13.1 | MIT |
| react-smooth | 4.0.4 | MIT |
| react-transition-group | 4.4.5 | BSD-3-Clause |
| read-cache | 1.0.0 | MIT |
| readdirp | 3.6.0 | MIT |
| recharts | 2.15.4 | MIT |
| recharts-scale | 0.4.5 | MIT |
| recharts/node_modules/react-is | 18.3.1 | MIT |
| reflect.getprototypeof | 1.0.10 | MIT |
| regexp.prototype.flags | 1.5.4 | MIT |
| resolve | 1.22.11 | MIT |
| resolve-from | 4.0.0 | MIT |
| resolve-pkg-maps | 1.0.0 | MIT |
| reusify | 1.1.0 | MIT |
| run-parallel | 1.2.0 | MIT |
| safe-array-concat | 1.1.3 | MIT |
| safe-push-apply | 1.0.0 | MIT |
| safe-regex-test | 1.1.0 | MIT |
| scheduler | 0.23.2 | MIT |
| semver | 7.8.5 | ISC |
| set-function-length | 1.2.2 | MIT |
| set-function-name | 2.0.2 | MIT |
| set-proto | 1.0.0 | MIT |
| sharp | 0.35.5 | Apache-2.0 |
| shebang-command | 2.0.0 | MIT |
| shebang-regex | 3.0.0 | MIT |
| side-channel | 1.1.0 | MIT |
| side-channel-list | 1.0.0 | MIT |
| side-channel-map | 1.0.1 | MIT |
| side-channel-weakmap | 1.0.2 | MIT |
| source-map-js | 1.2.1 | BSD-3-Clause |
| stable-hash | 0.0.5 | MIT |
| stop-iteration-iterator | 1.1.0 | MIT |
| string.prototype.includes | 2.0.1 | MIT |
| string.prototype.matchall | 4.0.12 | MIT |
| string.prototype.repeat | 1.0.0 | MIT |
| string.prototype.trim | 1.2.10 | MIT |
| string.prototype.trimend | 1.0.9 | MIT |
| string.prototype.trimstart | 1.0.8 | MIT |
| strip-bom | 3.0.0 | MIT |
| strip-json-comments | 3.1.1 | MIT |
| styled-jsx | 5.1.6 | MIT |
| sucrase | 3.35.1 | MIT |
| supports-color | 7.2.0 | MIT |
| supports-preserve-symlinks-flag | 1.0.0 | MIT |
| tailwind-merge | 2.6.1 | MIT |
| tailwindcss | 3.4.19 | MIT |
| thenify | 3.3.1 | MIT |
| thenify-all | 1.6.0 | MIT |
| tiny-invariant | 1.3.3 | MIT |
| tinyglobby | 0.2.15 | MIT |
| tinyglobby/node_modules/fdir | 6.5.0 | MIT |
| tinyglobby/node_modules/picomatch | 4.0.4 | MIT |
| to-regex-range | 5.0.1 | MIT |
| ts-api-utils | 2.5.0 | MIT |
| ts-interface-checker | 0.1.13 | Apache-2.0 |
| tsconfig-paths | 3.15.0 | MIT |
| tslib | 2.8.1 | 0BSD |
| type-check | 0.4.0 | MIT |
| typed-array-buffer | 1.0.3 | MIT |
| typed-array-byte-length | 1.0.3 | MIT |
| typed-array-byte-offset | 1.0.4 | MIT |
| typed-array-length | 1.0.7 | MIT |
| typescript | 5.9.3 | Apache-2.0 |
| typescript-eslint | 8.57.2 | MIT |
| unbox-primitive | 1.1.0 | MIT |
| undici-types | 6.21.0 | MIT |
| unrs-resolver | 1.11.1 | MIT |
| update-browserslist-db | 1.3.3 | MIT |
| uri-js | 4.4.1 | BSD-2-Clause |
| util-deprecate | 1.0.2 | MIT |
| victory-vendor | 36.9.2 | MIT AND ISC |
| which | 2.0.2 | ISC |
| which-boxed-primitive | 1.1.1 | MIT |
| which-builtin-type | 1.2.1 | MIT |
| which-collection | 1.0.2 | MIT |
| which-typed-array | 1.1.20 | MIT |
| word-wrap | 1.2.5 | MIT |
| yallist | 3.1.1 | ISC |
| yocto-queue | 0.1.0 | MIT |
| zod | 4.3.6 | MIT |
| zod-validation-error | 4.0.2 | MIT |

## Go module inventory

Includes transitive/test modules in the module graph; licenses inspected in downloaded module roots. Native C/C++ libdave dependencies are separate.

| Module | Version | Root license evidence |
| --- | --- | --- |
| github.com/davecgh/go-spew | v1.1.1 | ISC |
| github.com/disgoorg/disgo | v0.19.2 | Apache-2.0 |
| github.com/disgoorg/godave | v0.1.0 | Apache-2.0 |
| github.com/disgoorg/godave/golibdave | v0.1.0 | Apache-2.0 |
| github.com/disgoorg/godave/libdave | v0.1.0 | Apache-2.0 |
| github.com/disgoorg/json/v2 | v2.0.0 | Apache-2.0 |
| github.com/disgoorg/omit | v1.0.0 | Apache-2.0 |
| github.com/disgoorg/snowflake/v2 | v2.0.3 | Apache-2.0 |
| github.com/gorilla/websocket | v1.5.3 | BSD-2-Clause |
| github.com/klauspost/compress | v1.18.7 | BSD-3-Clause AND Apache-2.0 (different portions; bundled notices apply) |
| github.com/pmezard/go-difflib | v1.0.0 | BSD-2-Clause |
| github.com/sasha-s/go-csync | v0.0.0-20240107134140-fcbab37b09ad | Apache-2.0 |
| github.com/stretchr/testify | v1.10.0 | MIT |
| golang.org/x/crypto | v0.48.0 | BSD-3-Clause |
| golang.org/x/net | v0.49.0 | BSD-3-Clause |
| golang.org/x/sys | v0.41.0 | BSD-3-Clause |
| golang.org/x/term | v0.40.0 | BSD-3-Clause |
| golang.org/x/text | v0.34.0 | BSD-3-Clause |
| gopkg.in/yaml.v3 | v3.0.1 | MIT AND Apache-2.0 (different portions) |


## Public website npm lock inventory

Added **5 October 2026** from `public-site/package-lock.json`. This separate
static-site package does not include the dashboard or its authentication
dependencies. All resolved archives use the public npm registry. Registry license
labels below include development and optional platform packages; they do not
certify every compiled dependency. The Vercel website artifact contains only
`public-site/out`, not `node_modules`, Sharp/libvips or native build tools.
Preserve the client component notices and the Manrope OFL/copyright files, and
review the actual generated files when changing dependencies. An npm lockfile
inventory does not by itself satisfy artifact attribution obligations.

| Package installation path | Locked version | Declared license |
| --- | --- | --- |
| @alloc/quick-lru | 5.3.0 | MIT |
| @emnapi/runtime | 1.11.3 | MIT |
| @img/colour | 1.1.0 | MIT |
| @img/sharp-darwin-arm64 | 0.35.5 | Apache-2.0 |
| @img/sharp-darwin-x64 | 0.35.5 | Apache-2.0 |
| @img/sharp-freebsd-wasm32 | 0.35.5 | Apache-2.0 |
| @img/sharp-libvips-darwin-arm64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-darwin-x64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-arm | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-arm64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-ppc64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-riscv64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-s390x | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-x64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linuxmusl-arm64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linuxmusl-x64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-linux-arm | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-arm64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-ppc64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-riscv64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-s390x | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-x64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linuxmusl-arm64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linuxmusl-x64 | 0.35.5 | Apache-2.0 |
| @img/sharp-wasm32 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later AND MIT |
| @img/sharp-webcontainers-wasm32 | 0.35.5 | Apache-2.0 |
| @img/sharp-win32-arm64 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later |
| @img/sharp-win32-ia32 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later |
| @img/sharp-win32-x64 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later |
| @jridgewell/gen-mapping | 0.3.13 | MIT |
| @jridgewell/remapping | 2.3.5 | MIT |
| @jridgewell/resolve-uri | 3.1.2 | MIT |
| @jridgewell/sourcemap-codec | 1.6.0 | MIT |
| @jridgewell/trace-mapping | 0.3.31 | MIT |
| @next/env | 16.3.8 | MIT |
| @next/swc-darwin-arm64 | 16.3.8 | MIT |
| @next/swc-darwin-x64 | 16.3.8 | MIT |
| @next/swc-linux-arm64-gnu | 16.3.8 | MIT |
| @next/swc-linux-arm64-musl | 16.3.8 | MIT |
| @next/swc-linux-x64-gnu | 16.3.8 | MIT |
| @next/swc-linux-x64-musl | 16.3.8 | MIT |
| @next/swc-win32-arm64-msvc | 16.3.8 | MIT |
| @next/swc-win32-x64-msvc | 16.3.8 | MIT |
| @playwright/test | 1.58.2 | Apache-2.0 |
| @swc/helpers | 0.5.23 | Apache-2.0 |
| @tailwindcss/node | 4.3.3 | MIT |
| @tailwindcss/oxide | 4.3.3 | MIT |
| @tailwindcss/oxide-android-arm64 | 4.3.3 | MIT |
| @tailwindcss/oxide-darwin-arm64 | 4.3.3 | MIT |
| @tailwindcss/oxide-darwin-x64 | 4.3.3 | MIT |
| @tailwindcss/oxide-freebsd-x64 | 4.3.3 | MIT |
| @tailwindcss/oxide-linux-arm-gnueabihf | 4.3.3 | MIT |
| @tailwindcss/oxide-linux-arm64-gnu | 4.3.3 | MIT |
| @tailwindcss/oxide-linux-arm64-musl | 4.3.3 | MIT |
| @tailwindcss/oxide-linux-x64-gnu | 4.3.3 | MIT |
| @tailwindcss/oxide-linux-x64-musl | 4.3.3 | MIT |
| @tailwindcss/oxide-wasm32-wasi | 4.3.3 | MIT |
| @tailwindcss/oxide-win32-arm64-msvc | 4.3.3 | MIT |
| @tailwindcss/oxide-win32-x64-msvc | 4.3.3 | MIT |
| @tailwindcss/postcss | 4.3.3 | MIT |
| @types/node | 22.19.10 | MIT |
| @types/prop-types | 15.7.15 | MIT |
| @types/react | 18.3.28 | MIT |
| @types/react-dom | 18.3.7 | MIT |
| baseline-browser-mapping | 2.11.27 | Apache-2.0 |
| caniuse-lite | 1.0.30001814 | CC-BY-4.0 |
| client-only | 0.0.1 | MIT |
| csstype | 3.2.3 | MIT |
| detect-libc | 2.1.2 | Apache-2.0 |
| enhanced-resolve | 5.26.0 | MIT |
| fsevents | 2.3.2 | MIT |
| graceful-fs | 4.2.11 | ISC |
| jiti | 2.7.0 | MIT |
| js-tokens | 4.0.0 | MIT |
| lightningcss | 1.32.0 | MPL-2.0 |
| lightningcss-android-arm64 | 1.32.0 | MPL-2.0 |
| lightningcss-darwin-arm64 | 1.32.0 | MPL-2.0 |
| lightningcss-darwin-x64 | 1.32.0 | MPL-2.0 |
| lightningcss-freebsd-x64 | 1.32.0 | MPL-2.0 |
| lightningcss-linux-arm-gnueabihf | 1.32.0 | MPL-2.0 |
| lightningcss-linux-arm64-gnu | 1.32.0 | MPL-2.0 |
| lightningcss-linux-arm64-musl | 1.32.0 | MPL-2.0 |
| lightningcss-linux-x64-gnu | 1.32.0 | MPL-2.0 |
| lightningcss-linux-x64-musl | 1.32.0 | MPL-2.0 |
| lightningcss-win32-arm64-msvc | 1.32.0 | MPL-2.0 |
| lightningcss-win32-x64-msvc | 1.32.0 | MPL-2.0 |
| loose-envify | 1.4.0 | MIT |
| lucide-react | 0.469.0 | ISC |
| magic-string | 0.30.21 | MIT |
| nanoid | 3.3.19 | MIT |
| next | 16.3.8 | MIT |
| picocolors | 1.1.1 | ISC |
| playwright | 1.58.2 | Apache-2.0 |
| playwright-core | 1.58.2 | Apache-2.0 |
| postcss | 8.5.23 | MIT |
| react | 18.3.1 | MIT |
| react-dom | 18.3.1 | MIT |
| scheduler | 0.23.2 | MIT |
| semver | 7.8.5 | ISC |
| sharp | 0.35.5 | Apache-2.0 |
| source-map-js | 1.2.2 | BSD-3-Clause |
| styled-jsx | 5.1.6 | MIT |
| tailwindcss | 4.3.3 | MIT |
| tapable | 2.3.3 | MIT |
| tslib | 2.8.1 | 0BSD |
| typescript | 5.9.3 | Apache-2.0 |
| undici-types | 6.21.0 | MIT |
