# Installation and optional capability status

The initial release runs from a complete source checkout or a runtime image built from this repository. Use the same reviewed revision and `uv.lock` throughout an installation. API startup, CLI runtime commands and migrations validate required root configuration and policy assets before doing runtime work. CLI `--help` remains available without credentials.

A successful `uv build` verifies distribution metadata and Python packaging. The wheel is **not a standalone service installer**: it omits root `.codex` policies, `alembic.ini`, operational scripts and deployment assets. Installing only the wheel cannot satisfy the runtime contract and fails with instructions to use a full checkout or repository-built image. Do not advertise a PyPI install as the service installation procedure. A future standalone installer needs its own asset/configuration ownership design and isolated installation tests.

## Validation boundaries

| Capability | Current status | Prerequisites and evidence required before claiming support |
| --- | --- | --- |
| Source Python development | Primary development path; Python 3.11 checked locally | Public locked dependencies, full source assets, PostgreSQL with `vector`, explicit secrets. Other Python versions allowed by metadata require their own validation. |
| Administration UI | Node 22 is the CI target; minimum Node 20.9 | Locked npm install, build, browser binaries. Mocked UI tests do not prove real API workflows. |
| Linux runtime images | Source-based container path | Build the role-specific target and verify its startup, database permissions and real worker behavior. Image build alone is insufficient. |
| Local object storage | Digest-pinned published SeaweedFS 4.48; private S3 gateway and scoped credentials | See [local storage and explicit migration](../ops/seaweedfs/README.md). Isolated access/retention-policy checks do not establish production exposure or thirty-day physical deletion. |
| Native Windows / WSL | Experimental, not validated | Filesystem/process/toolchain differences and complete API/worker/browser workflows require validation. No native Windows deployment promise. |
| macOS source development | Core local checks exercised on arm64 | Isolated configured backing services and complete source checkout. A local tool inventory does not prove a deployed worker. |
| iOS QA | Experimental, not validated end to end | A macOS worker, Xcode, `xcrun simctl`, available simulator/runtime, app build/signing and capture workflow. Linux containers cannot run Xcode. |
| Android QA | Experimental, not validated end to end | Java, Android SDK/build tools, `adb`, authorized device/emulator, app build and capture workflow. Select `android-runtime` for the container worker. |
| Discord voice | Experimental, not validated end to end | Python `voice` extra, FFmpeg/Opus, Go/libdave transport, explicitly licensed model downloads, Discord application/permissions and real encrypted audio exchange. |
| Temporal worker | Experimental, not validated end to end | Your namespace/task queue, configured server and a real workflow/recovery test. |
| Coolify / managed host previews | Experimental, not validated against a live provider | Explicit API credentials, authorized host/network access, deployment callbacks and a real deploy/recovery test. |
| Hetzner package | Experimental provisioning scripts and Compose package | Review scripts before use; fresh-host, credential, TLS, startup and reboot-recovery tests remain required. “One-click” in design documents is a target, not a support claim. |
| AWS / GCP packages | Design specifications only | No complete IaC installer is included. Provisioning, least-privilege roles, availability and recovery tests must be implemented before release support is claimed. |
| GPU / local-model inference | Experimental | Compatible hardware/drivers, reviewed model license and native dependency notices, model availability/cache and inference tests. No model redistribution rights are implied. |

Provider design targets are recorded in [deployment packaging](deployment-packaging.md); optional settings are documented in [configuration](configuration.md). Distribution obligations for images, binaries and downloaded models are covered by [third-party notices](../THIRD_PARTY_NOTICES.md) and [the license review](third-party-licenses.md).

## Codex runtime tool

The image defaults to exact stable release **`@openai/codex@0.160.0`**, verified from the public npm publisher on 2026-10-02. Its upstream license is Apache-2.0 and remains separate from the project license. The build rejects moving distribution tags or version ranges and checks `codex --version` against the requested release. Changing the build argument requires an exact stable version and revalidation; it does not inherit a support promise. Existing environment files that set `CODEX_NPM_VERSION=latest` must be changed explicitly to `0.160.0`; the build fails rather than silently selecting a different release.

For a host worker, install and check the same version:

```bash
npm install --global @openai/codex@0.160.0
codex --version
codex exec --help
```

The expected version output is `codex-cli 0.160.0`. Version/help checks perform no model execution. Authenticate your own runtime separately using the [official Codex CLI documentation](https://learn.chatgpt.com/docs/codex/cli). Native authentication, model access, structured responses, tools and full delivery workflows still require real integration verification.

## Local prerequisite checks

Run only checks for capabilities you intend to enable:

```bash
docker --version
docker compose version
xcodebuild -version
xcrun --find simctl
adb version
java -version
mvn -version
ffmpeg -version
go version
pkg-config --modversion dave
```

Commands for absent optional tools fail normally; install the actual prerequisite rather than substituting a fallback. On the review host (macOS arm64), Docker, Xcode/simctl, adb, Java 17, Maven, FFmpeg and Go checks succeeded. `pkg-config --modversion dave` failed because libdave was absent. No device, simulator, Discord voice session, cloud provisioning or model inference workflow was executed by these checks. The exact verification record is in [the portability follow-up](open-source-portability-followup.md).
