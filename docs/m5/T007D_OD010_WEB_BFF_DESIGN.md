# T-007D / OD-010 Next.js Server-Mediated Auth BFF & Standalone Cloud Run Container Design

> **Issue**: `#38` (Track 2 Wave 2B)  
> **Scope**: `apps/web` standalone container (`linux/amd64`, `OD-013` / `OD-015`) and server-mediated Supabase Auth PKCE + encrypted `HttpOnly` cookie BFF (`OD-010`)  
> **Contract**: `contracts/import-v1/openapi.yaml` unchanged (`07594d75425744d6149250a9f7ff95ac24659f80`)

---

## 1. Architecture Overview

In hosted mode (`NEXT_PUBLIC_API_MODE=hosted`), the browser **never** holds raw Supabase `access_token` or `refresh_token` values in JavaScript memory, `localStorage`, `sessionStorage`, or non-`HttpOnly` cookies. All browser API traffic stays strictly same-origin through Next.js Route Handlers:

```text
Browser (Zero raw tokens, zero browser storage)
  │
  ├─ GET  /auth/login?next=...      ──▶ Encrypted one-time PKCE cookie + 302 Supabase /auth/v1/authorize (S256)
  ├─ GET  /auth/callback?code&state ──▶ Server-to-server PKCE exchange + Encrypted __Host-cm_session HttpOnly cookie
  ├─ GET  /auth/session             ──▶ Single-flight refresh + GET $API_ORIGIN/v1/session (Bearer + X-CM-Origin-Auth)
  │                                     Returns { authenticated, csrf_token, session } (zero tokens)
  ├─ POST /auth/logout              ──▶ Origin + CSRF guard + POST $SUPABASE_URL/auth/v1/logout + Clear cookie
  ├─ *    /bff/v1/[...path]         ──▶ Origin + CSRF guard (mutations) + Single-flight refresh
  │                                     + Server-only Authorization: Bearer & X-CM-Origin-Auth -> $API_ORIGIN/v1/...
  └─ GET  /healthz                  ──▶ 200 {"status":"ok","service":"career-memory-web"} (Cache-Control: no-store)
```

### Runtime Mode Isolation (`next.config.ts`)
- **`NEXT_PUBLIC_API_MODE=mock` (default)**: Preserves 100% of existing fixture-driven UI and Playwright E2E tests unchanged.
- **`NEXT_PUBLIC_API_MODE=live`**: Preserves 100% of Track 1 local PostgreSQL 18 fullstack `beforeFiles` rewrites (`/v1/:path*`, `/healthz`, `/local-dev/:path*`) so `scripts/run_local_fullstack.py --smoke-check` remains untouched.
- **`NEXT_PUBLIC_API_MODE=hosted`**: Emits empty `rewrites()`, ensuring `/local-dev/:path*` and unauthenticated `/v1/:path*` passthrough are **never** exposed. All authenticated traffic flows through `/auth/*` and `/bff/v1/*`.

---

## 2. Cryptographic & Security Invariants (`apps/web/src/server/bff/`)

| Control | Implementation (`node:crypto`, zero extra deps) |
| --- | --- |
| **Session Cookie Encryption** | AES-256-GCM with 12-byte random IV, 16-byte auth tag, HKDF-SHA256 derived 32-byte key from `CAREER_MEMORY_BFF_COOKIE_SECRET` (min 32 chars, fail-closed `503 BFF_NOT_CONFIGURED`), and purpose-bound AAD (`cm_session_v1` vs `cm_pkce_v1`) preventing cookie substitution. |
| **Cookie Naming & Attributes** | HTTPS (`CAREER_MEMORY_COOKIE_SECURE=true` or `https://` public origin): `__Host-cm_session` & `__Host-cm_pkce` with `HttpOnly; Secure; SameSite=Lax; Path=/` and **no `Domain` attribute**. Local HTTP loopback test mode (`CAREER_MEMORY_COOKIE_SECURE=false` on `127.0.0.1`/`localhost` only): `cm_session` & `cm_pkce` with `HttpOnly; SameSite=Lax; Path=/`. |
| **CSRF Defense (`OD010-3`)** | On `POST /auth/logout` and `POST\|PATCH\|PUT\|DELETE /bff/v1/*`: strict `Origin === CAREER_MEMORY_PUBLIC_WEB_ORIGIN` (never trusts `X-Forwarded-Host`), `Sec-Fetch-Site` rejects `cross-site`/`same-site`, plus constant-time verification of session-bound `X-CSRF-Token = HMAC-SHA256(csrfKey, session_id + ":" + issued_at)` before touching Backend API. |
| **PKCE & Open-Redirect Defense (`OD010-1`)** | RFC 7636 32-byte `code_verifier` (`S256`), 24-byte `state`, encrypted 300s PKCE cookie cleared on every callback exit path, plus server-side `ConsumedPkceStateStore` enforcing at-most-once callback consumption even on cookie replay (`400 PKCE_STATE_MISMATCH`). `?next=` allowlists only single-leading-slash relative paths, rejecting `//`, `https://`, `\`, `..`, control chars, `/auth/*`, `/bff/*` (`400 INVALID_RETURN_PATH`). |
| **Single-Flight Refresh (`OD010-4`)** | `SingleFlightRefreshCoordinator` keyed by `session_id` (plus short recent-rotation deduplication window) guarantees concurrent browser tabs share **exactly 1** upstream `POST /auth/v1/token?grant_type=refresh_token` call. `session_id` and `issued_at` are preserved across silent refresh so open tabs' CSRF tokens stay valid. |
| **Origin Auth Signing (`OD-013` / `T-507`)** | `X-CM-Origin-Auth: t=<unix_ts>,sig=<64-hex>` computed as `HMAC-SHA256(CAREER_MEMORY_ORIGIN_HMAC_SECRET, "${ts}:${METHOD}:${pathname}")` where `pathname` is strictly `/v1/...` **without query string**, matching `apps/api/src/career_memory_api/edge_guard/models.py::sign_origin_auth_header` byte-for-byte. Sent only on server-to-API requests; browser `Cookie` and `Authorization` headers are stripped. |
| **401 / 503 Cookie Discipline (`OD010-5`)** | `POST /auth/logout` revokes GoTrue (`scope=local\|global`) and clears cookie. Backend `401 SESSION_TERMINATED` clears cookie and purges coordinator cache. Backend or auth `503 AUTH_VERIFICATION_UNAVAILABLE` **preserves** the web session cookie (`Set-Cookie` not cleared) and performs **0 automatic `POST` retries**. |
| **Private No-Store Caching** | All `/auth/*` and `/bff/v1/*` responses set `Cache-Control: private, no-store`, `CDN-Cache-Control: no-store`, `Vary: Authorization, Cookie, X-Workspace-ID, Origin`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. |

---

## 3. `OD010-1..6` Verification Matrix (Local Automated Evidence, `₩0` Spend)

| Requirement | Covered By | Verified Invariants |
| --- | --- | --- |
| **OD010-1 (PKCE Login, Callback, Open Redirect)** | `bff-security-and-proxy.test.ts` | S256 PKCE login -> callback happy path; replay fails `400 PKCE_STATE_MISMATCH`; tampered `state` rejected; `//evil.example`, `https://evil.example`, `/\evil`, `/auth/callback` rejected with `400 INVALID_RETURN_PATH`. |
| **OD010-2 (Zero Token Exposure & Origin Auth)** | `bff-security-and-proxy.test.ts`, `HostedBffShell.test.tsx` | Raw `access_token`/`refresh_token` absent from `/auth/session` JSON, plaintext `Set-Cookie`, and browser `fetch` headers; `X-CM-Origin-Auth` matches canonical `/v1/<path>` without query string; service login kept separate from GitHub repository ingestion (`OD-011`). |
| **OD010-3 (HttpOnly/Secure Cookie & Origin/CSRF)** | `bff-security-and-proxy.test.ts`, `HostedBffShell.test.tsx` | `__Host-cm_session` (`HttpOnly; Secure; SameSite=Lax; Path=/`, no `Domain`); cross-origin `Origin`, `Sec-Fetch-Site: cross-site`, and missing/invalid `X-CSRF-Token` return `403 CSRF_VALIDATION_FAILED` with 0 backend API calls. |
| **OD010-4 (Single-Flight Concurrent Tab Refresh)** | `bff-security-and-proxy.test.ts` | 4 simultaneous expired-token `/bff/v1/*` requests trigger **exactly 1** upstream refresh call, rotate encrypted `Set-Cookie`, and preserve session-bound CSRF token. |
| **OD010-5 (Logout, 401 Clear, 503 Preservation)** | `bff-security-and-proxy.test.ts`, `HostedBffShell.test.tsx` | `POST /auth/logout` revokes GoTrue + clears cookie; backend `401 SESSION_TERMINATED` clears cookie; backend `503 AUTH_VERIFICATION_UNAVAILABLE` preserves cookie and executes 0 automatic `POST` retries. |
| **OD010-6 (Workspace Scope Isolation)** | `HostedBffShell.test.tsx` + Backend API `GET /v1/session` & RLS | BFF forwards `X-Workspace-ID` without caching tenant state; Backend API enforces active membership & PostgreSQL `FORCE RLS` per request. |

---

## 4. Production Standalone Container (`apps/web/Dockerfile`)

- Base image: `node:22-slim` (`linux/amd64` compatible) in a 3-stage build (`deps` -> `builder` -> `runner`).
- `next.config.ts` sets `output: "standalone"` so `next build` emits `.next/standalone/server.js` for fast `min-instances=0` Cloud Run cold starts (`OD-015`).
- `runner` stage runs as non-root user `nextjs` (`UID 10001`), binds `HOSTNAME=0.0.0.0`, `PORT=8080`, and exposes `GET /healthz` returning `200 {"status":"ok","service":"career-memory-web"}` with `Cache-Control: no-store`.
