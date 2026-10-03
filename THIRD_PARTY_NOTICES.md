# Third-party notices

Master Builder's original material is licensed under `AGPL-3.0-only` as
described in [LICENSE](LICENSE). That declaration does not replace the
licenses of third-party code, libraries, fonts, model weights, voice samples,
or tools. Their copyright notices and license conditions remain applicable.

The dependency inventory and unresolved distribution questions are recorded
in [docs/third-party-licenses.md](docs/third-party-licenses.md). It is a review
snapshot, not a substitute for the license files supplied with each exact
artifact. Binary and container releases must retain those license files,
copyright notices, required attribution, and corresponding-source or
relinking material where required by the relevant license.

## shadcn/ui component templates

`admin-ui/components.json` identifies shadcn/ui, and the basic components in
`admin-ui/components/ui/` and `admin-ui/lib/utils.ts` follow its component
templates. The original template portions retain the following MIT notice.
Project-specific additions are covered by the project's AGPL license.
Immutable reviewed references, local hashes and modification scope are recorded
in [third_party/imported-materials.json](third_party/imported-materials.json).
Historical original import revisions were not recorded and remain unknown.

Source: <https://github.com/shadcn-ui/ui/blob/f3d14c48cbad1b6a3b3c9c81db9f198a4188f712/LICENSE.md>

```text
MIT License

Copyright (c) 2023 shadcn

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Superpowers skill material

The local verification and test-driven-development skills contain substantial
text matching [obra/superpowers](https://github.com/obra/superpowers). Original
portions of `.codex/skills/verification-before-completion/` and
`.codex/skills/test-driven-development/`, including related testing guidance,
retain the upstream MIT terms below. Project-specific additions remain under
the project's AGPL license. Immutable source/license references and local
modification evidence are in the provenance manifest; original import receipts
remain unknown.

Source: <https://github.com/obra/superpowers/blob/8ca22dba9a94f28898bbce59f2537ff4d87c747d/LICENSE>

```text
MIT License

Copyright (c) 2025 Jesse Vincent

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Matt Pocock skill material

The local `.codex/skills/grill-me/` and
`.codex/skills/improve-codebase-architecture/` skills and related reference
material derive from templates published in
[mattpocock/skills](https://github.com/mattpocock/skills). Their original portions
retain the upstream MIT notice below. Project-specific additions remain under
the project's AGPL license. The architecture skill and its reference match a
license-bearing upstream revision byte for byte. Reviewed sources and modification
evidence are in the provenance manifest; original import receipts remain unknown.

Source: <https://github.com/mattpocock/skills/blob/a6bdfd9fed6c17d21b306aeb7ff6a0de8b72ef3c/LICENSE>

```text
MIT License

Copyright (c) 2026 Matt Pocock

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## uv runtime installer

The Docker recipes copy uv 0.9.24 from Astral's official image. uv is
available under MIT OR Apache-2.0; its MIT grant and notice are preserved
below. This does not cover every library compiled into its executable;
include the exact binary's dependency notices in any distributed image.

Source: <https://github.com/astral-sh/uv/blob/0.9.24/LICENSE-MIT>

```text
MIT License

Copyright (c) 2025 Astral Software Inc.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Historical skill material

Git history contains `.codex/skills/frontend-skill/` with an Apache-2.0
license. Those files were already deleted in the working tree when this
review began. They have not been relicensed. If a release restores or
includes that material, retain its original license and establish its
source and any required notices. A new project license does not relicense
historical third-party files.

## Distribution boundaries

- Python wheels and npm packages are installed from public registries;
  their original licenses remain in their installed distributions.
- Native voice transport uses Apache-2.0 Go bindings, pinned MIT Discord
  libdave, BSD-2-Clause MLSPP, MIT nlohmann-json and Boost-licensed
  MPark.Variant. The current native build requires OpenSSL 3 (Apache-2.0)
  from Debian packages and verifies compilation and dynamic linkage.
  Exact source archives, original licenses, Debian OpenSSL copyright and
  build receipts accompany the installed native material. Earlier BoringSSL
  archives with legacy OpenSSL/SSLeay terms remain historical review evidence;
  they are not selected by the current build and remain incompatible for
  the previously reviewed AGPL-covered linked combination. Actual final
  binary/source obligations and Discord interoperability require separate proof.
- Model weights and voice samples are separate works. Pocket TTS code is
  MIT; its published weights declare CC-BY-4.0. Individual voices can have
  different licenses, including non-commercial terms. Do not treat the
  model's license or the project's AGPL license as permission for every
  voice sample.
- Google's Android SDK is subject to a separate agreement. The repository's
  local build recipe is not permission to redistribute the downloaded SDK
  in a public container image. Confirm component-level rights before
  distributing Android worker images.
- Optional Linux voice dependencies include proprietary NVIDIA CUDA
  components. Their redistribution conditions require a separate review;
  they are not covered by AGPL. No NVIDIA binaries or model weights are
  vendored in this repository.

These notices add no restrictions to the project's AGPL license.

## Binary/tool notices and release source material

[third_party/licenses](third_party/licenses) retains exact upstream tool/native
license files, including Codex 0.160.0's Apache LICENSE and NOTICE, Ratatui notices
identified in its immutable Cargo.lock, uv 0.9.24's licenses, and historical prebuilt notices and active OpenSSL 3 source/variant license material. This is not a complete inventory of all
Rust/native/browser/wheel constituents. The project's declaration does not replace
these upstream grants or certify all final native distribution obligations.

The [distribution guide](docs/distribution-material.md) covers actual image
inventory, source retrieval, corresponding-source/relinking material and remaining
SDK/CUDA/model/voice obligations. The offline collector captures actual installed
notices and source-package versions from a Docker export; `collected-not-cleared`
means exactly that. Publish complete required notices/source for the actual released
artifact after resolving its missing evidence and legal obligations.

## SeaweedFS development server

The local storage stack references the upstream published SeaweedFS 4.48 OCI image.
The exact Apache-2.0 root license is retained in
[third_party/licenses/seaweedfs-4.48/LICENSE](third_party/licenses/seaweedfs-4.48/LICENSE),
with source revision, image identity and notice checksum in the distribution manifest.
This server is separate third-party material; its full image dependency inventory
and redistribution obligations require review before mirroring or repackaging.
The `minio` Python SDK is an S3 client and does not bundle the removed MinIO server.

## Manrope UI font

The administration UI uses the third-party Manrope font through `next/font/google`.
The observed build contains six WOFF2 subsets reporting version **4.504** and
`Copyright 2019 The Manrope Project Authors (https://github.com/sharanda/manrope)`.
Their family/version/copyright match the reviewed official Google Fonts source at
commit `8f9a401dbb3793e0d1264b15d96aa253f05280f5`.

The exact unmodified upstream [OFL/copyright file](third_party/licenses/manrope-4.504/OFL.txt)
and [companion font notice](third_party/licenses/manrope-4.504/NOTICE.txt) are retained.
The OFL file's original 2018 notice and the actual font metadata's 2019 notice are
both preserved. Public UI mirrors are `public/licenses/manrope-OFL.txt` and
`public/licenses/manrope-NOTICE.txt` within `admin-ui`; keep them with the fonts in
UI/container distributions. Manrope remains under **SIL Open Font License 1.1**;
the first-party AGPL declaration does not replace its terms. The OFL governs font
bundling and redistribution independently of the application's commercial-use grant.

The source/font hashes and observed metadata are recorded in the distribution
manifest. Google Fonts uses an unversioned download endpoint, so matching metadata
does not establish an exact CDN transformation receipt or provenance for future
builds. This notice does not clear all constituents of a final image.
