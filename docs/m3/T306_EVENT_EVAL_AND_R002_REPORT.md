# M3 Event Quality Evaluation & `R-002` Curation Telemetry Report (`T-306` + `T-306A`, Issue #11)

## 1. Executive Summary & Governance Classification

- **Track / Task IDs**: `M3-WAVE4-T306A-EVAL` (`T-306` Event quality eval + curation usability telemetry & `T-306A` Execute frozen Event dataset on production implementation, GitHub Issue `#11`)
- **Authoritative Frozen Dataset**:
  - **Entrypoint Manifest**: `evaluation/event-draft/releases/1.1.0-mvp-approved/manifest.json` (loaded strictly via `validate_approved_release.load_approved_21_case_set()`)
  - **Dataset Version**: `1.1.0-mvp-approved` (`15` unchanged baseline cases + `1` replacement `DELTA-EV-002` + `5` new `CAND-EV-019..023` = **21 cases** across **16 same-source bundles**)
  - **Composite SHA-256**: `91cac742b48ad3bebee2e84aac3f6d3c5d9202c65d2406f3828b8494bf114a4d`
  - **Frozen Immutability (`ADR-018`)**: **0 bytes modified** under `evaluation/event-draft/**`; oracle comparison is strictly **read-only** with zero auto-overwrite flags.
- **Database & Schema Scope**:
  - Real local ephemeral **PostgreSQL 18** cluster with migrations **`0010` ~ `0060`** (`0010_identity_workspace_source_auth.sql` through `0060_m3_event_versions_and_constraints.sql`), least-privilege `NOBYPASSRLS` roles (`cm_api_app`, `cm_worker_app`), and `ENABLE + FORCE ROW LEVEL SECURITY`.
- **Immutable OpenAPI Contract (`contracts/import-v1/openapi.yaml`)**:
  - Verified unchanged at Git blob SHA `07594d75425744d6149250a9f7ff95ac24659f80`.
- **Production Stack Exercised (Zero Mock/Shadow Rule Engines)**:
  - `career_memory_api.event_candidates.PostgresEventCandidateBuilder` (plus replay idempotency verification)
  - `career_memory_api.work_events.PostgresWorkEventsReadService` (`GET /v1/work-events`, `GET /v1/work-events/{id}`)
  - `career_memory_api.work_events.PostgresWorkEventMutationService` (`confirm`, `edit`, `reject`, `split`, `merge`, `dissolve`)
  - `career_memory_api.work_events.create_work_events_router` (executed via FastAPI `TestClient`)
  - `career_memory_api.work_events.query_searchable_top_level_work_events` & `collapse_event_ids_for_search_and_export`
- **Overall Result**:
  - **21 / 21 cases PASSED** across **16 / 16 same-source bundles**
  - **1,054** confirmed logical oracle assertions verified, **0 oracle diffs**
  - **0 Plan C (`ADR-013`) violations** (`text_content IS NULL`, `content_ref IS NULL`, `extracted_span IS NULL`, **0** raw PR body canary hits across 39 PG18 tables, **0** forbidden JSONB keys)
  - **0 deterministic drift** between live PG18 execution and `contracts/event-eval/t306a_eval_bundle.v1.json`
- **Governance Classification (`T-306`)**:
  - Results are classified strictly as **contract regression** (`SYNTHETIC_CONTRACT_REGRESSION`) and **`R-002` usability evidence** (`CLOSED_PILOT_USER_TELEMETRY`), **NOT** architecture go/no-go.
  - Per `T-306`, curation telemetry or review-cost observations **MUST NOT** activate automatic multi-PR or multi-direct-commit clustering in MVP (`auto_clustering_enabled = false`).

---

## 2. Production Contract Defect Caught & Fixed by `T-306A` Execution

Running the frozen 21-case release against real PostgreSQL 18 exposed one genuine production contract defect, which was fixed directly in production code without touching any frozen fixture:

1. **Multi-token non-contiguous search query handling in `query_searchable_top_level_work_events` (`apps/api/src/career_memory_api/work_events/search_collapse.py`)**:
   - **Observed Failure**: During `EV-019` (`CAND-EV-019`), `search_checks[1]` queried `"currency rounding"` expecting `ev_cand_019_pr_rebase` (PR #1902 titled `"Add ledger currency precision rounding tests"`). Because `query_searchable_top_level_work_events` matched `ILIKE '%currency rounding%'` as a single contiguous substring, it returned `0` results instead of `1`.
   - **Fix**: Updated `query_searchable_top_level_work_events` to split `query_text` into deduplicated whitespace tokens, escape SQL `LIKE` metacharacters (`\`, `%`, `_`), and require every token (`AND` semantics) to match case-insensitively across non-raw WorkEvent fields (`title`, `problem`, `action`, `outcome`) or linked `source_objects.provider_object_id` before collapsing through `collapse_event_ids_for_search_and_export`.
   - **Verification**: `EV-019` (`CAND-EV-019`) now returns exact `1` collapsed result (`ev_cand_019_pr_rebase`) while preserving `0` matches on negative queries (`"fee schedule parser"`, `"legacy maintenance patch"`), and all 21/21 cases + existing `work_events` PG18 tests pass.

---

## 3. 16-Bundle & 21-Case Evaluation Matrix (`1.1.0-mvp-approved`)

### 3.1 Same-Source Evaluation Bundles (`16 / 16 PASSED`)

| # | `eval_bundle_id` | Cases | Result |
|---:|---|---|---|
| 1 | `bundle_mvp_ledger_service` | `EV-001`, `EV-015` | **2/2 PASSED** |
| 2 | `bundle_mvp_edge_gateway` | `EV-002` (`DELTA-EV-002`) | **1/1 PASSED** |
| 3 | `bundle_mvp_billing_engine` | `EV-003` | **1/1 PASSED** |
| 4 | `bundle_mvp_search_indexer` | `EV-004` | **1/1 PASSED** |
| 5 | `bundle_mvp_notification_hub` | `EV-005` | **1/1 PASSED** |
| 6 | `bundle_mvp_catalog_api` | `EV-006`, `EV-009`, `EV-012` | **3/3 PASSED** |
| 7 | `bundle_mvp_reporting_core` | `EV-007`, `EV-008` | **2/2 PASSED** |
| 8 | `bundle_mvp_queue_dispatcher` | `EV-010` | **1/1 PASSED** |
| 9 | `bundle_mvp_web_portal` | `EV-011` | **1/1 PASSED** |
| 10 | `bundle_mvp_auth_services` | `EV-013` | **1/1 PASSED** |
| 11 | `bundle_mvp_analytics_pipe` | `EV-014` | **1/1 PASSED** |
| 12 | `bundle_mvp_multi_workspace` | `EV-016` | **1/1 PASSED** |
| 13 | `bundle_cand_payments_trunk` | `EV-019` (`CAND-EV-019`) | **1/1 PASSED** |
| 14 | `bundle_cand_legacy_master` | `EV-020` (`CAND-EV-020`) | **1/1 PASSED** |
| 15 | `bundle_cand_policy_engine` | `EV-021` (`CAND-EV-021`) | **1/1 PASSED** |
| 16 | `bundle_cand_workflow_audit` | `EV-022` (`CAND-EV-022`), `EV-023` (`CAND-EV-023`) | **2/2 PASSED** |

### 3.2 Per-Case Execution & Read-Only Oracle Comparison (`21 / 21 PASSED`)

| Case ID | Source Record | Origin | Bundle | Execution Mode | Assertions | Diffs | Replay Idemp. | Plan C | Logical State SHA-256 |
|---|---|---|---|---|---:|---:|---|---|---|
| `EV-001` | `EV-001` | `BASELINE_1_0_0` | `bundle_mvp_ledger_service` | `CANDIDATE_BUILDER` | 29 | 0 | **PASS** | **PASS** | `bde18e86311a` |
| `EV-002` | `DELTA-EV-002` | `REPLACED_FROM_M0_GATE_DELTA` | `bundle_mvp_edge_gateway` | `CANDIDATE_BUILDER` | 32 | 0 | **PASS** | **PASS** | `21457ea1f6de` |
| `EV-003` | `EV-003` | `BASELINE_1_0_0` | `bundle_mvp_billing_engine` | `CANDIDATE_BUILDER` | 18 | 0 | **PASS** | **PASS** | `e17b0c3e8afe` |
| `EV-004` | `EV-004` | `BASELINE_1_0_0` | `bundle_mvp_search_indexer` | `CANDIDATE_BUILDER` | 17 | 0 | **PASS** | **PASS** | `c2c0ee34c6d6` |
| `EV-005` | `EV-005` | `BASELINE_1_0_0` | `bundle_mvp_notification_hub` | `CANDIDATE_BUILDER` | 29 | 0 | **PASS** | **PASS** | `a9bfdc04840e` |
| `EV-006` | `EV-006` | `BASELINE_1_0_0` | `bundle_mvp_catalog_api` | `CANDIDATE_BUILDER` | 43 | 0 | **PASS** | **PASS** | `ab812614c870` |
| `EV-007` | `EV-007` | `BASELINE_1_0_0` | `bundle_mvp_reporting_core` | `CANDIDATE_BUILDER` | 31 | 0 | **PASS** | **PASS** | `1bd28d2e7d60` |
| `EV-008` | `EV-008` | `BASELINE_1_0_0` | `bundle_mvp_reporting_core` | `CURATION_COMMANDS` | 73 | 0 | N/A | **PASS** | `1cd05a1a3d0f` |
| `EV-009` | `EV-009` | `BASELINE_1_0_0` | `bundle_mvp_catalog_api` | `CURATION_COMMANDS` | 65 | 0 | N/A | **PASS** | `e45b8b29729d` |
| `EV-010` | `EV-010` | `BASELINE_1_0_0` | `bundle_mvp_queue_dispatcher` | `READ_COLLAPSE` | 82 | 0 | N/A | **PASS** | `92ee590718ad` |
| `EV-011` | `EV-011` | `BASELINE_1_0_0` | `bundle_mvp_web_portal` | `CURATION_COMMANDS` | 49 | 0 | N/A | **PASS** | `148cf824f572` |
| `EV-012` | `EV-012` | `BASELINE_1_0_0` | `bundle_mvp_catalog_api` | `CURATION_COMMANDS` | 72 | 0 | N/A | **PASS** | `a81cca3f4974` |
| `EV-013` | `EV-013` | `BASELINE_1_0_0` | `bundle_mvp_auth_services` | `CURATION_COMMANDS` | 40 | 0 | N/A | **PASS** | `d76df833f7f7` |
| `EV-014` | `EV-014` | `BASELINE_1_0_0` | `bundle_mvp_analytics_pipe` | `CURATION_COMMANDS` | 105 | 0 | N/A | **PASS** | `6f697ec0a307` |
| `EV-015` | `EV-015` | `BASELINE_1_0_0` | `bundle_mvp_ledger_service` | `CURATION_COMMANDS` | 34 | 0 | N/A | **PASS** | `5bc652971cb3` |
| `EV-016` | `EV-016` | `BASELINE_1_0_0` | `bundle_mvp_multi_workspace` | `CURATION_COMMANDS` | 72 | 0 | N/A | **PASS** | `c34af38eff2f` |
| `EV-019` | `CAND-EV-019` | `NEW_FROM_M0_GATE_DELTA` | `bundle_cand_payments_trunk` | `CANDIDATE_BUILDER` | 75 | 0 | **PASS** | **PASS** | `bb7b18b1dcc3` |
| `EV-020` | `CAND-EV-020` | `NEW_FROM_M0_GATE_DELTA` | `bundle_cand_legacy_master` | `CANDIDATE_BUILDER` | 24 | 0 | **PASS** | **PASS** | `a033cdcfd365` |
| `EV-021` | `CAND-EV-021` | `NEW_FROM_M0_GATE_DELTA` | `bundle_cand_policy_engine` | `CURATION_COMMANDS` | 59 | 0 | N/A | **PASS** | `9e018ff832d0` |
| `EV-022` | `CAND-EV-022` | `NEW_FROM_M0_GATE_DELTA` | `bundle_cand_workflow_audit` | `CURATION_COMMANDS` | 43 | 0 | N/A | **PASS** | `6841235e2589` |
| `EV-023` | `CAND-EV-023` | `NEW_FROM_M0_GATE_DELTA` | `bundle_cand_workflow_audit` | `CURATION_COMMANDS` | 62 | 0 | N/A | **PASS** | `7caf623ad967` |

Key cross-cutting verifications confirmed in the live PG18 run:
- **Zero-mutation rejection guarantee**: All 10 rejected curation commands (`EV-013`, `EV-014`, `EV-015`, `EV-016`, `CAND-EV-021`) proved exact pre-command == post-command PostgreSQL state SHA-256 (`state_mutated = false`).
- **Dual cross-workspace verification (`EV-016`)**: `cmd_syn_016_cross_merge` verified BOTH external HTTP `404 RESOURCE_NOT_FOUND` concealment AND internal domain `CROSS_WORKSPACE_MEMBER` policy enforcement (`conceal_missing_as_not_found=False`), while `GET /v1/work-events` enforced strict FORCE RLS tenant isolation (`ws_syn_016_a = 2`, `ws_syn_016_b = 1`).
- **Route handoff compliance (`CAND-EV-021`, `CAND-EV-022`, `CAND-EV-023`)**: Manual merge executed via `POST /v1/work-events/actions/merge`, confirm via `POST /v1/work-events/{id}/actions/confirm` (no invented Keep API), edit via `PATCH /v1/work-events/{id}`.

---

## 4. Explicitly Excluded Unconfirmed Fields (`T-306A-HANDOFF.md` §3) & M5 Contract Boundaries

### 4.1 Excluded from Ground-Truth Comparison (`T-306A-HANDOFF.md` §3)
- UI badge strings & unconfirmed API field names for direct-commit association confirmation (`DELTA-EV-002`, `CAND-EV-019`)
- External API `ReviewAction` response tokens (`CAND-EV-022`, `CAND-EV-023`; domain `ReviewAction` `CONFIRM`/`EDIT` IS asserted)
- Unconfirmed HTTP status codes (`409` vs `412`, `If-Match`, `403` vs `404`, `CANNOT_LINK_CONFLICT` HTTP code, confirm/edit HTTP codes)
- Internal diagnostic record persistence format & live GitHub API lookup assumptions for out-of-window PR deduplication (`CAND-EV-019`)
- `OD-007` physical database table / storage layout details
- Editing behavior on an already-`CONFIRMED` Event (`CAND-EV-023` covers active `CANDIDATE` → new `CANDIDATE` only)
- Multi-violation error precedence (`EV-014` `cmd_syn_014_nested`: `MEMBER_ALREADY_COMPOSED` vs `MEMBER_NOT_ACTIVE`), non-Event ID wire error codes (`EV-013`), unauthorized foreign-workspace external wire codes (`EV-016`), mixed-purpose flag literal (`EV-007`), and historical `SUPERSEDED` detail GET shape (`EV-010`)

### 4.2 Unimplemented M5 Contract Boundaries (`T-306A` Step 4)
1. **M5 Full-Text Search (FTS) Ranking Engine**: Not yet implemented in M3. All `search_checks` are verified strictly at the contract level for single-logical-event collapse (`collapse_event_ids_for_search_and_export`) and non-raw top-level projection (`query_searchable_top_level_work_events` + `GET /v1/work-events` under `FORCE RLS`).
2. **M5 Export Generation / Download Adapter**: No `exports` table exists in migrations `0010`–`0060`. In `EV-012` (`dissolve`), dependent `facts` and `claims` are verified directly as `STALE` (not deleted, not transferred to members) in PG18, while `EXPORT` `STALE` invalidation is verified at the contract boundary via the atomic `WORK_EVENT_COMPOSITE_DISSOLVED` outbox cleanup event (`cleanup_required: true`).

---

## 5. `R-002` Curation Usability Telemetry — Strict Partition Separation

Synthetic contract regression metrics and real closed-pilot human usability telemetry are **structurally isolated** so synthetic command latency is NEVER presented as real user curation time:

| Metric | `SYNTHETIC_CONTRACT_REGRESSION` (Frozen 21-Case Harness) | `CLOSED_PILOT_USER_TELEMETRY` (`R-002` Real Human Pilot) |
|---|---|---|
| **Classification** | `CONTRACT_REGRESSION_ONLY_NOT_ARCHITECTURE_GO_NO_GO` | `R-002_USABILITY_EVIDENCE` |
| **Status** | `SYNTHETIC_21_CASE_REGRESSION_EXECUTED` | `NOT_YET_COLLECTED_PRE_PILOT` |
| **Session / Case Count** | 17 active telemetry cases (21 total evaluated) | **0** (`pilot_session_count = 0`) |
| **Review / Execution Time (`p50` / `p95`)** | Recorded solely in `volatile_runtime_metrics` (automated PG18/HTTP execution ms; excluded from deterministic bundle hash) | **`null` / `null`** (not yet collected; never fabricated from synthetic timings) |
| **Action Counts (Total / Accepted / Rejected)** | `CONFIRM`: 1 (1/0), `EDIT`: 1 (1/0), `SPLIT`: 1 (1/0), `MERGE`: 9 (1/8), `DISSOLVE`: 1 (1/0), `REJECT`: 3 (1/2) | All `0` |
| **Action Distribution Ratios** | `MERGE`: 56.25%, `REJECT`: 18.75%, `CONFIRM`/`EDIT`/`SPLIT`/`DISSOLVE`: 6.25% each | All `0.0` |
| **Candidate Anchor Ratio (`PR` vs `DIRECT_COMMIT`)** | `PR`: 7, `DIRECT_COMMIT`: 2 (`DIRECT_COMMIT/PR` = `0.2857`, `DIRECT_COMMIT` share = `22.22%`) | `PR`: 0, `DIRECT_COMMIT`: 0 (`null`) |
| **`USER_COMPOSITE` Merge Frequency** | 1 accepted / 6 accepted commands (`16.67%`) across 9 attempts (8 negative guard checks) | `0` (`null`) |
| **Review Flag Hit / Utilization Rate** | Hit rate `44.44%` (4 flagged / 9 candidates), utilization `25.00%` (1/4 in `EV-008` split) | `null` / `null` |
| **Post-Merge Top-Level / Search / Export Duplicates** | **0** | **0** |
| **Auto-Clustering Enabled** | **`false`** (prohibited by `T-306`) | **`false`** (prohibited by `T-306`) |

---

## 6. Exact Reproduction Commands

```bash
# 1. Frozen dataset 3-layer immutability & integrity verification
python3 -B evaluation/event-draft/lint/lint_fixtures.py --dataset-dir evaluation/event-draft --verify-negatives
python3 -B evaluation/event-draft/review-candidates/m0-gate-spec-delta/validate_delta_candidates.py
python3 -B evaluation/event-draft/releases/1.1.0-mvp-approved/validate_approved_release.py

# 2. Live PG18 21-case production evaluation & 0-drift bundle verification
cd apps/api && uv run python ../../scripts/run_t306a_event_eval.py --verify-bundle

# 3. Full API unit + PostgreSQL 18 integration test suite (0 skipped, 0 failed)
cd apps/api && uv run pytest -v -m ""

# 4. Regression verification: B-004 fault harness & Conformance CI
cd apps/api && uv run python ../../scripts/run_b004_fault_harness.py --verify-bundle
cd apps/api && uv run python ../../scripts/run_conformance_ci.py --with-postgres

# 5. Frozen dataset untouched & immutable OpenAPI contract verification
git status --porcelain evaluation/event-draft
git rev-parse HEAD:contracts/import-v1/openapi.yaml
```
