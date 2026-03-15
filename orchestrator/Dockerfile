FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV ORCHESTRATOR_DATABASE_URL=postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator
ENV ORCHESTRATOR_ADMIN_USERNAME=admin
ENV ORCHESTRATOR_ADMIN_PASSWORD=change-me
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=/opt/android-sdk
ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="${PATH}:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools"

ARG ANDROID_CMDLINE_TOOLS_VERSION=11076708
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
      default-jdk-headless \
      ripgrep \
      unzip && \
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
    npm install -g "@openai/codex@${CODEX_NPM_VERSION}" && \
    rm -rf /var/lib/apt/lists/* /tmp/android-cmdline-tools.zip

COPY pyproject.toml README.md ./
COPY .codex ./.codex
COPY orchestrator ./orchestrator
COPY alembic.ini ./alembic.ini

RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip && \
    pip install .

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["sh", "-c", "python -m orchestrator migrate && uvicorn orchestrator.api.main:app --host 0.0.0.0 --port 4000"]
