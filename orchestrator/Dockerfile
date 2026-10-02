FROM golang:1.25-trixie AS live-voice-transport-builder

WORKDIR /src/discord_live_voice_transport

ARG TARGETARCH
ARG LIBDAVE_VERSION=v1.1.0
ENV SHELL=/bin/sh
ENV GOTOOLCHAIN=local GOFLAGS=-p=2

RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      build-essential \
      binutils \
      cmake \
      git \
      libssl-dev \
      pkg-config \
      python3 && \
    rm -rf /var/lib/apt/lists/*

COPY scripts/install_libdave.py /tmp/install_libdave.py
COPY third_party/licenses/libdave-openssl3 /tmp/libdave-notices

# Pinned native source builds require OpenSSL 3; no prebuilt or crypto fallback.
RUN python3 /tmp/install_libdave.py \
      --version "${LIBDAVE_VERSION}" \
      --architecture "${TARGETARCH}" \
      --notices /tmp/libdave-notices \
      --parallel 1 \
      --prefix /root/.local && \
    rm -f /tmp/install_libdave.py

COPY discord_live_voice_transport/go.mod ./
COPY discord_live_voice_transport/go.sum ./
COPY discord_live_voice_transport/cmd ./cmd
COPY discord_live_voice_transport/internal ./internal

ENV PKG_CONFIG_PATH=/root/.local/lib/pkgconfig
ENV LD_LIBRARY_PATH=/root/.local/lib

RUN --mount=type=cache,target=/root/.cache/go-build,sharing=locked \
    --mount=type=cache,target=/go/pkg/mod,sharing=locked \
    go mod download && go mod verify && \
    mkdir -p /root/.local/licenses/voice-go /root/.local/share/master-builder/voice-go && \
    go list -m -json all > /root/.local/share/master-builder/voice-go/modules.json && \
    go mod graph > /root/.local/share/master-builder/voice-go/module-graph.txt && \
    go mod vendor && \
    go test -mod=vendor -tags=libdave ./... && \
    go build -mod=vendor -tags=libdave -o /tmp/live-voice-transport ./cmd/discord-live-voice-transport && \
    cp "$(go env GOROOT)/LICENSE" /root/.local/licenses/voice-go/Go-LICENSE && \
    find vendor -type f \( -iname '*license*' -o -iname '*notice*' -o -iname '*copyright*' -o -iname '*copying*' \) \
      -exec cp --parents '{}' /root/.local/licenses/voice-go/ \; && \
    cp go.mod go.sum /root/.local/share/master-builder/voice-go/ && \
    go version > /root/.local/share/master-builder/voice-go/go-version.txt && \
    go version -m /tmp/live-voice-transport > /root/.local/share/master-builder/voice-go/build-info.txt && \
    tar -czf /root/.local/share/master-builder/voice-go/vendor-source.tar.gz vendor && \
    tar -czf /root/.local/share/master-builder/voice-go/transport-source.tar.gz go.mod go.sum cmd internal


FROM python:3.11-slim-trixie AS python-common-base

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV ORCHESTRATOR_ADMIN_USERNAME=admin
ENV VIRTUAL_ENV=/opt/venv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_LINK_MODE=copy
ENV UV_HTTP_TIMEOUT=120
ENV UV_HTTP_RETRIES=10
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
ENV PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000

COPY --from=ghcr.io/astral-sh/uv:0.9.24 /uv /usr/local/bin/uv

ARG CODEX_NPM_VERSION=0.160.0
ARG PLAYWRIGHT_NPM_VERSION=1.58.2

WORKDIR /app

RUN printf '%s\n' "${CODEX_NPM_VERSION}" | grep -Eq '^[0-9]+[.][0-9]+[.][0-9]+$' || \
    { echo 'CODEX_NPM_VERSION must be an exact stable release' >&2; exit 1; }

RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      docker-cli \
      docker-compose \
      gh \
      git \
      fonts-freefont-ttf \
      fonts-ipafont-gothic \
      fonts-liberation \
      fonts-noto-color-emoji \
      fonts-tlwg-loma-otf \
      fonts-unifont \
      fonts-wqy-zenhei \
      libasound2t64 \
      libatk-bridge2.0-0t64 \
      libatk1.0-0t64 \
      libatspi2.0-0t64 \
      libcairo2 \
      libcups2t64 \
      libdbus-1-3 \
      libdrm2 \
      libgbm1 \
      libglib2.0-0t64 \
      libnspr4 \
      libnss3 \
      libpango-1.0-0 \
      libx11-6 \
      libxcb1 \
      libxcomposite1 \
      libxdamage1 \
      libxext6 \
      libxfixes3 \
      libxkbcommon0 \
      libxrandr2 \
      nodejs \
      npm \
      ripgrep \
      unzip \
      xvfb \
      xfonts-scalable && \
    npm install -g "@openai/codex@${CODEX_NPM_VERSION}" "playwright@${PLAYWRIGHT_NPM_VERSION}" && \
    test "$(codex --version)" = "codex-cli ${CODEX_NPM_VERSION}" && \
    playwright install chromium && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
FROM python-common-base AS app-runtime-base

COPY README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY third_party ./third_party
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY scripts/bootstrap_deployment_host_agent.py ./scripts/bootstrap_deployment_host_agent.py
COPY scripts/qa_demo_android_recorder.py ./scripts/qa_demo_android_recorder.py
COPY scripts/qa_demo_mobile_recorder.py ./scripts/qa_demo_mobile_recorder.py
COPY scripts/qa_demo_release_context.py ./scripts/qa_demo_release_context.py
COPY scripts/qa_demo_recorder.mjs ./scripts/qa_demo_recorder.mjs
COPY alembic.ini ./alembic.ini

RUN printf '#!/usr/bin/env sh\nexec python -m orchestrator "$@"\n' > "${VIRTUAL_ENV}/bin/orchestrator" && \
    chmod +x "${VIRTUAL_ENV}/bin/orchestrator"

RUN "${VIRTUAL_ENV}/bin/python" -m compileall -q -j 0 /app/orchestrator

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["uvicorn", "orchestrator.api.main:app", "--host", "0.0.0.0", "--port", "4000"]


FROM python-common-base AS python-android-base

ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=/opt/android-sdk
ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="${PATH}:/opt/android-sdk/cmdline-tools/latest/bin:/usr/lib/android-sdk/platform-tools:/usr/lib/android-sdk/build-tools/debian"

ARG ANDROID_CMDLINE_TOOLS_VERSION=11076708

RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      adb \
      aapt \
      android-sdk-build-tools \
      android-sdk-platform-tools \
      default-jdk-headless \
      maven && \
    mkdir -p "${ANDROID_SDK_ROOT}/cmdline-tools" && \
    curl -fsSL "https://dl.google.com/android/repository/commandlinetools-linux-${ANDROID_CMDLINE_TOOLS_VERSION}_latest.zip" \
      -o /tmp/android-cmdline-tools.zip && \
    unzip -q /tmp/android-cmdline-tools.zip -d "${ANDROID_SDK_ROOT}/cmdline-tools" && \
    mv "${ANDROID_SDK_ROOT}/cmdline-tools/cmdline-tools" "${ANDROID_SDK_ROOT}/cmdline-tools/latest" && \
    yes | "${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager" \
      --sdk_root="${ANDROID_SDK_ROOT}" \
      --licenses >/dev/null && \
    "${ANDROID_SDK_ROOT}/cmdline-tools/latest/bin/sdkmanager" \
      --sdk_root="${ANDROID_SDK_ROOT}" \
      "platforms;android-34" >/dev/null && \
    rm -rf /var/lib/apt/lists/* /tmp/android-cmdline-tools.zip


FROM python-android-base AS android-runtime

COPY README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY third_party ./third_party
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY scripts/bootstrap_deployment_host_agent.py ./scripts/bootstrap_deployment_host_agent.py
COPY scripts/qa_demo_android_recorder.py ./scripts/qa_demo_android_recorder.py
COPY scripts/qa_demo_mobile_recorder.py ./scripts/qa_demo_mobile_recorder.py
COPY scripts/qa_demo_release_context.py ./scripts/qa_demo_release_context.py
COPY scripts/qa_demo_recorder.mjs ./scripts/qa_demo_recorder.mjs
COPY alembic.ini ./alembic.ini

RUN printf '#!/usr/bin/env sh\nexec python -m orchestrator "$@"\n' > "${VIRTUAL_ENV}/bin/orchestrator" && \
    chmod +x "${VIRTUAL_ENV}/bin/orchestrator"

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["uvicorn", "orchestrator.api.main:app", "--host", "0.0.0.0", "--port", "4000"]


FROM python-common-base AS voice-runtime

ENV ORCHESTRATOR_LIVE_VOICE_TRANSPORT_COMMAND=/usr/local/bin/live-voice-transport
ENV ORCHESTRATOR_LIVE_VOICE_TRANSPORT_STARTUP_TIMEOUT_SECONDS=10
ENV ORCHESTRATOR_LIVE_VOICE_TRANSPORT_REQUEST_TIMEOUT_SECONDS=10
ENV LD_LIBRARY_PATH=/root/.local/lib

RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      libopus0 \
      libssl3t64 \
      libstdc++6 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=live-voice-transport-builder /root/.local /root/.local
COPY --from=live-voice-transport-builder /tmp/live-voice-transport /usr/local/bin/live-voice-transport

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --extra voice

COPY README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY third_party ./third_party
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY scripts/bootstrap_deployment_host_agent.py ./scripts/bootstrap_deployment_host_agent.py
COPY scripts/qa_demo_android_recorder.py ./scripts/qa_demo_android_recorder.py
COPY scripts/qa_demo_mobile_recorder.py ./scripts/qa_demo_mobile_recorder.py
COPY scripts/qa_demo_release_context.py ./scripts/qa_demo_release_context.py
COPY scripts/qa_demo_recorder.mjs ./scripts/qa_demo_recorder.mjs
COPY alembic.ini ./alembic.ini

RUN printf '#!/usr/bin/env sh\nexec python -m orchestrator "$@"\n' > "${VIRTUAL_ENV}/bin/orchestrator" && \
    chmod +x "${VIRTUAL_ENV}/bin/orchestrator"

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["uvicorn", "orchestrator.api.main:app", "--host", "0.0.0.0", "--port", "4000"]
