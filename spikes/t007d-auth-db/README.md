# T-007D Local Auth & PostgreSQL Workspace Isolation Spike

Isolated security/backend prototype validating **PostgreSQL least-privilege roles, RLS + Composite FK workspace isolation, single-connection pool reuse, and server-mediated session/JWT/BFF boundaries** using synthetic data and an ephemeral local PostgreSQL 18 cluster.

> **Not Production Code**: Everything in `spikes/t007d-auth-db/` uses synthetic schemas (`app_t007d`, `auth_ctx`), synthetic roles (`t007d_*`), and ephemeral local credentials. No product User/Identity tables, shared auth modules, or hosted Supabase projects are modified.

---

## 1. Local Reproduction

### Prerequisites
- Local PostgreSQL 18 binaries (`initdb`, `pg_ctl`, `psql`, `createdb`) — auto-discovered from `PG_BIN_DIR`, `/opt/homebrew/opt/postgresql@18/bin`, or `PATH`.
- Python 3.13+ (`python3.13` or `python3`).

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

The pytest session fixture (`tests/conftest.py`) initializes an isolated temporary PostgreSQL 18 cluster on an ephemeral `127.0.0.1` port, applies `sql/001_roles.sql` → `002_schema_and_rls.sql` → `003_seed_synthetic.sql`, runs all tests, and stops PostgreSQL cleanly on exit.

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

> **Scope Caveat**: Tested with `psycopg_pool.ConnectionPool(max_size=1)` on local PostgreSQL 18 (`pg_backend_pid()` verified identical across requests). This is **not** a Supabase hosted Supavisor pooler test.

| Pattern | Observed Behavior on Same `pg_backend_pid()` | Verdict |
| --- | --- | --- |
| `SELECT set_config('app.current_workspace_id', $1, true)` inside explicit transaction (`SET LOCAL` semantics) | WS_A → unscoped (0 rows) → WS_B → WS_A rollback → unscoped (0 rows): **0 leakage** across both API and Worker single-connection pools and HTTP endpoints | **Safe** & compatible with Supavisor Transaction Mode |
| `SELECT set_config('app.current_workspace_id', $1, false)` (session-level `SET`, no `RESET ALL`) | WS_A query leaks `PROJECT_A1` into the next unscoped transaction on the same `pg_backend_pid()` | **Unsafe Anti-Pattern** (and breaks Transaction Mode pooling) |

---

## 5. Auth / Session / Cookie / CSRF: Verified Locally vs. Design Candidates

> **Non-Finalized API Error Contract Note**: HTTP status codes shown below (`401`/`403`/`404`) are **local synthetic spike observations only** and do not lock unapproved product API error status codes or error payloads.

| Area | Verified Locally in T-007D | Design Candidate / Hosting-Required Only |
| --- | --- | --- |
| **JWT Claims & Signature** | RS256 signature, wrong key, tampered sig, `alg` confusion (`HS256`), wrong `iss`, wrong `aud`, expired `exp`, mixed-concern claims (`github_token`, `llm_plan`) all rejected in local spike (`401`) | Live Supabase `/.well-known/jwks.json` fetch, `kid` rotation, ECC/RSA prod config, `anon`/`service_role` rejection |
| **Server-Side Authorization** | Current DB `workspace_memberships` checked on every request; body `workspace_id` spoof, JWT `workspace_id` claim spoof, live DB revocation (`revoked_at`) immediately blocks unexpired JWT, out-of-workspace IDs concealed identically to missing IDs in local spike | Production RBAC role matrix (`owner`/`member`/`viewer`) per endpoint and approved API error contract |
| **Browser → BFF → API Boundary** | `HttpOnly; SameSite=Lax` cookie set by synthetic BFF; raw Bearer JWT never returned in BFF responses; BFF attaches `Authorization: Bearer` only on server-to-API hop | Next.js App Router (Middleware / RSC / Route Handler) verification candidate & real Supabase login/PKCE callback |
| **Cookies & CSRF** | Local `Origin` allowlist + session-bound `X-CSRF-Token` verified on `POST` in local spike | HTTPS `Secure` / `__Host-` cookie candidate enforcement, real browser OAuth/PKCE redirect `SameSite` behavior, Cloud Run `X-Forwarded-Host` normalization |
| **Session Expiry & Logout / Revocation** | Expired synthetic JWT rejected; synthetic BFF session deletion + optional synthetic denylist tested; **also proved** that without a backend denylist, an already-issued JWT remains valid until `exp` | Real access/refresh expiration, concurrent-tab refresh rotation, real Supabase `signOut({ scope: 'global' | 'local' })` & chosen short-TTL vs denylist policy |

See [HOSTED_VERIFICATION_RUNBOOK.md](./HOSTED_VERIFICATION_RUNBOOK.md) for the full OD-006 / OD-010 verification matrices, free-tier/cost guardrails, and hosted execution procedures.

---

## 6. Proposals for Orchestrator Review (Not Finalized Here)

### OD-006 (DB Isolation, Roles, Pooling & Data API Boundary) — Verification Candidate
1. **Separate 3 DB roles**: CI-only `migrator` (owner, Session `:5432` or Direct connection, never in Cloud Run runtime), `api_app` (`NOBYPASSRLS`, read-only self active memberships + tenant CRUD), and `worker_app` (`NOBYPASSRLS`, no identity/membership access, narrow domain privileges).
2. **Mandatory `ENABLE` + `FORCE ROW LEVEL SECURITY`** with fail-closed `auth_ctx.current_workspace_id()` on every tenant-bearing table, bound strictly via transaction-local `set_config(..., true)`.
3. **Mandatory Composite FK `(workspace_id, parent_id) -> parent(workspace_id, id)`** on all parent-child tenant tables (eliminates the PostgreSQL owner-RI cross-workspace vulnerability and UUID existence oracle).
4. **Pooling**: Use Supabase shared Transaction Pooler (`:6543`) with **`psycopg` `prepare_threshold=None`** (do **not** use `prepare_threshold=0`, which forces immediate query preparation in psycopg3) for API/Worker runtime, and Session/Direct mode for CI migrations only — **pending hosted verification in `HOSTED_VERIFICATION_RUNBOOK.md`**.
5. **Data API Bypass Prevention**: Verify in hosted staging that browsers cannot bypass backend authorization via Supabase Data API (PostgREST `/rest/v1/*`) using `anon` or `authenticated` credentials.

### OD-010 (Browser Session, Next.js Server Proxy & CSRF Boundary) — Verification Candidate
1. **Next.js Server Proxy + `HttpOnly`/`Secure` Cookie (Candidate Under Verification)**: Browser holds only an `HttpOnly`/`Secure` (`SameSite=Lax`, prod `__Host-` candidate) session cookie; raw Supabase access/refresh tokens stay server-side and are forwarded as `Authorization: Bearer` only on the Server → Backend API hop.
2. **Authoritative Backend Authorization**: Backend API verifies Supabase JWKS (`iss`, `aud`, `exp`, `alg`) and resolves current active `workspace_memberships` in DB on every request; never trusts client-supplied `workspace_id` or JWT workspace claims alone; exact error status/body format awaits separate API contract approval.
3. **CSRF Defense**: Enforce `Origin` allowlist + synchronizer `X-CSRF-Token` (or Fetch Metadata + Origin) on state-changing BFF routes.
4. **Session Expiry & Logout Policy**: Validate real access/refresh expiration, concurrent-tab refresh rotation, and direct-API reuse of pre-logout unexpired access JWTs (short TTL vs server-side revocation) in hosted staging before locking OD-010.
5. **Concern Separation**: Keep Supabase Auth service login sessions strictly separate from GitHub repository ingestion grants (OD-011) and LLM plan/billing entitlements.
