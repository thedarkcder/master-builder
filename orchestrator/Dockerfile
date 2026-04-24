FROM golang:1.25-trixie AS live-voice-transport-builder

WORKDIR /src/discord_live_voice_transport

ARG GODAVE_VERSION=v0.1.0
ARG LIBDAVE_VERSION=v1.1.0
ARG LIBDAVE_FORCE_BUILD=0
ENV SHELL=/bin/sh

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
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

ARG CODEX_NPM_VERSION=latest

WORKDIR /app

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      gh \
      git \
      nodejs \
      npm \
      ripgrep \
      unzip && \
    npm install -g "@openai/codex@${CODEX_NPM_VERSION}" && \
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
    "${VIRTUAL_ENV}/bin/pip" install -r /tmp/requirements-base.txt
FROM python-common-base AS app-runtime-base

COPY README.md ./
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY alembic.ini ./alembic.ini

RUN --mount=type=cache,target=/root/.cache/pip \
    "${VIRTUAL_ENV}/bin/pip" install --no-deps --no-build-isolation .

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["uvicorn", "orchestrator.api.main:app", "--host", "0.0.0.0", "--port", "4000"]


FROM python-common-base AS python-android-base

ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=/opt/android-sdk
ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="${PATH}:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools"

ARG ANDROID_CMDLINE_TOOLS_VERSION=11076708

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      default-jdk-headless && \
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
      "platform-tools" \
      "build-tools;34.0.0" \
      "platforms;android-34" >/dev/null && \
    rm -rf /var/lib/apt/lists/* /tmp/android-cmdline-tools.zip


FROM python-android-base AS android-runtime

COPY README.md ./
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY alembic.ini ./alembic.ini

RUN --mount=type=cache,target=/root/.cache/pip \
    "${VIRTUAL_ENV}/bin/pip" install --no-deps --no-build-isolation .

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["uvicorn", "orchestrator.api.main:app", "--host", "0.0.0.0", "--port", "4000"]


FROM python-common-base AS voice-runtime

ENV ORCHESTRATOR_LIVE_VOICE_TRANSPORT_COMMAND=/usr/local/bin/live-voice-transport
ENV ORCHESTRATOR_LIVE_VOICE_TRANSPORT_STARTUP_TIMEOUT_SECONDS=10
ENV ORCHESTRATOR_LIVE_VOICE_TRANSPORT_REQUEST_TIMEOUT_SECONDS=10
ENV LD_LIBRARY_PATH=/root/.local/lib

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update && \
    apt-get install -y --no-install-recommends \
      ffmpeg \
      libopus0 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=live-voice-transport-builder /root/.local /root/.local
COPY --from=live-voice-transport-builder /tmp/live-voice-transport /usr/local/bin/live-voice-transport

RUN --mount=type=cache,target=/root/.cache/pip \
    "${VIRTUAL_ENV}/bin/pip" install -r /tmp/requirements-voice.txt

COPY README.md ./
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY alembic.ini ./alembic.ini

RUN --mount=type=cache,target=/root/.cache/pip \
    "${VIRTUAL_ENV}/bin/pip" install --no-deps --no-build-isolation .

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["uvicorn", "orchestrator.api.main:app", "--host", "0.0.0.0", "--port", "4000"]
