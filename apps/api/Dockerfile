# Multi-stage, linux/amd64-compatible single image for Cloud Run API (`cm-api`)
# and Private IAM Worker (`cm-worker`) selected strictly via CAREER_MEMORY_SERVICE_ROLE.
#
# Build from repository root:
#   docker build --platform linux/amd64 -t cm-api:local -f apps/api/Dockerfile .

FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

RUN python -m venv /opt/venv

COPY apps/api/pyproject.toml apps/api/README.md apps/api/uv.lock /build/apps/api/
COPY apps/api/src /build/apps/api/src

# Install production dependencies only (no dev group: no pytest / httpx2)
RUN /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install /build/apps/api


FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HOST=0.0.0.0 \
    CAREER_MEMORY_SERVICE_ROLE=api

RUN groupadd --gid 10001 cmapp \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin cmapp \
    && mkdir -p /app/db/migrations \
    && chown -R 10001:10001 /app

COPY --from=builder /opt/venv /opt/venv
# Read-only SQL migrations reference for catalog/version inspection (runtime roles never run DDL)
COPY db/migrations/*.sql /app/db/migrations/
RUN chmod 0444 /app/db/migrations/*.sql

WORKDIR /app
USER 10001:10001
EXPOSE 8080

ENTRYPOINT ["python", "-m", "career_memory_api.hosted_runtime.entrypoint"]
