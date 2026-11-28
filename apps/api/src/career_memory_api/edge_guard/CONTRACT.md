# Edge Ingress, Origin Auth, Cache Isolation, CI Trust & Cost Guard Contract (`M5-T507` / `INF-07`, `INF-09`)

## 1. Scope & Zero-Spend Hosting Boundary (`ADR-020` / `OD-013` / `OD-015`)
`career_memory_api.edge_guard` is the composable ASGI security, cache-isolation, and runtime cost-guard boundary for Career Memory's Cloudflare -> Cloud Run architecture, together with the deterministic GitHub Actions CI trust-boundary auditor (`INF-09`).

- **Zero External Calls / Zero Cloud Spend**: All verification runs offline against pure ASGI apps and local `.github/workflows/*.yml` files (`external_cloud_calls == 0`).
- **Hosted Public Pilot Gate (`HELD_NO_GO_PENDING_OD013_OD015`)**: Passing app-layer `INF-07` and `INF-09` checks does **NOT** authorize live internet exposure on Cloudflare + Cloud Run + Supabase Free until the operator separately verifies billing/budget cutoffs, Cloudflare origin mTLS/transform rules, Cloud Run IAM ingress settings, and Supabase Free project limits.

## 2. Route Trust Matrix (`RouteTrustClass`)
| Trust Class | Routes | Origin / Worker Auth | Cache & Security Headers | Cost-Guard Pause Behavior |
|---|---|---|---|---|
| `PUBLIC_HEALTH` | `GET /healthz`, `HEAD /healthz` | None (Cloud Run liveness/startup probe) | `Cache-Control: no-store`, `CDN-Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` | Always allowed |
| `AUTHENTICATED_API` | Declared `/v1/**` resource prefixes only | Required when `require_origin_auth=True`: cryptographic `X-CM-Origin-Auth` (HMAC-SHA256 timestamped signature or constant-time rotation ring) OR trusted-proxy `X-Forwarded-Client-Cert` (mTLS); direct `*.run.app` bypass rejected (`403 ORIGIN_AUTH_REQUIRED`) | Forced on **every 2xx/4xx/5xx** response (stripping upstream `public` cache headers): `Cache-Control: private, no-store`, `CDN-Cache-Control: no-store`, `Pragma: no-cache`, `Vary: Authorization, Cookie, X-Workspace-ID, Origin`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` | High-cost/mutable growth routes (`POST /v1/sync-runs`, `POST /v1/sync-runs/{id}/retry`, `POST /v1/work-events/{id}/claims:generate`, `POST /v1/exports`, and other non-essential mutations) return `503 RUNTIME_PAUSED_COST_GUARD` + `Retry-After` + `free_alternatives` (`paid_fallback_allowed: false`). Essential safety & user-rights routes (`POST /v1/source-accounts/{id}/disconnect`, `POST /v1/source-resources/{id}/deletion-preview`, `POST /v1/source-resources/{id}/deletion-confirm`, `GET /v1/deletion-jobs/{id}`) remain **ALLOWED**. |
| `INTERNAL_WORKER_ONLY` | `/internal/worker/**` | Immediately denies ANY edge/browser/session header (`CF-Connecting-IP`, `CF-Ray`, `X-CM-Origin-Auth`, `Cookie`, `Origin`, `Sec-Fetch-Site`, `Referer`) with `403 PUBLIC_WORKER_ROUTE_FORBIDDEN`; requires verified internal IAM/OIDC audience token | `Cache-Control: private, no-store`, `CDN-Cache-Control: no-store`, `Pragma: no-cache`, `Vary: ...`, security headers | Blocked (`503 RUNTIME_PAUSED_COST_GUARD`) |
| `DENIED_UNDECLARED` | Any path outside allowlist or prohibited prefix (`/debug`, `/admin`, `/metrics`, `/v1/byok`, `/v1/llm/callback`, path traversal `..` / `%2e` / `//`) | Fail closed before handler dispatch (`404` or `403`, `ROUTE_NOT_DECLARED`) | `Cache-Control: private, no-store`, `CDN-Cache-Control: no-store`, security headers | Always denied |

## 3. One-Line Integration for Wave 2 / Wave 3 (`app.py`)
```python
from career_memory_api.edge_guard import (
    CorsCsrfConfig,
    EdgeGuardConfig,
    EdgeRuntimeCaps,
    ManualEdgePauseController,
    OriginAuthConfig,
    WorkerAuthConfig,
    install_edge_guard,
)

edge_runtime = install_edge_guard(
    app,
    EdgeGuardConfig(
        origin_auth=OriginAuthConfig(
            require_origin_auth=True,
            origin_hmac_secrets=(current_secret, previous_secret),
        ),
        worker_auth=WorkerAuthConfig(
            allowed_worker_audiences=frozenset({"https://worker.internal.career-memory.local"}),
            allowed_worker_service_accounts=frozenset({"cm-worker@project.iam.gserviceaccount.com"}),
            token_verifier=worker_oidc_verifier,
        ),
        cors_csrf=CorsCsrfConfig(
            allowed_web_origins=frozenset({"https://app.career-memory.example"}),
        ),
        caps=EdgeRuntimeCaps(
            max_request_body_bytes=262_144,
            max_concurrent_requests=16,
            rate_limit_per_window=60,
            rate_window_seconds=60.0,
            api_request_slice_timeout_seconds=15.0,
            worker_request_slice_timeout_seconds=60.0,
        ),
    ),
    controller=ManualEdgePauseController(),
)
```

## 4. Runtime Caps & Manual Pause Switch Guarantees
- **Body Size Cap (`413 REQUEST_BODY_TOO_LARGE`)**: Enforced on declared `Content-Length` AND incrementally inside ASGI `receive()` so chunked/streamed oversized bodies are terminated before route handler buffering.
- **Concurrency Cap (`503 CONCURRENCY_CAP_REACHED`)**: Non-blocking bounded slot counter returns `503` + `Retry-After` + `free_alternatives` instead of unbounded queueing.
- **Rate Limit (`429 RATE_LIMIT_EXCEEDED`)**: Thread-safe injectable-clock sliding window keyed by redacted actor/workspace hash or verified client IP.
- **Slice Timeout (`504 REQUEST_SLICE_TIMEOUT`)**: `15.0s` default for API routes and `60.0s` default for internal worker routes.
- **Manual Pause / Resume (`ManualEdgePauseController`)**: `pause(reason_code, actor_id)` engages `PAUSED_COST_GUARD` or `PAUSED_INCIDENT`; `resume(actor_id, budget_verified=False)` raises `ManualResumeRejectedError` and keeps the pause intact until `budget_verified=True`.

## 5. CI Trust Boundary Auditor (`INF-09`)
```python
from pathlib import Path
from career_memory_api.edge_guard import (
    audit_workflow_file,
    audit_workflows_directory,
    validate_release_workflow_manifest,
)
```
- Verifies explicit minimal `permissions: contents: read`, rejects `write-all` / `id-token: write` on `pull_request` / `pull_request_target`, rejects `pull_request_target`, blocks cloud deploy secret references (`GCP_SA_KEY`, `CLOUDFLARE_API_TOKEN`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`), and asserts immutable M0 OpenAPI blob SHA `07594d75425744d6149250a9f7ff95ac24659f80`.
- Provides `validate_release_workflow_manifest()` to gate future staging/release workflows on `concurrency` (`cancel-in-progress: false`), `@sha256:<64-hex>` image digest pinning, 40-hex action commit SHA pinning, and OIDC WIF (zero static `GCP_SA_KEY`).
