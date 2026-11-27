# T-007D Local Auth & PostgreSQL Workspace Isolation Spike

Isolated security/backend prototype validating **PostgreSQL least-privilege roles, RLS + Composite FK workspace isolation, single-connection pool reuse, and server-mediated session/JWT/BFF boundaries** using synthetic data, an ephemeral local PostgreSQL 18 cluster, and an optional ephemeral local Supabase CLI stack.

> **Status & Boundaries**
> - **Gate Status**: Hosting verification remains **`NO-GO`**; **OD-006 / OD-010** remain **`Validation Required`**.
> - **Approval vs. OD Decision Order**: Confirm free-tier headroom, permissions, and cleanup budget and obtain **isolated hosted execution approval → run M0 candidate verification → review OD-006/010 adoption using the M0 evidence**. OD adoption is **not** a prerequisite for M0 candidate testing; product API, DB write, Job/outbox, and idempotency integration tests remain in M1/M2.
> - **Not Production Code**: Everything in `spikes/t007d-auth-db/` uses synthetic schemas (`app_t007d`, `auth_ctx`), synthetic roles (`t007d_*`), and ephemeral local credentials. No product User/Identity tables, shared auth modules, or hosted Supabase projects are modified.
> - **Recorded Local Run (AUTHDB-04)**: 27 passed / 0 failed / 0 skipped (`bash spikes/t007d-auth-db/scripts/run_local_verification.sh`, cited from the AUTHDB-04 execution record; not re-run during doc sync).

---

## 1. Local Reproduction

### Prerequisites
- Local PostgreSQL 18 binaries (`initdb`, `pg_ctl`, `psql`, `createdb`) — auto-discovered from `PG_BIN_DIR`, `/opt/homebrew/opt/postgresql@18/bin`, or `PATH`.
- Python 3.13+ (`python3.13` or `python3`).
- For the 4 local Supabase CLI integration tests (`test_local_supabase_integration.py`, `test_local_supabase_fastapi_e2e.py`): running local Docker Engine and `supabase` CLI (ephemeral isolated project `t007d-authdb-ephemeral`).

### One-Command Run
```bash
bash spikes/t007d-auth-db/scripts/run_local_verification.sh
```

### Manual Run (with Existing Virtualenv)
```bash
venv_dir="$(mktemp -d)"
python3.13 -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install -r spikes/t007d-auth-db/requirements.txt
"$venv_dir/bin/pytest" -v spikes/t007d-auth-db/tests
rm -rf "$venv_dir"
```

The pytest session fixture (`tests/conftest.py`) initializes an isolated temporary PostgreSQL 18 cluster on an ephemeral `127.0.0.1` port, applies `sql/001_roles.sql` → `002_schema_and_rls.sql` → `003_seed_synthetic.sql`, runs the local PG/BFF tests (and, when Docker/Supabase CLI is available, the ephemeral local Supabase CLI tests), and stops/cleans up ephemeral test resources on exit.

---

## 2. Role & Privilege Matrix Verified Locally

| Role | `SUPERUSER` / `BYPASSRLS` | Table Owner | `users` | `workspaces` / `workspace_memberships` | `projects` (Parent) | `artifacts` (Child) | Runtime Use |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `t007d_migrator` | No / No | **Yes** | Full (DDL) | Full (DDL) | Full (DDL) | Full (DDL) | **CI/Migration ONLY** (never in API/Worker) |
| `t007d_api_app` | No / No | No | **Denied** | `SELECT` only (`workspace_memberships` RLS: `subject_id = current_subject_id() AND revoked_at IS NULL`) | Scoped CRUD (RLS) | Scoped CRUD (RLS) | Backend API runtime |
| `t007d_worker_app` | No / No | No | **Denied** | **Denied** | Scoped `SELECT` only (RLS) | Scoped `SELECT/INSERT/UPDATE` only (no `DELETE`) | Background Worker candidate |

Verified denials on runtime roles:
- `SET ROLE t007d_migrator` → `InsufficientPrivilege`
- `SET LOCAL row_security = off` + query → `InsufficientPrivilege`
- `CREATE TABLE` / DDL → `InsufficientPrivilege`
- Unscoped query (`app.current_workspace_id` unset or `''`) → **0 rows (fail-closed)**

---

## 3. Cross-Workspace Parent-Child Integrity Comparison

Tested in `tests/test_db_isolation_and_fk.py::test_cross_workspace_fk_candidates_isolated_comparison` against real PostgreSQL 18:

| Candidate | Table in Spike | Blocks Restricted API Cross-WS Insert? | Eliminates Parent UUID Existence Oracle? | Blocks Non-RLS / Backfill / Superuser Path? | Operational Trade-Offs |
| --- | --- | --- | --- | --- | --- |
| **Baseline (Single-column FK + RLS)** | `artifacts_single_fk_baseline` | **NO (VULNERABLE)** — PG RI trigger checks `projects(id)` as table owner bypassing RLS! | **NO** — Existing WS_A parent succeeds while missing UUID throws `23503` | **NO** | Unacceptable; proves RLS alone cannot guarantee cross-workspace referential integrity |
| **Candidate A: Composite FK `(workspace_id, project_id) -> projects(workspace_id, id)`** *(Recommended)* | `artifacts` | **YES** (`23503`) | **YES** — Identical `23503` for WS_A parent and nonexistent UUID | **YES** | Declarative (`pg_constraint`), native `FOR KEY SHARE` locking, requires `UNIQUE (workspace_id, id)` on parent |
| **Candidate B: Single FK + RLS `WITH CHECK EXISTS`** | `artifacts_rls_exists_candidate` | YES (`42501`) | Partial (different SQLSTATE `42501` vs `23503`) | **NO** — Superuser/backfill bypasses RLS and inserts corrupt cross-WS link | Subquery overhead on every write; does not protect migration/backfill paths |
| **Candidate C: Single FK + `BEFORE` Trigger** | `artifacts_trigger_candidate` | YES (`23503`) | YES (`23503`) | YES (unless trigger disabled) | Custom PL/pgSQL per child table; needs paired parent-update guard; hidden from FK metadata |

---

## 4. Connection Reuse & Pool Scoping Evidence (Local PG Only)

> **Scope Caveat**: Tested with `psycopg_pool.ConnectionPool(max_size=1)` directly against local PostgreSQL 18 (`pg_backend_pid()` verified identical across requests). This is **not** a hosted Supabase Supavisor (`:6543`) test, and `src/db.py::make_single_connection_pool` does not yet apply `prepare_threshold=None`. The `:6543` + `prepare_threshold=None` combination remains an **unverified hosted candidate (`NO-GO`)**.

| Pattern | Observed Behavior on Same `pg_backend_pid()` | Verdict |
| --- | --- | --- |
| `SELECT set_config('app.current_workspace_id', $1, true)` inside explicit transaction (`SET LOCAL` semantics) | WS_A → unscoped (0 rows) → WS_B → WS_A rollback → unscoped (0 rows): **0 leakage** across both API and Worker single-connection pools and HTTP endpoints | **Safe locally**; `:6543` + `prepare_threshold=None` still requires hosted M0 verification |
| `SELECT set_config('app.current_workspace_id', $1, false)` (session-level `SET`, no `RESET ALL`) | WS_A query leaks `PROJECT_A1` into the next unscoped transaction on the same `pg_backend_pid()` | **Unsafe Anti-Pattern** (and breaks Transaction Mode pooling) |

---

## 5. Auth / Session / Cookie / CSRF: Verified Locally vs. Hosted / Product Scope

> **Approved Error Contract Scope (`CONTRACT@87f1515`)**:
> - For the **four initial import routes ONLY** (`GET /v1/sync-runs`, `POST /v1/sync-runs`, `GET /v1/sync-runs/{id}`, `POST /v1/sync-runs/{id}/retry`), `CONTRACT@87f1515` (`contracts/import-v1/openapi.yaml`) approves four error responses — all requiring `Cache-Control: private, no-store`, keeping the `X-Request-ID` response header optional, requiring JSON `error.request_id`, and forbidding `details`: `401 SESSION_TERMINATED` (`retryable: false`, confirmed termination + re-login), `403 WORKSPACE_SCOPE_VIOLATION` (`retryable: false`, confirmed scope revocation + login preserved), `404 RESOURCE_NOT_FOUND` (`retryable: false`, missing/out-of-scope concealment), and `503 AUTH_VERIFICATION_UNAVAILABLE` (`retryable: true`, transient auth failure + session preserved + 0 sensitive writes/jobs; **not** a basis for automatic `POST` retries).
> - **General `401`/`403`/`422`, unapproved other `409`, Source Account/Resource responses, and `Retry-After` remain unfinalized.**
> - Local spike outputs (`SESSION_STATUS_UNAVAILABLE`, `WORKSPACE_FORBIDDEN` in `src/api_app.py`) remain test-harness labels only and are not renamed in spike code; see `HOSTED_VERIFICATION_RUNBOOK.md` §5 for the side-by-side mapping.

| Area | Verified Locally in T-007D (AUTHDB-04 Record, 27 Passed) | Unverified: Hosted M0 Spike (`NO-GO`) / M1–M2 Product Integration |
| --- | --- | --- |
| **JWT Claims & JWKS** | Synthetic RS256 negatives (bad sig, `HS256` confusion, wrong `iss`/`aud`, expired `exp`, mixed GitHub/LLM claims) rejected (`test_jwt_and_membership.py`); ephemeral local Supabase GoTrue real JWT verified in FastAPI via fixed-config JWKS (`jwks_url`, `expected_issuer`, `expected_audience`, `allowed_algorithms`) after reproducing the RS256-only gap (`test_local_supabase_fastapi_e2e.py`). *Local `ES256` and `jti` absence are local observations only, not product-wide assumptions.* | Hosted Supabase `/.well-known/jwks.json` `kid` rotation, cache, rate limits, and actual staging/prod signing config (`M0`); product `(auth_issuer, auth_subject)` user mapping (`M1/M2`) |
| **Server-Side Authorization & Revocation** | Current DB `workspace_memberships` checked on every request; spoofed body/JWT `workspace_id` rejected; live `revoked_at` immediately blocks unexpired JWT while preserving other active workspaces (`test_jwt_and_membership.py`, `test_od010_5_6_session_revocation.py`, `test_local_supabase_fastapi_e2e.py`); out-of-workspace IDs concealed identically to missing IDs. *(Cross-ref: FOUNDATION `c1e728a` local PG race test proves 0 rows on protected `SELECT` after revocation commit only — not multi-statement write transactions.)* | Multi-statement write transaction revocation races (lock vs conditional-write mechanism not yet finalized), full RBAC matrix, and initial-import 4-route contract + Job/outbox/idempotency atomicity (`M1/M2`) |
| **Data API (PostgREST) Bypass** | Local PG 18 Data API role layer (`test_od006_5_data_api_isolation.py`) and ephemeral local Supabase PostgREST (`/rest/v1/*`, `test_local_supabase_integration.py`): positive-control canary `200` for both `anon` and signed-in `authenticated`, followed by 4xx denial on `projects`/`artifacts` with 0 DB row/value mutations and successful server-mediated write | Hosted Supabase Exposed Schemas, `role_table_grants`, and staging `/rest/v1/*` bypass verification (`M0`) |
| **Browser → BFF → API & Cookies / CSRF** | Synthetic BFF `HttpOnly; SameSite=Lax` cookie, Bearer JWT hidden from responses, `Origin` allowlist + session-bound `X-CSRF-Token` on `POST` (`test_bff_session_csrf.py`) | Real Next.js App Router (Middleware/RSC/Route Handler) HTTPS OAuth/PKCE login/callback, `Secure`/`__Host-` cookie candidate, Cloud Run `X-Forwarded-Host` normalization (`M0`) |
| **Session Expiry, Logout & Transient Failures** | Expired synthetic JWT rejected; synthetic BFF cookie clearing + refresh denial + 4 sensitive paths fail-closed (`test_od010_5_6_session_revocation.py`); local GoTrue `logout?scope=local` refresh denial (`400`), unexpired JWT stateless pass vs live `/auth/v1/user` denial (`403 session_not_found`), Session 2 preserved (`200`), and E2E delete with 0 open DB transactions during `/auth/v1/user` plus timeout/503/ConnectError fail-closed (`test_local_supabase_integration.py`, `test_local_supabase_fastapi_e2e.py`) | Real access/refresh expiration, concurrent-tab refresh rotation, hosted `signOut({ scope: 'global' | 'local' })`, hosted `/auth/v1/user` latency/rate limits (`M0`), and `503 AUTH_VERIFICATION_UNAVAILABLE` WEB session preservation + 0 DB/Job writes on real import routes (`M1/M2`) |

See [HOSTED_VERIFICATION_RUNBOOK.md](./HOSTED_VERIFICATION_RUNBOOK.md) for the full OD-006 / OD-010 verification matrices, M0 vs M1/M2 split, approved initial-import error mapping, free-tier/cost guardrails, and hosted execution procedures.

---

## 6. Verification Candidates & Stage Split (Not Finalized Here)

### OD-006 (DB Isolation, Roles, Pooling & Data API Boundary — `Validation Required`)
1. **Separate 3 DB roles**: CI-only `migrator` (owner, Session `:5432` or Direct connection, never in Cloud Run runtime), `api_app` (`NOBYPASSRLS`, read-only self active memberships + tenant CRUD), and `worker_app` (`NOBYPASSRLS`, no identity/membership access, narrow domain privileges).
2. **Mandatory `ENABLE` + `FORCE ROW LEVEL SECURITY`** with fail-closed `auth_ctx.current_workspace_id()` on every tenant-bearing table, bound strictly via transaction-local `set_config(..., true)`.
3. **Mandatory Composite FK `(workspace_id, parent_id) -> parent(workspace_id, id)`** on all parent-child tenant tables (eliminates the PostgreSQL owner-RI cross-workspace vulnerability and UUID existence oracle).
4. **Pooling Candidate (Unverified in Hosting, `NO-GO`)**: Supabase shared Transaction Pooler (`:6543`) with **`psycopg` `prepare_threshold=None`** (do **not** use `prepare_threshold=0`, which forces immediate query preparation in psycopg3) for API/Worker runtime, and Session/Direct mode for CI migrations only — **requires M0 hosted verification in `HOSTED_VERIFICATION_RUNBOOK.md`**.
5. **Data API Bypass Prevention**: Verify in hosted staging (`M0`) that browsers cannot bypass backend authorization via Supabase Data API (PostgREST `/rest/v1/*`) using `anon` or `authenticated` credentials.
6. **M1/M2 Product Integration (Separate from M0)**: Real multi-statement write transactions (sync runs, Job/outbox, idempotency records) and membership revocation races during writes remain M1/M2 product verification items (without pre-locking a specific lock vs conditional-write implementation).

### OD-010 (Browser Session, Next.js Server Proxy & CSRF Boundary — `Validation Required`)
1. **Next.js Server Proxy + `HttpOnly`/`Secure` Cookie (Candidate Under Verification)**: Browser holds only an `HttpOnly`/`Secure` (`SameSite=Lax`, prod `__Host-` candidate) session cookie; raw Supabase access/refresh tokens stay server-side and are forwarded as `Authorization: Bearer` only on the Server → Backend API hop.
2. **Authoritative Backend Authorization**: Backend API verifies fixed-config Supabase JWKS (`iss`, `aud`, `exp`, `alg`) and resolves current active `workspace_memberships` in DB on every request; never trusts client-supplied `workspace_id` or JWT workspace claims alone.
3. **CSRF Defense**: Enforce `Origin` allowlist + synchronizer `X-CSRF-Token` (or Fetch Metadata + Origin) on state-changing BFF routes.
4. **Session Expiry, Logout & Transient Failure Policy**: Validate real access/refresh expiration, concurrent-tab refresh rotation, and hosted `signOut` / `/auth/v1/user` behavior in `M0` staging; verify the approved 4 initial-import error responses (including preserving WEB login on `503 AUTH_VERIFICATION_UNAVAILABLE` with 0 DB/Job writes and no automatic `POST`) in `M1/M2` product integration.
5. **Concern Separation**: Keep Supabase Auth service login sessions strictly separate from GitHub repository ingestion grants (OD-011 — including OAuth `state` one-time consumption and orphan secret cleanup) and LLM plan/billing entitlements.
