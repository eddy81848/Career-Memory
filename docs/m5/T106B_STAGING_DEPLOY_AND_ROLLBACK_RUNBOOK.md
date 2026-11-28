# T-106B Staging Deploy, Migration & Rollback Runbook

> [!IMPORTANT]
> **Gate & Secret Hygiene Boundary (`T-506` / `OD-013` / `OD-015` / `INF-09`)**
> - `PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE` remains `NO_GO` (`HELD_PENDING_OD013_OD015`) until live GCP + Supabase Free resources are provisioned and `OD-015` pre-flight returns `GO`.
> - Subscription LLM (`OD-002` / `OD-014`) remains strictly **HARD-OFF** (`LlmRuntimeMode.OFF_MANUAL_ONLY`, `default_adapter=None`) on hosted Cloud Run.
> - **Zero Secrets in Git**: NEVER commit real credentials, tokens, DSNs with passwords, HMAC/cookie secrets, project-specific secret values, private keys, or `.env*` files into Git, Dockerfiles, JSON manifests, or docs.
> - **Keyless WIF + Encrypted Secrets Only**: `.github/workflows/staging-deploy.yml` is bound to GitHub Environment `staging`, authenticates to GCP exclusively via OIDC Workload Identity Federation (zero static service account keys), reads deployment secrets from GitHub Environment `staging` Encrypted Secrets, and mounts runtime credentials into Cloud Run strictly via `--set-secrets` referencing GCP Secret Manager names. `.github/workflows/pr-conformance-local.yml` stays 100% secret-free per `INF-09`.

---

## 1. Architecture, Role Separation & Secret Storage Matrix

| Component | Runtime Role / Identity | DB Connection Path | Scale / Cost Caps (`OD-015`) |
|---|---|---|---|
| `cm-api` (Cloud Run) | `CAREER_MEMORY_SERVICE_ROLE=api`, DB role `cm_api_app` (`NOBYPASSRLS`) | Supabase Supavisor Transaction Pooler `:6543` (`prepare_threshold=None`, `max_size <= 2`, `sslmode=verify-full` / `require`) | `us-central1`, `min-instances=0`, `max-instances=2`, `cpu=1`, `memory=512Mi` |
| `cm-worker` (Cloud Run, `--no-allow-unauthenticated`) | `CAREER_MEMORY_SERVICE_ROLE=worker`, DB role `cm_worker_app` (`NOBYPASSRLS`) | Supabase Supavisor Transaction Pooler `:6543` (`prepare_threshold=None`, `max_size <= 2`, `sslmode=verify-full` / `require`) | `us-central1`, `min-instances=0`, `max-instances=1`, `cpu=1`, `memory=512Mi` |
| `cm-web` (Cloud Run BFF) | Next.js BFF signing `X-CM-Origin-Auth` HMAC | No direct DB access (proxies authenticated `/v1/*` & `/v1/session` to `cm-api`) | `us-central1`, `min-instances=0`, `max-instances=2`, `cpu=1`, `memory=512Mi` |
| CI / Operator Migrator | `MIGRATION_DATABASE_URL` (`cm_migrator` only) | Supabase Session Pooler `:5432` or Direct `:5432` (`:6543` refused) | Advisory lock + `lock_timeout=5s`; ledger in `auth_ctx.schema_migration_ledger` |

### GitHub Environment `staging` & GCP Secret Manager Inventory (`<= 4` Active Versions)

| Storage Plane | Key / Secret Name | Purpose | Notes |
|---|---|---|---|
| **GitHub Environment `staging` Encrypted Secrets** | `STAGING_MIGRATION_DATABASE_URL` | `cm_migrator` `:5432` Session/Direct DSN for CI migrations | Never mounted on Cloud Run runtime services (`OD006-6`) |
| **GitHub Environment `staging` Encrypted Secrets** | `STAGING_SUPABASE_ANON_KEY` | Supabase `apikey` header for GoTrue `/auth/v1/user` & BFF | Stored encrypted in GitHub Environment `staging` |
| **GitHub Environment `staging` Variables (non-secret)** | `GCP_PROJECT_ID`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SERVICE_ACCOUNT`, `GCP_API_RUNTIME_SA`, `GCP_WORKER_RUNTIME_SA`, `GCP_WEB_RUNTIME_SA`, `STAGING_SUPABASE_URL`, `STAGING_SUPABASE_JWT_ISSUER`, `STAGING_SUPABASE_JWKS_URL`, `STAGING_SUPABASE_AUTH_URL`, `STAGING_ALLOWED_WEB_ORIGINS`, `STAGING_WORKER_WORKSPACE_IDS` | Keyless WIF + public endpoints & allowlists | Zero credentials or private keys |
| **GCP Secret Manager (`1/4`)** | `cm-api-database-url` | Mounted on `cm-api` via `--set-secrets API_DATABASE_URL=...` | `cm_api_app` (`:6543`, `NOBYPASSRLS`) |
| **GCP Secret Manager (`2/4`)** | `cm-worker-database-url` | Mounted on `cm-worker` via `--set-secrets WORKER_DATABASE_URL=...` | `cm_worker_app` (`:6543`, `NOBYPASSRLS`) |
| **GCP Secret Manager (`3/4`)** | `cm-origin-hmac-secret` | Mounted on `cm-api` & `cm-web` via `--set-secrets` | Shared `X-CM-Origin-Auth` HMAC proof |
| **GCP Secret Manager (`4/4`)** | `cm-bff-session-secret` | Mounted on `cm-web` via `--set-secrets` | HttpOnly/Secure BFF cookie encryption key |

> Total active Secret Manager versions = **4** (`<= 4` `OD-015` cap, preserving 2 Free Tier rotation slots under the 6-version limit).

---

## 2. Step-by-Step Staging Deployment Procedure

### Step 1: Read-Only `OD-015` Cloud Pre-Flight Check (`0` Mutations)
Run before building, pushing, or deploying any container:

```bash
cd apps/api
uv run python ../../scripts/check_od015_cloud_preflight.py \
  --project-id "$GCP_PROJECT_ID" \
  --region us-central1 \
  --json
```

**Pass Criteria (`GO`, exit code `0`):**
1. Region is Tier 1 Free-Tier (`us-central1`, `us-east1`, or `us-west1`).
2. Paid Container Scanning (`containerscanning.googleapis.com`) is NOT enabled (**if enabled, STOP immediately as `NO_GO`; never disable shared project security APIs**).
3. Artifact Registry total storage `<= 250 MB` (`>= 250 MB` teardown headroom under `500 MB` Free Tier).
4. Secret Manager enabled secret versions `<= 4` (under `6` Free Tier versions).
5. Paid Serverless VPC Connectors `== 0`.
6. Cloud Run service caps (`min-instances=0`, `max-instances<=2`, `cpu<=1`, `memory<=512Mi`) and conservative monthly worst-case estimate `< ₩20,000`.

### Step 2: Expand-Only Migration (`:5432`) & Post-Migration Security Audit
Never run DDL through `:6543` or under `cm_api_app` / `cm_worker_app`. Source `MIGRATION_DATABASE_URL` strictly from GitHub Environment `staging` Encrypted Secrets (`secrets.STAGING_MIGRATION_DATABASE_URL`) in CI or an operator secret manager shell export (never committed or logged):

```bash
cd apps/api
# Export MIGRATION_DATABASE_URL from your secret manager before running:
uv run python ../../scripts/run_staging_migrations.py
```

The runner:
1. Acquires session-level PostgreSQL advisory lock (`MIGRATION_ADVISORY_LOCK_KEY`) with `lock_timeout = '5s'`.
2. Applies `0010..0080` idempotently and records SHA-256 checksums in `auth_ctx.schema_migration_ledger` (keeping `public` at exact 46 tables with `0` drift against `contracts/conformance/expected_db_catalog_snapshot.json`).
3. Revokes all `public` table/sequence/function grants from Supabase `anon` and `authenticated` roles (`OD006-5`).
4. Runs the mandatory post-migration security audit (`46/46` `ENABLE + FORCE ROW LEVEL SECURITY`, `cm_api_app` / `cm_worker_app` `rolsuper=false AND rolbypassrls=false` owning `0` tables, `0` `anon`/`authenticated` grants).

### Step 3: Enroll Closed-Pilot Owner (`cm_migrator` Only)
Because `cm_api_app` is strictly `SELECT`-only on `public.users`, `public.workspaces`, and `public.workspace_memberships`, enroll pilot users explicitly via `cm_migrator` (with `MIGRATION_DATABASE_URL` exported in memory only):

```bash
cd apps/api
uv run python ../../scripts/run_staging_migrations.py \
  --enroll-pilot-user \
  --auth-issuer "$STAGING_SUPABASE_JWT_ISSUER" \
  --auth-subject "$PILOT_SUPABASE_USER_ID" \
  --workspace-name "Closed Pilot Workspace" \
  --email "$PILOT_EMAIL" \
  --display-name "Pilot Owner" \
  --github-login "$PILOT_GITHUB_LOGIN"
```

### Step 4: Build & Push `linux/amd64` Image + Record `@sha256:` Digest
```bash
docker build --platform linux/amd64 \
  -t "us-central1-docker.pkg.dev/${GCP_PROJECT_ID}/career-memory/cm-api:${GIT_SHA}" \
  -f apps/api/Dockerfile .
docker push "us-central1-docker.pkg.dev/${GCP_PROJECT_ID}/career-memory/cm-api:${GIT_SHA}"
# Capture immutable digest:
API_IMAGE_DIGEST="$(docker inspect --format='{{index .RepoDigests 0}}' "us-central1-docker.pkg.dev/${GCP_PROJECT_ID}/career-memory/cm-api:${GIT_SHA}")"
```

### Step 5: Trigger Manual WIF Deploy (`.github/workflows/staging-deploy.yml`)
Trigger `workflow_dispatch` against GitHub Environment `staging` with `action=deploy`, immutable `@sha256:<64-hex>` `api_image_digest` & `web_image_digest`, and known-good `previous_*` rollback targets:
1. Validates `contracts/import-v1/openapi.yaml` blob SHA (`07594d75425744d6149250a9f7ff95ac24659f80`) and `ReleaseManifestV1`.
2. Authenticates via keyless OIDC WIF (`vars.GCP_WIF_PROVIDER` + `vars.GCP_DEPLOY_SERVICE_ACCOUNT`).
3. Runs `check_od015_cloud_preflight.py` (`0` mutations).
4. Runs `run_staging_migrations.py` using `secrets.STAGING_MIGRATION_DATABASE_URL`.
5. Deploys `cm-worker` (`--no-allow-unauthenticated`, `--set-secrets`), then `cm-api` (`--no-traffic --tag candidate`, `--set-secrets`), then `cm-web` (`--no-traffic --tag candidate`, `--set-secrets`).
6. Runs `run_t106b_release_verify.py --smoke-url <candidate-api-url> --worker-url <worker-url>` verifying:
   - `GET /healthz` -> `200`
   - Direct `GET /v1/session` without `X-CM-Origin-Auth` -> `403 ORIGIN_AUTH_REQUIRED` + `Cache-Control: private, no-store`
   - `POST /internal/worker/wake` on `cm-api` -> `403 PUBLIC_WORKER_ROUTE_FORBIDDEN`
   - Unauthenticated `POST /internal/worker/wake` on `cm-worker` -> `401/403`
7. Shifts `cm-api` and `cm-web` traffic to `100%` (`--to-latest`) ONLY after smoke verification passes.

---

## 3. Instant Rollback Procedure

### Option A: GitHub Actions Manual Rollback
Dispatch `.github/workflows/staging-deploy.yml` with `action=rollback` and the recorded `previous_api_revision`, `previous_web_revision`, and `previous_worker_digest` (`@sha256:` pinned).

### Option B: Direct Operator Rollback Commands
Generate exact commands from the saved `ReleaseManifestV1` JSON:

```bash
cd apps/api
uv run python ../../scripts/run_t106b_release_verify.py \
  --manifest /path/to/release-manifest.v1.json \
  --project-id "$GCP_PROJECT_ID" \
  --print-rollback
```

This emits:
1. Instant traffic shift on `cm-api`: `gcloud run services update-traffic cm-api --to-revisions=<previous_api_revision>=100`
2. Instant traffic shift on `cm-web`: `gcloud run services update-traffic cm-web --to-revisions=<previous_web_revision>=100`
3. Worker digest restore on `cm-worker`: `gcloud run deploy cm-worker --image=<previous_worker_digest> --no-allow-unauthenticated ...`

> [!NOTE]
> **Expand-Only Migrations**: Migrations `0010..0080` are additive/expand-only, so rolling back application revisions does NOT require destructive down-migrations.

---

## 4. `T-505` Encrypted Backup & Restore Drill (Supabase Free Non-PITR)

Because Supabase Free Tier does NOT provide Point-in-Time Recovery (PITR), operators must run the encrypted `pg_dump` / restore verification drill before promoting closed-pilot traffic:
1. Follow `docs/m5/T505_BACKUP_RESTORE_DRILL.md` using `cm_migrator` `:5432` Session/Direct connection (never `:6543`).
2. Verify restored database catalog matches `contracts/conformance/expected_db_catalog_snapshot.json` (`46` tables, `46/46` `FORCE RLS`, `0` resurrected rows behind active `deletion_jobs` fences).
3. Run `scripts/run_staging_migrations.py --audit-only` immediately after any restore to verify `anon`/`authenticated` remain revoked and runtime roles own `0` tables.
