FROM node:20-alpine

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund

COPY . .

ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_API_BASE_URL=http://localhost:4000

RUN npm run build

EXPOSE 4100

CMD ["npm", "run", "start", "--", "-H", "0.0.0.0", "-p", "4100"]
