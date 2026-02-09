FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV ORCHESTRATOR_DATABASE_URL=postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator
ENV ORCHESTRATOR_ADMIN_USERNAME=admin
ENV ORCHESTRATOR_ADMIN_PASSWORD=change-me
ENV ANDROID_SDK_ROOT=/opt/android-sdk
ENV ANDROID_HOME=/opt/android-sdk
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk
ENV PATH="${PATH}:/opt/android-sdk/cmdline-tools/latest/bin:/opt/android-sdk/platform-tools"

ARG PIP_EXTRA_INDEX_URL=""
ARG ANDROID_CMDLINE_TOOLS_VERSION=11076708
ENV PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL}

WORKDIR /app

COPY pyproject.toml README.md ./
COPY orchestrator ./orchestrator
COPY alembic.ini ./alembic.ini

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      nodejs \
      npm \
      openjdk-17-jdk \
      ripgrep \
      unzip && \
    arch="$(dpkg --print-architecture)" && \
    if [ -d "/usr/lib/jvm/java-17-openjdk-${arch}" ] && [ ! -e "/usr/lib/jvm/java-17-openjdk" ]; then \
      ln -s "/usr/lib/jvm/java-17-openjdk-${arch}" /usr/lib/jvm/java-17-openjdk; \
    fi && \
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
    npm install -g @openai/codex@latest && \
    rm -rf /var/lib/apt/lists/* /tmp/android-cmdline-tools.zip

RUN python -m pip install --upgrade pip && \
    test -n "$PIP_EXTRA_INDEX_URL" && \
    PIP_TRUSTED_HOST="$(echo "$PIP_EXTRA_INDEX_URL" | sed -E 's#^[a-zA-Z]+://([^/@]+@)?([^/:]+).*#\2#')" && \
    pip install --trusted-host "$PIP_TRUSTED_HOST" .[codex_assets]

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["sh", "-c", "python -m orchestrator migrate && uvicorn orchestrator.api.main:app --host 0.0.0.0 --port 4000"]
