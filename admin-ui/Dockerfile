FROM node:20-alpine

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_API_BASE_URL=http://localhost:8100

RUN npm run build

EXPOSE 4100

CMD ["npm", "run", "start", "--", "-H", "0.0.0.0", "-p", "4100"]
