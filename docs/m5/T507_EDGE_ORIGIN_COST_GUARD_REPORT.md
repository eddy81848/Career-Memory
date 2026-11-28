# M5 `T-507` Edge Ingress, Origin Auth, Cache Isolation, CI Trust & Cost Guard Report (`INF-07`, `INF-09`, `SEC-01`)

- **Task / Issue**: `T-507` / Issue `#26`
- **Package**: `apps/api/src/career_memory_api/edge_guard/`
- **Evidence Contract**: `contracts/edge-guard/t507_route_trust_matrix.v1.json`
- **External Cloud Calls Executed**: `0` (`₩0` spend)
- **Hosted Public Pilot Gate Status**: **`HELD (NO-GO)` — `HELD_NO_GO_PENDING_OD013_OD015`**

---

## 1. Route & Trust Boundary Matrix (`INF-07`)

All incoming HTTP requests are classified fail-closed by `classify_route()` before route dispatch:

| Trust Class | Routes | Auth & Ingress Guard | Enforced Response Headers (2xx/4xx/5xx) | `PAUSED_COST_GUARD` Behavior |
|---|---|---|---|---|
| `PUBLIC_HEALTH` | `GET /healthz`, `HEAD /healthz` | No origin auth required (Cloud Run startup/liveness probes) | `Cache-Control: no-store`, `CDN-Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` | **ALLOWED** |
| `AUTHENTICATED_API` | Declared `/v1/**` resource prefixes only (`/v1/sources/github`, `/v1/source-accounts`, `/v1/source-resources`, `/v1/deletion-jobs`, `/v1/sync-runs`, `/v1/content-units`, `/v1/work-events`, `/v1/identities`, `/v1/facts`, `/v1/claims`, `/v1/exports`, `/v1/workspaces`, `/v1/me`, `/v1/session`) | Requires `X-CM-Origin-Auth` (HMAC-SHA256 timestamped signature or constant-time rotation ring) OR trusted-proxy `X-Forwarded-Client-Cert` (mTLS); rejects direct `*.run.app` bypass (`403 ORIGIN_AUTH_REQUIRED`); enforces single-origin CORS & CSRF `Origin`/`Sec-Fetch-Site` checks (`403 CSRF_ORIGIN_DENIED` / `CORS_ORIGIN_DENIED`) | Strips any handler `public` cache/surrogate headers and forces `Cache-Control: private, no-store`, `CDN-Cache-Control: no-store`, `Pragma: no-cache`, `Vary: Authorization, Cookie, X-Workspace-ID, Origin`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` | **High-cost / mutable growth routes BLOCKED** (`503 RUNTIME_PAUSED_COST_GUARD` + `Retry-After` + `free_alternatives`, `paid_fallback_allowed: false`); **essential safety & user-rights routes ALLOWED** |
| `INTERNAL_WORKER_ONLY` | `/internal/worker/**` | Immediately denies ANY edge/browser/session marker (`CF-Connecting-IP`, `CF-Ray`, `CF-Visitor`, `X-CM-Origin-Auth`, `Cookie`, `Origin`, `Sec-Fetch-Site`, `Sec-Fetch-Mode`, `Referer`) with `403 PUBLIC_WORKER_ROUTE_FORBIDDEN`; requires verified internal IAM/OIDC audience + issuer + service account token | `Cache-Control: private, no-store`, `CDN-Cache-Control: no-store`, `Pragma: no-cache`, `Vary: ...`, security headers | **BLOCKED** (`503 RUNTIME_PAUSED_COST_GUARD`) |
| `DENIED_UNDECLARED` | Any path outside allowlisted prefixes, explicitly prohibited prefixes (`/debug`, `/admin`, `/metrics`, `/docs`, `/redoc`, `/openapi.json`, `/v1/byok`, `/v1/llm/**`), or unsafe path syntax (`..`, `%2e`, `//`, `\`) | Fail closed (`404` or configured `403`, `ROUTE_NOT_DECLARED`) before handler dispatch | `Cache-Control: private, no-store`, `CDN-Cache-Control: no-store`, security headers | **ALWAYS DENIED** |

---

## 2. Verified App-Layer Protections (`INF-07`, `SEC-01`, `ADR-020`, `OD-015`)

All protections are implemented in pure ASGI (`EdgeOriginAndCacheMiddleware`, composable via `install_edge_guard(app, config, controller)` without modifying `app.py` in Wave 1B) and verified with automated tests in `apps/api/tests/edge_guard/`:

1. **Direct `*.run.app` Bypass & Origin Authentication (`INF07-01`..`INF07-03`)**:
   - Missing or invalid `X-CM-Origin-Auth` returns `403 ORIGIN_AUTH_REQUIRED` with `Cache-Control: private, no-store`.
   - Direct `Host: *.run.app` requests are rejected (`403 ORIGIN_AUTH_REQUIRED`).
   - Supports zero-downtime secret rotation rings (constant-time `hmac.compare_digest`), timestamped cryptographic HMAC-SHA256 signatures (`sign_origin_auth_header`), and `X-Forwarded-Client-Cert` mTLS validation strictly gated on direct peer IP membership in `trusted_mtls_proxy_ips`.
   - Fails closed if `require_origin_auth=True` and zero secrets/mTLS rules are configured.
2. **Private Internal Worker Isolation (`INF07-04`)**:
   - `/internal/worker/**` rejects any request carrying Cloudflare, browser, or session headers (`CF-*`, `X-CM-Origin-Auth`, `Cookie`, `Origin`, `Sec-Fetch-*`, `Referer`) with `403 PUBLIC_WORKER_ROUTE_FORBIDDEN` even if a valid worker token is attached.
   - Missing, expired, wrong-audience, wrong-issuer, or unallowlisted service-account tokens are rejected fail-closed.
3. **Session / Tenant & Plan C Ephemeral Cache Isolation (`INF07-05`)**:
   - Deliberate handler attempts to emit `Cache-Control: public, max-age=86400`, `CDN-Cache-Control: public`, or `Surrogate-Control` are stripped and overwritten with `Cache-Control: private, no-store` and `CDN-Cache-Control: no-store` across **all 2xx, 4xx, and 5xx** responses.
   - `Vary: Authorization, Cookie, X-Workspace-ID, Origin` guarantees tenant/session separation; unhandled exceptions are caught and redacted so credentials never leak into error bodies.
4. **Strict Single-Origin CORS & CSRF Guard (`INF07-07`)**:
   - Rejects wildcards (`*`) and trailing slashes at config load time; preflight `OPTIONS` emits exact single-origin `Access-Control-Allow-Origin` only for `allowed_web_origins`.
   - Blocks untrusted `Origin`, `Sec-Fetch-Site: cross-site`, and cookie-authenticated state-changing requests missing `Origin` with `403 CSRF_ORIGIN_DENIED`.
5. **Compute, Memory, Concurrency, Rate & Timeout Caps (`INF07-08`..`INF07-11`)**:
   - **Body Size Cap (`413 REQUEST_BODY_TOO_LARGE`)**: Default `262_144` bytes (256 KiB); rejects oversized `Content-Length` before handler entry AND enforces cumulative streamed byte count inside ASGI `receive()` to stop chunked/spoofed payloads before buffering.
   - **Concurrency Cap (`503 CONCURRENCY_CAP_REACHED`)**: Non-blocking bounded slot counter (`default=16`) emits `Retry-After` + redacted `free_alternatives` (`paid_fallback_allowed: false`) with zero unbounded queueing.
   - **Sliding-Window Rate Limiter (`429 RATE_LIMIT_EXCEEDED`)**: Injectable-clock limiter keyed by redacted SHA-256 hash of actor/workspace credential or verified client IP; `/healthz` remains exempt.
   - **Request Slice Timeout (`504 REQUEST_SLICE_TIMEOUT`)**: Bounded `15.0s` default for API routes and `60.0s` for worker slices, cleanly releasing concurrency slots on timeout.
6. **Manual Emergency Pause / Spend Cutoff Switch (`INF07-12`)**:
   - `ManualEdgePauseController.pause(reason_code, actor_id)` engages `PAUSED_COST_GUARD` (or `PAUSED_INCIDENT`) with redacted audit logging.
   - `resume(actor_id, budget_verified=False)` raises `ManualResumeRejectedError` and keeps the pause active until `budget_verified=True`.
   - Under `PAUSED_COST_GUARD`:
     - **Blocked (`503 RUNTIME_PAUSED_COST_GUARD`, `paid_fallback_allowed: false`, `free_alternatives`)**: `POST /v1/sync-runs`, `POST /v1/sync-runs/{id}/retry`, `POST /v1/work-events/{id}/claims:generate`, `POST /v1/exports`, and other non-essential mutations.
     - **Preserved (`200 OK`)**: `GET /healthz`, `POST /v1/source-accounts/{id}/disconnect`, `POST /v1/source-resources/{id}/deletion-preview`, `POST /v1/source-resources/{id}/deletion-confirm`, `GET /v1/deletion-jobs/{id}` (`OD-003` user-rights preservation).

---

## 3. GitHub Actions CI Trust Boundary Audit (`INF-09`)

Executed deterministically offline via `career_memory_api.edge_guard.ci_trust_audit` against `.github/workflows/pr-conformance-local.yml`:

| Check | Result | Observed Evidence |
|---|---|---|
| Explicit minimal `permissions:` | **PASS** | `permissions: { contents: read }` (no `write-all`, no `id-token: write` on `pull_request`) |
| Absence of `pull_request_target` | **PASS** | Triggers are `pull_request` and `workflow_dispatch` only |
| Zero cloud deploy secrets (`GCP_SA_KEY`, `CLOUDFLARE_API_TOKEN`, `SUPABASE_SERVICE_ROLE_KEY`, `OPENAI_API_KEY`) | **PASS** | `0` secret references detected |
| Immutable M0 OpenAPI blob pin (`contracts/import-v1/openapi.yaml`) | **PASS** | Verified SHA `07594d75425744d6149250a9f7ff95ac24659f80` |
| Future staging/release manifest validator (`validate_release_workflow_manifest`) | **PASS** | Enforces `concurrency` (`cancel-in-progress: false`), `@sha256:<64-hex>` image digest pinning, 40-hex action commit SHA pinning, and OIDC WIF (blocks static `GCP_SA_KEY` and PR triggers) |

---

## 4. Honest `OD-013` / `OD-015` `₩0` Hosting Assessment (`HELD / NO-GO`)

Although all app-layer (`INF-07`) and CI workflow (`INF-09`) controls are implemented and verified locally at `₩0`, **live public internet exposure on Cloudflare + Google Cloud Run + Supabase Free remains strictly `HELD (NO-GO)`** until the human operator completes and records the following external infrastructure prerequisites:

1. **Billing & Hard Spend Cutoff (`OD-015`, `ADR-020`)**:
   - GCP Cloud Billing alerts are notification-only by default and do **not** automatically cap Cloud Run / egress / Artifact Registry spend at `₩0`. Live exposure requires explicit operator-verified billing caps/circuit-breaker automation or explicit budget approval, plus Cloud Run `max-instances` / `concurrency` / request timeout bounds configured in the service spec.
2. **Edge-to-Origin Cryptographic Lock (`INF-07`, `OD-013`)**:
   - Cloudflare Proxy / Transform Rule / mTLS Authenticated Origin Pulls and Cloud Run ingress/IAM restrictions must be provisioned and live-probed to confirm direct `*.run.app` internet access is blocked at the platform layer (not only inside FastAPI middleware) and `/internal/worker/**` is isolated on a private IAM-only service (`--no-allow-unauthenticated`).
3. **Supabase Free Project & Data Boundary (`OD-013`, `OD-014`)**:
   - Hosted Supabase Auth redirect allowlists, RLS policies, connection pooler limits, and free-tier inactivity/egress constraints must be verified against a live project before any external user traffic is accepted.

### Zero-Cost Local Demo & Evaluation Path (`₩0`)
Until `OD-013` and `OD-015` are explicitly cleared by the operator, all demos, integration tests, conformance checks, and grounded claim evaluations run 100% locally at `₩0` using:
- Ephemeral local PostgreSQL 18 (`scripts/run_conformance_ci.py --with-postgres`),
- Pure ASGI FastAPI + `EdgeOriginAndCacheMiddleware` with offline unit/integration suites (`uv run pytest -v -m ""`),
- Offline synthetic fixtures (`LlmProviderMode.SYNTHETIC_FIXTURE_OFFLINE`), making **0 external cloud or paid API calls**.
