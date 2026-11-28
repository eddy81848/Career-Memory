# syntax=docker/dockerfile:1.7
# Production multi-stage Cloud Run container (linux/amd64) for Career Memory Web BFF (OD-010 / OD-013 / OD-015)

FROM node:22-slim AS deps
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22-slim AS builder
WORKDIR /app
ARG NEXT_PUBLIC_API_MODE=hosted
ENV NEXT_TELEMETRY_DISABLED=1
ENV NEXT_PUBLIC_API_MODE=${NEXT_PUBLIC_API_MODE}
COPY --from=deps /app/node_modules ./node_modules
COPY . .
RUN mkdir -p public && npm run build

FROM node:22-slim AS runner
WORKDIR /app
ENV NODE_ENV=production
ENV NEXT_TELEMETRY_DISABLED=1
ENV HOSTNAME=0.0.0.0
ENV PORT=8080

RUN groupadd --system --gid 10001 nodejs \
  && useradd --system --uid 10001 --gid nodejs --home /app --shell /usr/sbin/nologin nextjs

COPY --from=builder --chown=10001:10001 /app/public ./public
COPY --from=builder --chown=10001:10001 /app/.next/standalone ./
COPY --from=builder --chown=10001:10001 /app/.next/static ./.next/static

USER 10001
EXPOSE 8080
CMD ["node", "server.js"]
