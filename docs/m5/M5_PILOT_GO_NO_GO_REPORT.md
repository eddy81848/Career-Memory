# M5 Closed Pilot & 3-Tier Go/No-Go Evaluation Report (`T-506`)

- **Task / Issue**: `T-506` / Issue `#33`
- **Evaluator Package**: `apps/api/src/career_memory_api/pilot_gate/`
- **Verification CLI**: `scripts/run_t506_pilot_gate.py --verify`
- **Release Gate Contract**: `contracts/pilot-gate/t506_release_gate.v1.json`
- **External Network Calls / Spend**: `0` calls / `₩0`
- **Immutable M0 OpenAPI Pin**: `contracts/import-v1/openapi.yaml` SHA `07594d75425744d6149250a9f7ff95ac24659f80`

---

## 1. Executive Summary — Strict 3-Tier Release Gate Decision (`ADR-018`, `ADR-019`, `ADR-020`)

### 한국어 요약
본 평가는 M0–M5, Track 2 호스티드 스테이징, Track 3 구독 LLM 정책 실사의 9개 커밋된 증거 번들(`B-004`, `T-306A`, `T-407A`, `T-408/INF-08`, `T-507/INF-07`, `T-505/INF-10`, `T-504A`, `T-007F/T-106B`, `T-006`)과 실제 PostgreSQL 18 + Next.js FE↔BE 및 GCP Cloud Run (`us-central1`) + Supabase PG17 라이브 검증 결과를 프로그래밍 방식으로 검증하여 **3단계 릴리스 게이트**를 정직하게 분리 판정합니다.

1. **`LOCAL_DETERMINISTIC_MVP_GATE` — `GO (PASSED)`**:
   - `GitHub OAuth/Allowlist → Initial Import → Event Review → Identity/Fact/Claim Review → Plan C Ephemeral Refetch → Zero-Quote Export → PG18 FTS Search → OD-003 Source Deletion → Quota/Circuit-Breaker → Encrypted PG18 Backup/Restore` 전 여정이 실제 PostgreSQL 18 (`0010..0080`, 46개 테이블 `FORCE RLS` + `NOBYPASSRLS`) 및 Next.js UI(`/`, `/event-review`, `/claim-review`, `/search`)에서 **외부 호출 0회, `₩0` 비용, Plan C 원문·시크릿 유출 0건**으로 완전히 검증되었습니다.
2. **`PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE` — `GO (PASSED)`**:
   - `OD-015` Free-Tier 사전 점검(`GO`, 월 최악 추정 `₩0 < ₩20,000`, Secret Manager `4/4` 버전, Artifact Registry `214.99 MB <= 250 MB`, VPC 커넥터 `0`), Supabase PostgreSQL 17.12(`0010..0080` 적용, `46/46` `FORCE RLS`, `:6543` Supavisor Transaction Pooler `prepare_threshold=None`, PostgREST 우회 `401` 차단), Cloud Run `us-central1` Scale-to-Zero(`cm-web` PKCE + `__Host-` AES-256-GCM HttpOnly/Secure 쿠키, `cm-api` `X-CM-Origin-Auth` HMAC 증명으로 직접 `*.run.app` 우회 `403` 차단, `cm-worker` `--no-allow-unauthenticated` + Google OIDC 전용), 그리고 Keyless WIF 기반 배포(`Run 46143882856`, `11/11` 통과) 및 즉시 롤백 드릴(`Run 46144193821`, `11/11` 통과)이 완료되었습니다 (`docs/m5/T007F_T106B_HOSTED_STAGING_REPORT.md` 참조; 유료 커스텀 도메인 미구매 `*.run.app` + HMAC 운영 모드는 `OD-013` 결정에 따라 투명하게 기록됨).
3. **`LIVE_SUBSCRIPTION_AI_PILOT_GATE` — `NO-GO (HELD_PENDING_OD002_OD014)`**:
   - 합성 픽스처 기반 계약·안전 게이트(`T-407A` 16 시나리오 + 27 refetch 케이스, `T-408/INF-08` 17 정책 케이스)는 유출 0건으로 모두 통과했습니다. 그러나 `M4`/`M5` 종료 기준 및 `ADR-018`/`ADR-020`에 따라, 외부 구독 LLM 전송이 `OFF`인 상태에서 합성 테스트 및 비-AI 호스티드 스테이징 통과만으로는 라이브 AI 모델 게이트를 절대 닫을 수 없으며, `OD-002`·`OD-014` 승인 및 실제 모델 의미 평가가 완료될 때까지 **`NO-GO (HELD)`**를 유지합니다.

### English Summary Table

| Release Gate Tier | Decision | Status Code | Blocking Open Decisions | Rationale |
|---|---|---|---|---|
| **`LOCAL_DETERMINISTIC_MVP_GATE`** | **`GO`** | `PASSED` | None | All 7 local M0–M5 evidence bundles, 9 invariant groups, and live PG18 + Next.js FE↔BE tests passed with `0` leaks at `₩0`. |
| **`PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE`** | **`GO`** | `PASSED` | None | `T-007F/T-106B` verified live `OD-015` Free-Tier `GO` (`₩0`), Supabase PG17 `46/46` `FORCE RLS` + `:6543` Supavisor, Cloud Run `cm-web`/`cm-api`/`cm-worker` (`min-instances=0`, `X-CM-Origin-Auth` HMAC, private IAM/OIDC worker), and keyless WIF deploy (`46143882856`) + rollback (`46144193821`) `11/11` smoke checks. |
| **`LIVE_SUBSCRIPTION_AI_PILOT_GATE`** | **`NO-GO`** | `HELD_PENDING_OD002_OD014` | `OD-002`, `OD-014` | Synthetic contract/safety gates (`T-407A`, `T-408`/`INF-08`) passed with `0` leaks, but synthetic passes NEVER close the live model gate while external subscription LLM transmission is `OFF`. |

---

## 2. Programmatically Verified Evidence Bundles (9/9 Verified)

| Bundle | Contract Path | Verified Scope | Key Metrics |
|---|---|---|---|
| **`B-004`** | `contracts/fault-harness/b004_evidence_bundle.v1.json` | Initial Backfill fault/checkpoint/replay harness on PG18 | `16/16` scenarios, `0` diffs, `0` canary leaks |
| **`T-306A`** | `contracts/event-eval/t306a_eval_bundle.v1.json` | Event candidate builder, curation mutations & composite collapse | `21/21` cases (`16/16` bundles), `0` oracle diffs, `0` Plan C leaks |
| **`T-407A`** | `contracts/claim-eval/t407a_eval_bundle.v1.json` | Synthetic Fact/Claim groundedness & `REF-01..08` runtime matrix | `16/16` scenarios, `29/29` claims, `27/27` refetch cases, `0` live LLM calls (`live_pilot_gate_eligible=false`) |
| **`T-408/INF-08`** | `contracts/llm-gate/inf08_evidence.v1.json` | Subscription LLM hard-OFF policy gate, PKCE/OIDC, unlink fence | `17/17` cases, `0` leaks, `0` DB token tables, `OFF_NOT_PASSED_PENDING_OD002_OD014` |
| **`T-507/INF-07`** | `contracts/edge-guard/t507_route_trust_matrix.v1.json` | Edge origin auth, private worker isolation, cache isolation, pause & CI audit | `14/14` cases, CI trust audit `PASS`, `HELD_NO_GO_PENDING_OD013_OD015` (pre-cloud baseline) |
| **`T-505/INF-10`** | `contracts/backup-restore/t505_restore_evidence.v1.json` | AES-256-GCM encrypted PG18 `pg_dump`/`pg_restore` & fence drill | `46` tables FORCE RLS, RTO `330ms <= 30,000ms`, `0` resurrected rows, `paid_pitr_assumed=false` |
| **`T-504A`** | `contracts/fault-matrix/t504a_evidence_bundle.v1.json` | Cross-subsystem fault & `threading.Barrier` race matrix (`XFLT-01..12`) | `12/12` scenarios, `167/167` assertions, `4` barrier races, `0` sleep waits, `0` leaks |
| **`T-007F/T-106B`** | `contracts/hosted-staging/t007f_t106b_staging_evidence.v1.json` | Live Cloud Run (`us-central1`) + Supabase PG17 `:6543` + WIF deploy & rollback | `OD-015` `GO` (`₩0`), `46/46` `FORCE RLS`, WIF deploy `11/11` & rollback `11/11`, `0` static keys |
| **`T-006`** | `contracts/llm-gate/t006_policy_audit.v1.json` | Official 3-provider (`OpenAI`, `Anthropic`, `Google`) hosting, training & zero-overage audit | Hosted Cloud Run locked `OFF_MANUAL_ONLY`, `0` LLM token storage, `LIVE_SUBSCRIPTION_AI_PILOT_GATE` held `NO-GO` |

---

## 3. Product Quality, Review Cost, Groundedness & Operations Assessment

### 3.1 Review Cost & Usability (`R-002` — 검토 비용 및 사용성)
- **Deterministic Non-Destructive Review Flows**: Event Inbox (`/event-review`), Claim Review (`/claim-review`), and Memory Ops (`/search`) preserve versioned history (`ER-012`), deterministic optimistic concurrency (`409` conflict recovery without losing user edits), and clear badge separation between `SOURCE_VERIFIED`, `MIXED`, `USER_CONFIRMED`, and `UNVERIFIED`.
- **Synthetic Contract Latency vs Closed Pilot Telemetry Honesty**: `T-306A` synthetic curation p50 execution is `28.7 ms` (p95 `151.5 ms`), while `CLOSED_PILOT_USER_TELEMETRY.sample_count` is explicitly `0`. Real human review-time telemetry (`R-002`) is NOT fabricated from synthetic benchmarks and will be measured only when a human closed pilot runs.

### 3.2 Groundedness & Plan C Ephemeral Verification (`T-407A` / `ADR-013`)
- Every `SOURCE_VERIFIED` Claim requires `APPROVED` status, live current-operation Plan C refetch (`REF-07a/b/d`), active `DIRECT` `fact_support` -> `claim_atom_support` lineage, non-conflicted verified identity attribution (`ID-04`), and read-only server-computed `VerificationCalculator` (direct client writes to `verification` fail closed with `422 PROHIBITED_READONLY_VERIFICATION_WRITE`).
- Across all 46 PostgreSQL 18 tables, `content_units.text_content`, `content_units.content_ref`, and `fact_support.extracted_span` remain strictly `NULL`, and `0` canary/XSS/secret strings appear in DB tables, logs, exports, or evidence bundles.
- **Population Accuracy Disclaimer**: Per `T-407A` and `ADR-018`, the 16-scenario / 29-claim deterministic synthetic set validates contract, lineage, and safety invariants ONLY — it makes **zero** population accuracy claims (`<1%` hallucination rate cannot be claimed until live human-audited model runs complete).

### 3.3 Export Usefulness & Safety (`EXPORT-01..02`)
- Verified zero-quote `MARKDOWN` and `JSON` exports (`SOURCE_VERIFIED_ONLY` and `ALLOW_MIXED_AND_USER_CONFIRMED`) include structured claim text, non-raw locator citations (`SpanLocatorV1`), and verification timestamps while excluding raw source quotes (`contains_raw_quotes: false`).
- Build-time `ER-012` Fact edits (`XFLT-03`), concurrent deletion fences (`XFLT-04`), and download-time provider SHA drift (`XFLT-03`, `XFLT-10`) all fail closed (`INVALIDATED` with `artifact_payload = NULL` / `409`).

### 3.4 Search, Deletion, Security, Quotas & Backup/Restore Verification
- **Search (`SEARCH-01..05`, `OD-008`)**: Real PG18 FTS index with authoritative pre-response recheck filters 100% of lagged `DRAFT`/`REJECTED`/`STALE`/fenced entries (`XFLT-06`), enforces version/scope-bound cursors (`400 INVALID_SEARCH_CURSOR`), rejects `project_id` (`422 PROHIBITED_PROJECT_ID_SCOPE`), and isolates tenants via 46-table `FORCE RLS`.
- **Deletion (`DELETE-01..07`, `OD-003`)**: Version-bound `preview_token`, zero-window fence, 5-component receipt idempotency (`XFLT-05`), 5-party `threading.Barrier` 4-worker zero-resurrection proof (`XFLT-04`), and live FE↔BE PG18 `deletion-preview -> deletion-confirm -> PARTIAL -> DELETED` verification preserving independent sibling repositories.
- **Security & Edge Guard (`SEC-01..02`, `INF-07`, `INF-09`)**: Hostile prompt-injection, XSS, binary/oversized payloads, SSRF/redirect targets (`REF-05`), direct `*.run.app` bypass, and edge-injected `/internal/worker/**` requests all fail closed (`XFLT-07`, `XFLT-09`); PR CI workflow passes `INF-09` minimal-permissions & zero-deploy-secret audit.
- **Quotas & Backup/Restore (`PERF-01`, `INF-03`, `INF-06`, `INF-10`, `REL-01`)**: Circuit breaker, ambiguous billable timeout no-auto-retry, and 95% cutoff (`paid_fallback_allowed=False`) block high-cost growth while guaranteeing `disconnect` and `OD-003` deletion rights (`XFLT-08`); AES-256-GCM encrypted `pg_dump`/`pg_restore` preserves Repo A lineage and Repo B deletion fences with `0` resurrected rows (`T-505`, `XFLT-11`).

---

## 4. Operator Checklist to Transition `HELD` Gates to `GO` (운영자 전환 체크리스트)

### A. Unlocking `PUBLIC_CLOUD_HOSTED_CLOSED_PILOT_GATE` (`OD-013` + `OD-015`)
1. **Billing Hard Cutoff (`OD-015`, `ADR-020`)**: Configure GCP Cloud Billing budget alerts + automated stop/pause webhook (or explicit human budget approval) and set Cloud Run `max-instances`, `concurrency`, CPU/memory, and request timeout caps.
2. **Platform Origin & Worker Lock (`INF-07`, `OD-013`)**: Enable Cloudflare Authenticated Origin Pulls (mTLS) / signed Transform Rule header, verify Cloud Run ingress/IAM blocks direct internet `*.run.app` traffic, and deploy `/internal/worker/**` on a private IAM-only service (`--no-allow-unauthenticated`).
3. **Supabase Free Production Boundary (`OD-013`, `INF-10`)**: Configure Supabase Auth redirect allowlists, verify `cm_api_app`/`cm_worker_app` `NOBYPASSRLS` roles and PgBouncer session/transaction pool limits, and schedule encrypted logical `pg_dump` backups to off-host encrypted storage (remembering Supabase Free does NOT include paid PITR).
4. **Live Smoke & Restore Drill**: Execute a live hosted smoke test (verify `403` on direct `*.run.app`, `private, no-store` on Cloudflare edge responses, and one isolated restore drill from the scheduled encrypted backup).

### B. Unlocking `LIVE_SUBSCRIPTION_AI_PILOT_GATE` (`OD-002` + `OD-014`)
1. **External Transmission Policy Sign-Off (`OD-002`)**: Obtain explicit owner sign-off on public-only unmasked ephemeral payload transmission to the subscription LLM provider and per-repository user consent copy.
2. **External Encrypted Token Vault (`OD-014`, `INF-08`)**: Provision and audit the external encrypted user-subscription OAuth token vault (zero tokens in PostgreSQL), verify `LOCAL_FENCE -> REMOTE_REVOKE -> LOCAL_SECRET_WIPE` unlink flow, and confirm zero overage-credit / paid-API fallback.
3. **Live Closed-Pilot Semantic Evaluation (`ADR-018`)**: Flip the 4 `HostedLlmApprovalConfig` flags ONLY in an isolated closed-pilot environment, run real human-audited groundedness & review-time (`R-002`) evaluation on real repositories, and record a separate live semantic evaluation bundle before general pilot enablement.
