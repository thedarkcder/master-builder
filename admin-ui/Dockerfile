FROM node:20-alpine

WORKDIR /app

COPY admin-ui/ ./
COPY LICENSE THIRD_PARTY_NOTICES.md ./
COPY third_party ./third_party

RUN npm ci --no-audit --no-fund

ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_API_BASE_URL=http://localhost:60001
ENV ADMIN_UI_PORT=4100

# Build-only key; supply an independent AUTH_SECRET at runtime.
# No local environment file is copied into the build context.
RUN AUTH_SECRET="$(node -e 'process.stdout.write(require("node:crypto").randomBytes(32).toString("hex"))')" ORCHESTRATOR_API_BASE_URL=http://api:4000 npm run build

EXPOSE 4100

CMD ["npm", "run", "start", "--", "-H", "0.0.0.0"]
