# B-004 Initial Backfill Fault Harness Verification Report (`T-005A` → `T-005B` → `T-005C` + `T-005D` Evidence)

## 1. Executive Summary & Gate Boundary

- **Gate / Track**: `B-004` (`M2-B004-HARNESS` — `T-005`, `T-005A`, `T-005B`, `T-005C`, and redacted evidence bundle for independent `T-005D` review)
- **Immutable OpenAPI Contract (`contracts/import-v1/openapi.yaml`)**: `07594d75425744d6149250a9f7ff95ac24659f80` (verified unchanged)
- **Production Stack Exercised (`ADR-017` Compliance)**:
  - **Zero shadow/mock importer models.** Every scenario runs the real production M2 services:
    - `career_memory_api.initial_import.InitialBackfillOrchestrator`
    - `career_memory_api.initial_import.InitialBackfillJobWorker`
    - `career_memory_api.initial_import.DerivedUnitIngestionProcessor`
    - `career_memory_api.backfill_engine.PostgresBackfillPageCommitter`
    - `career_memory_api.sync_runs.PostgresSyncRunService` & `create_sync_runs_router`
    - `career_memory_api.jobs.PostgresJobRuntime`
    - `career_memory_api.github_connect.PostgresGitHubConnectService` & `create_github_connect_router`
    - `career_memory_api.github_provider.HttpGitHubImportClient` & `SafeGitHubHttpTransport`
    - `career_memory_api.resolver.RefetchResolverService` & `create_resolver_router` (`GET /v1/content-units/{id}/content`)
    - `career_memory_api.contracts_api.create_contracts_router` (`OD-003` `deletion-preview` / `deletion-confirm`)
  - **Real PostgreSQL 18**: Local disposable PostgreSQL 18 cluster initialized fresh with `db/migrations/0010` → `0020` → `0030` → `0040` → `0050` and `NOBYPASSRLS` runtime roles (`cm_api_app`, `cm_worker_app`).
- **T-005C Prohibition Compliance**:
  - Expected logical snapshots (`contracts/fault-harness/expected_snapshots/S01.json` ~ `S16.json`) are strictly **read-only** at runtime. Neither `career_memory_api.fault_harness.comparator` nor `scripts/run_b004_fault_harness.py` exposes `--update-snapshots` or any automatic overwrite path.
- **Hosted Staging & Live External GitHub Verification Boundary**:
  - Hosted staging environments (Cloud Run / Supabase) and live external GitHub OAuth/API calls are explicitly **Deferred / Waived for current gate by owner decision (not executed)**. This harness executes strictly offline against `DeterministicFakeGitHubProvider` with 0 external network requests.

---

## 2. 16-Scenario Fault Matrix Results (`S01` ~ `S16`)

| ID | Scenario | Requirements | Default Branch | Injection Point(s) | Result | Invariant Violations | Canary Hits | S01 Core Convergence |
|---|---|---|---|---|---|---:|---:|---|
| `S01` | 10-page clean initial import completion (baseline) | `SYNC-01/02/05/20`, `REF-04` | `trunk` | `NONE` | **PASSED** | 0 | 0 | Baseline (`1da53839188b`) |
| `S02` | Transient 503 on PRS page 8/10 → safe checkpoint resume | `SYNC-04/10/14` | `trunk` | `BEFORE_PAGE_TX` | **PASSED** | 0 | 0 | **EXACT MATCH** (`1da53839188b`) |
| `S03` | Crash inside PG18 page TX before `COMMIT` (page 5 rollback) → resume | `SYNC-07`, `REPLAY-01` | `trunk` | `INSIDE_PAGE_TX_BEFORE_COMMIT` | **PASSED** | 0 | 0 | **EXACT MATCH** (`1da53839188b`) |
| `S04` | Crash after PG18 page `COMMIT` before worker ack (page 6) → resume | `SYNC-08`, `REPLAY-02` | `trunk` | `AFTER_PAGE_COMMIT_BEFORE_ACK` | **PASSED** | 0 | 0 | **EXACT MATCH** (`1da53839188b`) |
| `S05` | Concurrent duplicate `POST /v1/sync-runs` + 2-worker checkpoint race | `SYNC-09/10`, `REPLAY-04` | `main` | `CONCURRENT_REQUEST_AND_WORKER_RACE` | **PASSED** | 0 | 0 | N/A |
| `S06` | HTTP 200 `Content-Length` truncation + missing required fields | `SYNC-11` | `main` | `BEFORE_PAGE_TX` | **PASSED** | 0 | 0 | N/A |
| `S07` | Repeated / cyclic `Link: rel="next"` cursor detection | `SYNC-12` | `main` | `BEFORE_PAGE_TX` | **PASSED** | 0 | 0 | N/A |
| `S08` | Rate limit `429 Retry-After` and timeout exhaustion | `SYNC-13` | `main` | `BEFORE_PAGE_TX` | **PASSED** | 0 | 0 | N/A |
| `S09` | Mid-run PR cue drift & default-branch/HEAD drift (`PLAN_DRIFT`) | `SYNC-16/17` | `main` | `BEFORE_FINALIZE_SUCCESS` | **PASSED** | 0 | 0 | N/A |
| `S10` | `UNKNOWN` PR association block + `NO_MATCH_OBSERVED` badge + out-of-window/squash exclusions | `SYNC-18/19` | `main` | `BEFORE_FINALIZE_SUCCESS` | **PASSED** | 0 | 0 | N/A |
| `S11` | PR-linked commits deduplicated strictly under PR `MUST_LINK` (0 `DIRECT_COMMIT`) | `SYNC-20` | `master` | `NONE` | **PASSED** | 0 | 0 | N/A |
| `S12` | `max_objects` / `max_requests` / `max_total_bytes` limits (`PARTIAL_WITH_LIMIT`) | `SYNC-21` | `main` | `BEFORE_PAGE_TX` | **PASSED** | 0 | 0 | N/A |
| `S13` | Mid-run `PUBLIC→PRIVATE` flip, `401` token revocation, policy API `503` | `SYNC-22`, `POLICY-01` | `main` | `BEFORE_FINALIZE_SUCCESS` | **PASSED** | 0 | 0 | N/A |
| `S14` | Transient retry vs `PLAN_DRIFT` abandoned isolation vs post-`SUCCESS` `409` | `SYNC-04/14/16` | `main` | `BEFORE_PAGE_TX`, `BEFORE_FINALIZE_SUCCESS` | **PASSED** | 0 | 0 | N/A |
| `S15` | Pre-`IMPORT_READY` block + duplicate completion idempotency + refetch match/mismatch | `REPLAY-01/02/04`, `REF-04/06` | `main` | `AFTER_FINALIZE_BEFORE_DERIVED_UNITS` | **PASSED** | 0 | 0 | N/A |
| `S16` | Post-`SUCCESS` `OD-003` deletion fence & pre-resume deletion/`PRIVATE` cancellation | `SYNC-22`, `OD-003` | `main` | `AFTER_FINALIZE_BEFORE_DERIVED_UNITS`, `BEFORE_PAGE_TX` | **PASSED** | 0 | 0 | N/A |

### Explicit Scope Disclosure (`S09` Completion Gate)
- The completion gate verifies repository `default_branch`, `default_branch_head_sha`, and 2nd-pass merged PR cues (`pr_number`, `head_sha`, `updated_at`, `merge_commit_sha`). Global review/comment mutation detection across all PR threads is explicitly **not assumed**.

---

## 3. Six Global Invariants (`INV-1` ~ `INV-6`) & `REF-04/06` Canary Audit

Every scenario is checked at both **intermediate crash/pause boundaries** and **final state** directly against PostgreSQL 18:

1. **`INV-1` (`checkpoint <= committed page`)**: `0` violations across all 16 scenarios.
2. **`INV-2` (Zero `IMPORT_READY` before `SUCCESS`)**: `0` violations (`0` rows before `SUCCESS`, strictly `1` per succeeded run).
3. **`INV-3` (Zero domain promotions before `SUCCESS`)**: `0` violations (`work_events`, `content_units`, `facts`, `claims` all `0` before `SUCCESS`).
4. **`INV-4` (Zero concurrent `checkpoint_version` duplicate writes)**: `0` violations (verified with synchronized 2-worker thread race in `S05` and stale recommit in `S04`, yielding `STALE_CHECKPOINT_VERSION`).
5. **`INV-5` (`DIRECT_COMMIT` integrity & confirmation badge)**: `0` violations (`UNKNOWN`, `MATCHED_PR`, out-of-window PR, and squash/merge commits produce `0` `DIRECT_COMMIT` candidates; every `NO_MATCH_OBSERVED` `DIRECT_COMMIT` carries `requires_user_confirmation=True`).
6. **`INV-6` (Zero unauthorized raw/secret exposure — `REF-04/06`)**: `0` violations:
   - Synthetic canaries (`CANARY_B004_RAW_PR_BODY_*`, `CANARY_B004_RAW_COMMIT_MSG_*`, `CANARY_B004_RAW_REVIEW_BODY_*`, `CANARY_B004_OAUTH_TOKEN_*`) scanned across **all `public.*` PostgreSQL tables**, captured Python logs, non-ephemeral HTTP API responses, and generated redacted bundles: **0 hits total**.
   - All `public.content_units` rows verified: `text_content IS NULL`, `content_ref IS NULL`, `retention_class = 'NONE'`, `expires_at IS NULL`.
   - Blocked/private/revoked/deleted/`BLOCKED_SOURCE` resources block `GET /v1/content-units/{id}/content`, `AI_TRANSMISSION`, `EXPORT`, and `verify_required_supports_for_gate`.

---

## 4. Production Defects Discovered & Fixed by B-004 Execution

Running the real M2 stack on PostgreSQL 18 uncovered two concrete production issues, both resolved cleanly within M2 scope:

1. **`DerivedUnitIngestionProcessor.verify_unit_and_project_status` (`derived_units.py`)**: Referenced nonexistent enum member `ResolutionStatus.SOURCE_UNAVAILABLE` instead of `ResolutionStatus.UNAVAILABLE` (caught during `S15` post-`SUCCESS` `REVISION_MISMATCH` projection). Fixed to `ResolutionStatus.UNAVAILABLE`.
2. **`DerivedUnitIngestionProcessor` Gate 2 deletion fence handling (`derived_units.py`)**: When `is_deletion_fence_active` fired on an already-`SUCCESS` run in `S16`, Gate 2 projected `processing_status = 'BLOCKED_POLICY'` without transitioning existing `content_units.policy_state` to `'BLOCKED'`. Fixed to atomically set `content_units.policy_state = 'BLOCKED'` inside the same transaction.
   - *M5 `T-502` Defense-in-Depth Note*: Production `OD-003` deletion-confirm persistence in M5 should also atomically transition `content_units.policy_state = 'BLOCKED'` (or enforce `deletion_jobs` fence lookup inside `PostgresProvenanceMetadataStore`) so raw viewer requests arriving between `deletion-confirm` and background job execution are rejected with zero window.
3. **`DIRECT_COMMIT` confirmation flag (`association_classifier.py`, `postgres_page_committer.py`)**: Added explicit `requires_user_confirmation: True` alongside `user_review_required: True` in `DIRECT_COMMIT` `summary_metadata` and `direct_commit_badge_metadata`.

---

## 5. `T-005D` Independent Reviewer File Checklist & Reproduction Commands

### Files to Inspect
- Harness core (`T-005A`/`T-005B`/`T-005C`):
  - `apps/api/src/career_memory_api/fault_harness/scenario_schema.py`
  - `apps/api/src/career_memory_api/fault_harness/fake_provider.py`
  - `apps/api/src/career_memory_api/fault_harness/fault_hooks.py`
  - `apps/api/src/career_memory_api/fault_harness/snapshot.py`
  - `apps/api/src/career_memory_api/fault_harness/comparator.py`
  - `apps/api/src/career_memory_api/fault_harness/scenarios.py`
  - `apps/api/src/career_memory_api/fault_harness/runner.py`
  - `apps/api/src/career_memory_api/fault_harness/CONTRACT.md`
- Static read-only expected snapshots & redacted evidence bundle:
  - `contracts/fault-harness/expected_snapshots/S01.json` ~ `S16.json`
  - `contracts/fault-harness/b004_evidence_bundle.v1.json`
- Reproducible CLI & test suites:
  - `scripts/run_b004_fault_harness.py`
  - `apps/api/tests/fault_harness/test_b004_unit_and_schema.py`
  - `apps/api/tests/fault_harness/test_b004_fault_matrix_pg18.py`

### Exact Reproduction Commands
```bash
git rev-parse HEAD:contracts/import-v1/openapi.yaml
cd apps/api && uv run python ../../scripts/run_b004_fault_harness.py --verify-bundle
cd apps/api && uv run pytest -v -m ""
cd apps/api && uv run python ../../scripts/run_conformance_ci.py --with-postgres
cd spikes/web-import && npm run typecheck && npm test
```
