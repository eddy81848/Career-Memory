# DB-free minimal IAM probe image candidate for future Cloud Run Step 1 tests.
# Uses Python standard library only (zero external packages, zero pip install).
# Pinned to the verified OCI image index digest (multi-arch manifest list):
#   index:       python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b
#   linux/amd64: sha256:4a68d3169325c47e1bafbb86425220f03fbdff6f079a419e8d39407183288217 (3.12.16-slim-trixie)
# Build with: docker build --platform linux/amd64 --pull=false --load ...
# Do NOT push to any registry without explicit approval.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

WORKDIR /app
COPY probe.py ./probe.py

ENV PORT=8080 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
USER nobody

CMD ["python", "-B", "/app/probe.py"]
