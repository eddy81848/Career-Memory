# Career Memory API (Local Foundation Scaffold)

Minimal local FastAPI scaffold for `FOUNDATION-01`.

## Scope

- FastAPI application factory (`career_memory_api.app.create_app`) and local entrypoint (`career_memory_api.main:app`)
- `GET /healthz` liveness endpoint
- Minimal server-verified user identity and Workspace membership boundary (`career_memory_api.authz`)
- Fail-closed default provider (`UnconfiguredWorkspaceAccessProvider`) in normal runtime
- Test-only synthetic access provider living exclusively under `tests/` and injected explicitly in tests

Real Supabase JWT verification, DB membership lookup, product HTTP status codes, final role taxonomy, import APIs, and background jobs are intentionally deferred to `T-101` and later tasks.

## Prerequisites

- `uv`
- Python 3.12+

## Setup

Run commands inside `apps/api`:

```bash
uv sync --frozen
```

## Run Locally

```bash
uv run uvicorn career_memory_api.main:app --host 127.0.0.1 --port 8000
```

Or via module entrypoint (respects `CAREER_MEMORY_API_HOST`, `CAREER_MEMORY_API_PORT`, `CAREER_MEMORY_API_LOG_LEVEL`):

```bash
uv run python -m career_memory_api.main
```

Verify health check:

```bash
curl http://127.0.0.1:8000/healthz
```

Expected response:

```json
{"status":"ok"}
```

## Run Tests

```bash
uv run pytest
```
