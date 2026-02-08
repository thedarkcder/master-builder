FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV ORCHESTRATOR_DATABASE_URL=postgresql+psycopg://orchestrator:orchestrator@postgres:5432/orchestrator
ENV ORCHESTRATOR_ADMIN_USERNAME=admin
ENV ORCHESTRATOR_ADMIN_PASSWORD=change-me

ARG PIP_EXTRA_INDEX_URL=""
ENV PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL}

WORKDIR /app

COPY pyproject.toml README.md ./
COPY orchestrator ./orchestrator
COPY alembic.ini ./alembic.ini

RUN apt-get update && \
    apt-get install -y --no-install-recommends nodejs npm ripgrep && \
    npm install -g @openai/codex@latest && \
    rm -rf /var/lib/apt/lists/*

RUN python -m pip install --upgrade pip && \
    test -n "$PIP_EXTRA_INDEX_URL" && \
    PIP_TRUSTED_HOST="$(echo "$PIP_EXTRA_INDEX_URL" | sed -E 's#^[a-zA-Z]+://([^/@]+@)?([^/:]+).*#\2#')" && \
    pip install --trusted-host "$PIP_TRUSTED_HOST" .[codex_assets]

RUN mkdir -p /app/data

EXPOSE 4000

CMD ["sh", "-c", "python -m orchestrator migrate && uvicorn orchestrator.api.main:app --host 0.0.0.0 --port 4000"]
