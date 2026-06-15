FROM golang:1.25-trixie AS live-voice-transport-builder

WORKDIR /src/discord_live_voice_transport

ARG GODAVE_VERSION=v0.1.0
ARG LIBDAVE_VERSION=v1.1.0
ARG LIBDAVE_FORCE_BUILD=0
ENV SHELL=/bin/sh

RUN apt-get update -o Acquire::Retries=3 && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      build-essential \
      cmake \
      curl \
      git \
      libssl-dev \
      nasm \
      ninja-build \
      perl \
      pkg-config \
      python3 \
      unzip \
      zip && \
    rm -rf /var/lib/apt/lists/*

RUN curl -fsSL "https://raw.githubusercontent.com/disgoorg/godave/${GODAVE_VERSION}/scripts/libdave_install.sh" \
      -o /tmp/libdave_install.sh && \
    if [ "${LIBDAVE_FORCE_BUILD}" = "1" ] || [ "${LIBDAVE_FORCE_BUILD}" = "true" ] || [ "${LIBDAVE_FORCE_BUILD}" = "yes" ]; then \
      FORCE_BUILD=1 sh /tmp/libdave_install.sh "${LIBDAVE_VERSION}"; \
    else \
      sh /tmp/libdave_install.sh "${LIBDAVE_VERSION}"; \
    fi && \
    rm -f /tmp/libdave_install.sh

COPY discord_live_voice_transport/go.mod ./
COPY discord_live_voice_transport/go.sum ./
COPY discord_live_voice_transport/cmd ./cmd
COPY discord_live_voice_transport/internal ./internal

ENV PKG_CONFIG_PATH=/root/.local/lib/pkgconfig
ENV LD_LIBRARY_PATH=/root/.local/lib

RUN --mount=type=cache,target=/root/.cache/go-build,sharing=locked \
    --mount=type=cache,target=/go/pkg/mod,sharing=locked \
    go build -tags=libdave -o /tmp/live-voice-transport ./cmd/discord-live-voice-transport


FROM python:3.11-slim-trixie AS python-common-base

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV ORCHESTRATOR_DATABASE_URL=postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator
ENV ORCHESTRATOR_ADMIN_USERNAME=admin
ENV ORCHESTRATOR_ADMIN_PASSWORD=change-me
ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
ENV PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000

ARG CODEX_NPM_VERSION=latest
ARG PLAYWRIGHT_NPM_VERSION=1.51.1

WORKDIR /app

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
    playwright install chromium && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./

RUN python -m venv "${VIRTUAL_ENV}"

RUN python - <<'PY'
from pathlib import Path
import tomllib

project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
dependencies = project.get("project", {}).get("dependencies", [])
voice_dependencies = project.get("project", {}).get("optional-dependencies", {}).get("voice", [])
Path("/tmp/requirements-base.txt").write_text("\n".join(dependencies) + "\n", encoding="utf-8")
Path("/tmp/requirements-voice.txt").write_text("\n".join(voice_dependencies) + "\n", encoding="utf-8")
PY

RUN --mount=type=cache,target=/root/.cache/pip \
    "${VIRTUAL_ENV}/bin/pip" install --retries 10 --timeout 120 -r /tmp/requirements-base.txt
FROM python-common-base AS app-runtime-base

COPY README.md ./
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY scripts/bootstrap_deployment_host_agent.py ./scripts/bootstrap_deployment_host_agent.py
COPY scripts/qa_demo_android_recorder.py ./scripts/qa_demo_android_recorder.py
COPY scripts/qa_demo_mobile_recorder.py ./scripts/qa_demo_mobile_recorder.py
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
      ffmpeg \
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

COPY README.md ./
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY scripts/qa_demo_android_recorder.py ./scripts/qa_demo_android_recorder.py
COPY scripts/qa_demo_mobile_recorder.py ./scripts/qa_demo_mobile_recorder.py
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
      libopus0 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=live-voice-transport-builder /root/.local /root/.local
COPY --from=live-voice-transport-builder /tmp/live-voice-transport /usr/local/bin/live-voice-transport

RUN --mount=type=cache,target=/root/.cache/pip \
    "${VIRTUAL_ENV}/bin/pip" install --retries 10 --timeout 120 -r /tmp/requirements-voice.txt

COPY README.md ./
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY alembic.ini ./alembic.ini

RUN printf '#!/usr/bin/env sh\nexec python -m orchestrator "$@"\n' > "${VIRTUAL_ENV}/bin/orchestrator" && \
    chmod +x "${VIRTUAL_ENV}/bin/orchestrator"

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["uvicorn", "orchestrator.api.main:app", "--host", "0.0.0.0", "--port", "4000"]
