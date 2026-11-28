# `career_memory_api.hosted_runtime` Contract (Track 2 Wave 2A)

## Purpose
Production Cloud Run API (`cm-api`), Private IAM Worker (`cm-worker`), Supabase Supavisor Transaction Pooler (`:6543`), pinned JWKS/GoTrue live-session verification, `cm_migrator` (`:5432`) migration runner, and read-only `OD-015` cloud pre-flight gate.

## Non-Negotiable Invariants
1. **Bare App Fail-Closed**: `career_memory_api.app:create_app()` and `career_memory_api.main:app` remain 100% untouched and fail-closed (`UnconfiguredWorkspaceAccessProvider`).
2. **Supavisor `:6543` Compatibility (`OD-006`)**:
   - `psycopg_pool.ConnectionPool` ALWAYS sets `prepare_threshold=None` (NEVER `0`) and `autocommit=False`.
   - `1 <= max_size <= 4` (worker `max_size <= 2`).
   - Refuses privileged roles (`postgres`, `supabase_admin`, `service_role`, `cm_migrator`, etc.) and any role with `rolsuper=true`, `rolbypassrls=true`, or `> 0` owned `public` tables.
   - Zero startup `options=-c ...` on `:6543`; timeouts and RLS context use transaction-local `set_config(..., true)`.
3. **Zero DB Hold During Remote Auth I/O (`OD-006` / `T-007D`)**:
   - `HostedPostgresWorkspaceAccessProvider.verify_identity()` executes JWKS and GoTrue `GET /auth/v1/user` verification BEFORE checking out any pooled DB connection (`connections_in_use == 0`, `open_tx == 0`).
4. **Edge & Origin Guard (`T-507` / `OD-010`)**:
   - `cm-api` blocks direct unauthenticated `*.run.app` calls (`403 ORIGIN_AUTH_REQUIRED`), accepts signed `X-CM-Origin-Auth` from `cm-web` BFF / Cloudflare, and unconditionally denies `/internal/worker/**` (`403 PUBLIC_WORKER_ROUTE_FORBIDDEN`).
   - `GET /v1/session` is mounted ONLY on `create_hosted_api_app()`, executes `SELECT`-only under `cm_api_app`, returns `Cache-Control: private, no-store`, and NEVER includes `access_token` or `refresh_token`.
5. **Private Worker & Post-Commit Wake (`OD-012`)**:
   - `cm-worker` connects ONLY as `cm_worker_app` (`NOBYPASSRLS`), denies all `/v1/**` routes, and requires verified Google OIDC / HMAC worker identity with zero edge/browser headers on `/internal/worker/wake` and `/internal/worker/recover`.
   - Post-commit `HttpCloudRunWakeNotifier` uses a bounded timeout (`2.0s`), zero retries, and never fails an already-committed API `202`.
6. **Migration Isolation & Zero Catalog Drift (`T-106B`)**:
   - `migration_runner.py` runs strictly on `:5432` Session/Direct mode under a PostgreSQL advisory lock, stores checksums in `auth_ctx.schema_migration_ledger` (outside `public` so `public` stays at 46 tables with 0 catalog drift), revokes `anon`/`authenticated` `public` grants (`OD006-5`), and enforces a post-migration security audit.
7. **Zero-Mutation `OD-015` Pre-Flight**:
   - `preflight_od015.py` is strictly read-only (`mutations_performed == 0`) and emits immediate `NO_GO` if `containerscanning.googleapis.com` is enabled without mutating project security settings.
