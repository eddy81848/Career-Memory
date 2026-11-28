# Track 2 Hosted Staging Verification & WIF Release/Rollback Report (`T-007A..F` · `T-106B`)

- **Tasks Covered**: `T-007A`, `T-007B`, `T-007C`, `T-007D`, `T-007E`, `T-007F`, `T-106B`
- **Open Decisions Validated & Accepted**: `OD-005`, `OD-006`, `OD-010`, `OD-012`, `OD-013`, `OD-015`
- **Redacted Evidence Bundle**: `contracts/hosted-staging/t007f_t106b_staging_evidence.v1.json`
- **WIF Workflow**: `.github/workflows/staging-deploy.yml` (Deploy Run `46143882856` `SUCCESS`, Rollback Run `46144193821` `SUCCESS`)
- **Immutable M0 OpenAPI Pin**: `contracts/import-v1/openapi.yaml` (`07594d75425744d6149250a9f7ff95ac24659f80`)

---

## 1. Live Hosted Topology & Zero-Secrets Architecture (`us-central1` + Supabase Free PG17)

| Plane / Service | Live Endpoint / Target | Runtime Identity & Guardrails | Scale / Cost Caps (`OD-015`) |
|---|---|---|---|
| **`cm-web` (Next.js BFF)** | `https://cm-web-oaxjehpcqq-uc.a.run.app` | `cm-web-sa`, PKCE S256 + `__Host-cm_session` AES-256-GCM `HttpOnly; Secure; SameSite=Lax`, signs `X-CM-Origin-Auth` HMAC | `min-instances=0`, `max-instances=2`, `1 vCPU`, `512 MiB` |
| **`cm-api` (FastAPI Public API)** | `https://cm-api-oaxjehpcqq-uc.a.run.app` | `cm-api-sa`, `cm_api_app` (`:6543` Supavisor, `NOBYPASSRLS`), `T-507` `edge_guard` (`403 ORIGIN_AUTH_REQUIRED` on direct bypass, `403` on `/internal/worker/**`) | `min-instances=0`, `max-instances=2`, `1 vCPU`, `512 MiB` |
| **`cm-worker` (Private Worker)** | `https://cm-worker-oaxjehpcqq-uc.a.run.app` | `cm-worker-sa`, `--no-allow-unauthenticated` (`roles/run.invoker` granted strictly to `cm-api-sa` & `cm-deploy-sa`) + app-layer Google OIDC verifier, `cm_worker_app` (`:6543`, `NOBYPASSRLS`) | `min-instances=0`, `max-instances=1`, `1 vCPU`, `512 MiB`, `concurrency=4`, `timeout=60s` |
| **Supabase PostgreSQL 17.12** | `ap-northeast-1` (`mkzqvxcuqhrmfnkgjpfh`) | `0010..0080` applied via `:5432` Session Pooler (`cm_migrator`); `46/46` `ENABLE + FORCE RLS`; `anon`/`authenticated` `public` grants revoked (`401` on PostgREST bypass) | Free Tier (`paid_pitr_assumed=false`; encrypted `pg_dump` drill in `T505_BACKUP_RESTORE_DRILL_REPORT.md`) |

### Secret Hygiene Verification (`INF-09`)
- **Zero credentials in Git / Docker / `NEXT_PUBLIC_*`**: Verified `0` `.env*` files, `0` passwords/tokens/DSNs in Git, and `NEXT_PUBLIC_API_MODE=hosted` as the sole public build arg.
- **Keyless OIDC Workload Identity Federation**: GitHub Environment `staging` authenticates to `cm-deploy-sa` via `github-actions-pool/github-oidc` (`0` static `GCP_SA_KEY`).
- **4/4 Secret Manager Versions (`<= 4` `OD-015` Cap)**: `cm-api-database-url`, `cm-worker-database-url`, `cm-origin-hmac-secret`, and `cm-bff-session-secret` mounted strictly via `--set-secrets` with per-secret least-privilege IAM bindings.

---

## 2. Live Verification Results (`T-007A..F`, `T-106B`, `OD-015`)

### 2.1 Read-Only `OD-015` Cloud Pre-Flight (`GO`)
`scripts/check_od015_cloud_preflight.py --project-id career-memory-516011 --region us-central1 --json`:
- `OD015_REGION_TIER1_FREE_TIER`: **PASS** (`us-central1`)
- `OD015_PAID_CONTAINER_SCANNING_DISABLED`: **PASS** (`containerscanning.googleapis.com` disabled)
- `OD015_ARTIFACT_REGISTRY_HEADROOM`: **PASS** (`214.99 MB <= 250.0 MB` under `500 MB` Free Tier)
- `OD015_SECRET_MANAGER_VERSION_HEADROOM`: **PASS** (`4 <= 4` enabled versions under `6` Free Tier limit)
- `OD015_ZERO_SERVERLESS_VPC_CONNECTORS`: **PASS** (`0` paid VPC connectors)
- `OD015_CLOUD_RUN_CAPS_AND_KRW_BUDGET`: **PASS** (`cap_violations=[]`, worst-case monthly estimate `₩0 < ₩20,000`)

### 2.2 WIF Candidate Deploy (`Run 46143882856`) & Instant Rollback Drill (`Run 46144193821`)
Both manual WIF runs against GitHub Environment `staging` passed **11/11** live security & health assertions:
1. `api_health_200`: `200 OK` (`Cache-Control: no-store`)
2. `api_direct_origin_auth_blocked_403`: `403 ORIGIN_AUTH_REQUIRED` (`Cache-Control: private, no-store`)
3. `api_internal_worker_route_blocked_403`: `403 PUBLIC_WORKER_ROUTE_FORBIDDEN`
4. `worker_unauthenticated_blocked_401_403`: `403` at Cloud Run IAM boundary
5. `worker_oidc_authorized_recover_200`: `200 OK` with Google-signed OIDC token (`cm-deploy-sa`) against `/internal/worker/recover` (exercising live `:6543` Supavisor + `cm_worker_app` RLS transaction)
6. `web_health_200`: `200 OK` (`Cache-Control: no-store`)
7. `web_auth_session_unauthenticated_200_no_store`: `200 OK` (`{"authenticated":false}`, `Cache-Control: private, no-store`)
8. `web_pkce_login_redirect_and_secure_httponly_cookie`: `302` redirect to Supabase GoTrue PKCE (`code_challenge=...`) + `__Host-` `HttpOnly; Secure` state cookie
9. `web_open_redirect_rejected_400`: `400` on `?next=//evil.example`
10. `web_bff_cross_origin_csrf_blocked_403`: `403` on cross-origin `POST /bff/v1/sync-runs`
11. `supabase_postgrest_domain_table_bypass_blocked`: `401` on direct PostgREST `GET /rest/v1/work_events` with `anon` key

### 2.3 Raw Cold-Start & Warm Latency Samples (`T-007F`)
Per `T-007F` small-sample measurement rules, raw observed latencies (milliseconds) are recorded without extrapolated p99 claims:
- **`cm-worker` OIDC `/internal/worker/recover` (`200`, includes container cold start + `:6543` TLS pool checkout)**: cold start `4605.14 ms`; warm calls `230.25 ms`, `255.02 ms`
- **`cm-api` `/api/health` (`200`)**: `251.25 ms`, `343.58 ms`, `348.70 ms`
- **`cm-api` direct `/v1/session` origin block (`403`)**: `75.10 ms`, `79.86 ms`, `84.24 ms`, `262.64 ms`
- **`cm-web` `/api/health` (`200`)**: `269.11 ms`, `489.51 ms`, `561.18 ms`
- **`cm-worker` unauthenticated IAM block (`403`)**: `148.16 ms`, `189.64 ms`, `194.56 ms`, `393.72 ms`

---

## 3. Honest Posture Notes (`OD-013`, `T-003` / `R-002`, `Track 3`)

- **`OD-013` Zero-Domain Scale-to-Zero Ingress**: Per explicit owner decision to avoid paid custom domain spend (`₩0`), `cm-web` and `cm-api` serve over HTTPS `*.run.app` with `min-instances=0` while `cm-api` enforces cryptographic `X-CM-Origin-Auth` HMAC proof (`403 ORIGIN_AUTH_REQUIRED` on any direct `*.run.app` call). If a custom Cloudflare domain is attached later, `CAREER_MEMORY_ALLOW_RUN_APP_ORIGIN_PROOF` can be flipped to `false` + Cloudflare Origin mTLS enabled with zero code changes.
- **`T-003` (`R-002`) Closed-Pilot Human Review Telemetry**: Remains open until real human closed-pilot review sessions are conducted; synthetic/smoke latencies are never substituted for human review-time telemetry.
- **Track 3 (`LIVE_SUBSCRIPTION_AI_PILOT_GATE`)**: Remains strictly **`NO-GO (HELD_PENDING_OD002_OD014)`** (`LlmRuntimeMode.OFF_MANUAL_ONLY`, `default_adapter=None`) pending Track 3 decisions (`T-006`, `OD-002`, `OD-014`).
