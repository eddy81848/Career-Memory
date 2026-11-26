# T-007D Hosted Verification Runbook (Staging / Hosted Supabase + Cloud Run)

> **Important Scope Boundary**: Local T-007D tests verify PostgreSQL 18 role privileges, `FORCE ROW LEVEL SECURITY`, composite FK enforcement, local single-connection reuse (`psycopg_pool(max_size=1)`), synthetic RS256 JWT validation, server-side membership lookup, and synthetic BFF cookie/Origin/CSRF enforcement. **None of the items below are verified by local tests** and MUST be executed in a staging environment before closing **OD-006** and **OD-010**.

---

## 1. Hosted Pooler Mode Verification (OD-006)

Do **not** equate local `psycopg_pool(max_size=1)` connection reuse with Supabase Supavisor testing.

### 1.1 Supavisor Transaction Mode (`:6543`) — Runtime API & Worker Path
- **Target**: Cloud Run API (`api_app` role) and Worker (`worker_app` role) connecting via Supavisor Transaction Pooler (`port 6543`).
- **Checks**:
  1. Disable server-side prepared statements in driver (`psycopg`: `prepare_threshold=None` / `prepare_threshold=0` or SQLAlchemy `prepared_statement_cache_size=0`) and verify repeated parameterized queries do not fail with `prepared statement "..." already exists`.
  2. Verify every workspace-scoped query runs inside an explicit transaction using `SELECT set_config('app.current_workspace_id', $1, true)` (`is_local = true`).
  3. Confirm zero usage of session-level `SET`, `LISTEN/NOTIFY`, `TEMP TABLE`, or session-level advisory locks on the transaction pooler path.
  4. Run concurrent interleaved Workspace A / Workspace B requests across pooler slots and verify 0 cross-workspace reads/writes and 0 `app.current_workspace_id` carryover when a transaction aborts mid-flight.

### 1.2 Session Mode (`:5432`) / Direct Connection — Migrations & DDL Only
- **Target**: CI/CD migration runner (`migrator` role ONLY).
- **Checks**:
  1. Confirm `migrator` connects over Session Pooler or Direct IPv6/IPv4 endpoint, never shared with Cloud Run runtime secrets.
  2. After every migration in CI, execute an automated audit query checking `pg_class` (`relrowsecurity = true AND relforcerowsecurity = true`), `pg_roles` (`rolbypassrls = false AND rolsuper = false` for runtime roles), and `pg_constraint` (composite FK presence on every child tenant table).

---

## 2. TLS, Network & Connection Sizing Verification (OD-006)

- **TLS**:
  1. Enforce `sslmode=verify-full` (or at minimum `verify-ca`) using Supabase's root CA bundle in Cloud Run container images; reject downgrade to plaintext (`sslmode=disable`) or unverified `require` where MITM hostname checks are skipped.
- **Connectivity & Limits**:
  1. Verify Cloud Run egress path (IPv4 vs IPv6 compatibility with Supavisor pooler vs Direct DB host).
  2. Measure `max_connections` / pooler client cap under realistic Cloud Run auto-scaling (`max_instances × app_pool_max_size`).
  3. Verify `statement_timeout`, `idle_in_transaction_session_timeout`, and connection acquire timeout prevent stuck transactions from exhausting pooler slots.

---

## 3. Supabase Auth JWT JWKS & Key Rotation Verification (OD-010)

- **Asymmetric JWKS Verification**:
  1. Confirm Supabase project signing algorithm (asymmetric ECC/RSA via `https://<project-ref>.supabase.co/auth/v1/.well-known/jwks.json` vs legacy HS256 JWT secret).
  2. Verify API JWT middleware validates `iss` (`https://<project-ref>.supabase.co/auth/v1`), `aud` (`authenticated`), `exp`, `alg` allowlist, and `kid` lookup against cached JWKS.
  3. Test key rotation / unknown `kid`: confirm JWKS cache refreshes safely with rate limiting and rejects forged/unlisted `kid` tokens.
  4. Confirm `service_role` tokens and `anon` tokens are rejected on user-scoped endpoints (`aud`/`role` enforcement).

---

## 4. Real Logout, Refresh Token Revocation & Stateless Access JWT Lifetime (OD-010)

- **Critical Reality Check**:
  - Local T-007D tests explicitly demonstrate that deleting a BFF session cookie does **not** automatically invalidate an already-issued stateless JWT unless a server-side denylist/session check is enforced.
- **Staging Verification Steps**:
  1. Execute real `supabase.auth.signOut({ scope: 'global' })` and `scope: 'local'` flows.
  2. Verify refresh token rotation fails immediately after sign-out (cannot mint a new access JWT).
  3. Capture an access JWT issued before sign-out and confirm whether:
     - Backend API relies on short access JWT TTL (e.g., 5–15 min) + BFF cookie deletion (browser cannot call BFF), OR
     - Backend API performs an explicit session/revocation check (e.g., checking Supabase `auth.sessions` / Redis denylist / server-side session version) for high-sensitivity endpoints.
  4. Verify DB membership revocation (`workspace_memberships.revoked_at`) immediately blocks workspace access even while the user's Supabase Auth JWT has not yet expired.

---

## 5. Hosted OAuth / Magic-Link Callback, Cookie Security & CSRF Verification (OD-010)

- **Cookie Attributes in Real Browser over HTTPS**:
  1. Verify `__Host-` cookie prefix (or `Secure; HttpOnly; SameSite=Lax; Path=/`) works behind Cloud Run / custom domain TLS termination.
  2. Confirm browser DevTools (`document.cookie`, `localStorage`, `sessionStorage`) never expose backend Bearer JWTs or refresh tokens.
- **Auth Callback & Redirect Chain**:
  1. Verify PKCE (`code_verifier` cookie + `code` exchange on server route) and `state` validation succeed on real OAuth/Magic-Link redirects (`SameSite=Lax` allows top-level GET callback navigation).
  2. Verify open-redirect protection on post-login `next`/`redirect_to` parameters.
- **CSRF & Proxy Headers**:
  1. Verify `Origin` / `Host` / `X-Forwarded-Host` normalization behind Cloud Run / CDN proxies.
  2. Verify cross-site form POST and cross-origin `fetch(..., { credentials: 'include' })` are rejected with `403` before reaching Backend API.
  3. Verify concurrent-tab token refresh in Next.js App Router (Middleware + Route Handlers + RSC) does not cause spurious logouts due to refresh-token reuse detection.

---

## 6. Strict Separation of Concerns Guardrail

Verify across both staging code and token inspection that **three authorization planes remain completely separate**:

| Plane | Authority | Storage / Transport | Must NOT Be Mixed With |
| --- | --- | --- | --- |
| **1. User Login Session** | Supabase Auth (`sub`, `session_id`) + DB `workspace_memberships` | Web BFF `HttpOnly` cookie → Backend `Bearer` JWT | GitHub ingestion tokens, LLM plan quotas |
| **2. GitHub Ingestion Grant (OD-011)** | GitHub App installation / scoped OAuth grant per workspace/connection | Encrypted server-side credential store / vault | Login cookie, Supabase Auth JWT claims |
| **3. LLM Plan / Entitlements** | Billing / subscription / quota service per workspace | Server-side entitlement table checked by LLM gateway | Login JWT claims or DB RLS tenant role |
