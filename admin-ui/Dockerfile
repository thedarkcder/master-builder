FROM node:20-alpine

WORKDIR /app

COPY . .

RUN if [ -f package-lock.json ]; then \
      npm ci --no-audit --no-fund; \
    elif [ -f yarn.lock ]; then \
      corepack enable && yarn install --frozen-lockfile; \
    else \
      npm install --no-audit --no-fund; \
    fi

ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_API_BASE_URL=http://localhost:60001
ENV ADMIN_UI_PORT=4100

RUN npm run build

EXPOSE 4100

CMD ["npm", "run", "start", "--", "-H", "0.0.0.0"]
