# T-005D Independent Adversarial Review & Re-Audit Report (`B-004` Initial Import 16-Scenario Fault Matrix)

```text
VERDICT: PASS
B-004 GATE RELEASE (LOCAL PG18): APPROVED (16/16 PASSED after Round 2 re-audit; BLOCKER-1 & NB-1..3 resolved)
ROUND 1 VERDICT (69a15529): FAIL_WITH_BLOCKERS (1 BLOCKER — S16 / PostgresProvenanceMetadataStore zero-window deletion fence)
ROUND 2 VERDICT (2a413a9d): PASS (0 BLOCKERS; zero-window probe & full PG18/conformance/web-import suites green)
```

## 1. Audit Metadata & Scope

| Field | Value |
|---|---|
| **Track / Task IDs** | Round 1: `M2-T005D-REVIEW` / Round 2: `M2-T005D-REAUDIT` (`T-005D — Execute initial import matrix + B-004 review`) |
| **Reviewer Role** | Independent Adversarial Reviewer (strictly separated from `M2-B004-HARNESS` & `M2-B004-HARNESS-FIX`) |
| **Round 1 Reviewed Commit** | `eddy81848/m2-b004-fault-harness` @ `69a15529c9ccb434369574af3812637222109e86` (`FAIL_WITH_BLOCKERS` recorded at `89198b21d03f4fc6a1e5b9c93335e66002c30ef0`) |
| **Round 2 Remediation Commit** | `2a413a9d2b71602f5825f73ab4e534a6d27b842f` (*"fix(b004): enforce zero-window OD-003 deletion fence in resolver store (T-005D BLOCKER-1, NB-1..3)"*), merged cleanly with `0` conflicts |
| **Immutable OpenAPI Contract (`contracts/import-v1/openapi.yaml`)** | `07594d75425744d6149250a9f7ff95ac24659f80` (**VERIFIED UNCHANGED across Round 1 & Round 2**) |
| **Execution Environment** | Local ephemeral PostgreSQL 18 (`migrations 0010..0050`, `NOBYPASSRLS` roles `cm_api_app` & `cm_worker_app`), offline `DeterministicFakeGitHubProvider` (0 external network calls) |
| **Hosted Staging / Live GitHub / Live LLM** | **Deferred / Waived for current gate by owner decision (not executed)** — verified honestly disclosed without false pass claims |

---

## 2. Executive Verdict & Audit Trail (Round 1 → Round 2)

1. **Round 1 (`69a15529` → `FAIL_WITH_BLOCKERS`, committed at `89198b21`)**: Independent code audit and live PostgreSQL 18 adversarial probing verified `S01`–`S15`, `ADR-017` real production stack execution, `T-005C` read-only comparator integrity, negative mutation detection, Plan C redaction, and hosting waiver honesty, but **rejected `B-004` release** due to **`BLOCKER-1`** (`S16` / `OD-003` zero-window deletion fence bypass in `PostgresProvenanceMetadataStore`, masked by `_run_s16` invoking `DerivedUnitIngestionProcessor` before checking policy gates).
2. **Round 2 (`2a413a9d` → `PASS`)**: After merging `2a413a9d2b71602f5825f73ab4e534a6d27b842f`, full line-by-line diff audit, re-execution of the **exact same adversarial zero-window PostgreSQL 18 probe**, and full re-execution of the 16-scenario fault matrix (`--verify-bundle`), 311-test pytest suite, PG18 conformance CI, and `spikes/web-import` typecheck/tests confirm **`BLOCKER-1` is completely closed (0ms window)** and **`NB-1`–`NB-3` are also resolved**, with **0 regressions and 0 redaction leaks**.

Under Notion `B-004` release criteria (*"독립 검토자가 T-005D의 결과 묶음을 확인"* & *"모든 필수 시나리오가 통과한 뒤에만 B-004를 Resolved로 바꾼다"*), this independent review upgrades the final verdict to **`VERDICT: PASS`** for the local PostgreSQL 18 `B-004` gate (with hosted staging / live GitHub / live LLM explicitly remaining under the owner-approved hosting waiver boundary).

---

## 3. `BLOCKER-1` Audit History & Before/After Live PostgreSQL 18 Empirical Proof

### 3.1 Round 1 Finding (`69a15529` — `FAIL_WITH_BLOCKERS`)
- `PersistedUnitResolutionTarget` (`resolver/models.py:325`) and `evaluate_target_policy` (`resolver/policy.py:217-222`) checked `target.deletion_fence_active`, but `PostgresProvenanceMetadataStore` (`resolver/postgres_store.py`) never queried `public.deletion_jobs` and left `deletion_fence_active=False` on every loaded target (including `REF-07e` `check_policy_fence_current`).
- Because `PostgresJobRuntime.request_source_deletion()` (`jobs/postgres_runtime.py:1052-1152`) inserts `public.deletion_jobs(status='PENDING')` without mutating `source_resources.status` or `content_units.policy_state`, any raw viewer (`GET /v1/content-units/{id}/content`) or resolver gate (`APPROVAL` / `EXPORT_DOWNLOAD`) arriving between `deletion-confirm` (`202`) and background worker execution succeeded and exposed raw source content.
- In Round 1, `_run_s16` (`runner.py:2699-2703`) manually invoked `stack_a.derived_processor.process_import_ready_run(...)` before `_check_policy_gates(...)`, masking this exposure window.

### 3.2 Round 2 Code Audit of Remediation (`2a413a9d`)
- **`apps/api/src/career_memory_api/resolver/postgres_store.py`**:
  - Added `_query_deletion_fence_active(cur, workspace_id, source_resource_id)` guarded by `SELECT to_regclass('public.deletion_jobs') IS NOT NULL` (preserving backward compatibility with `0010+0020`-only resolver test clusters) and querying `public.deletion_jobs` for `status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED')` — matching `idx_deletion_jobs_active_fence` in `db/migrations/0040_sync_runs_jobs_outbox_audit.sql:1022-1024` and `is_deletion_fence_active()` in `jobs/postgres_runtime.py:53-78` under `workspace_scoped_transaction` (`cm_api_app` `NOBYPASSRLS`).
  - Wired `deletion_fence_active` into **all three store read paths**:
    1. `get_unit_resolution_target` (L190–L192)
    2. `list_sibling_unit_anchors` (L217–L234, cached per distinct `source_resource_id` within the same transaction)
    3. `check_policy_fence_current` (`REF-07e` TOCTOU guard, L236–L241, delegates directly to `get_unit_resolution_target`)
- **`apps/api/src/career_memory_api/fault_harness/runner.py` (`_run_s16`)**:
  - Now executes `_check_policy_gates` and `verify_b004_invariants` **immediately after `POST /deletion-confirm` returns `202` and BEFORE invoking `derived_processor.process_import_ready_run()`** (L2688–L2713), asserting `refetch_content_allowed is False` (`POLICY_BLOCKED`), `external_ai_gate_allowed is False`, `export_gate_allowed is False`, and `claim_approval_gate_allowed is False`.
- **`apps/api/tests/fault_harness/test_b004_fault_matrix_pg18.py`**:
  - Added dedicated PG18 regression test `test_b004_od003_deletion_confirm_zero_window_blocks_resolver_and_gates_immediately` (L133–L245) verifying `HTTP 403`, zero canary leakage, and `POLICY_BLOCKED` on both `APPROVAL` and `EXPORT_DOWNLOAD` gates before any worker runs.

### 3.3 Before vs. After Live PostgreSQL 18 Adversarial Probe Comparison (0ms Pre-Worker Window)

Exact same probe sequence: `S16` Part A `SUCCESS` → `POST /v1/source-resources/{id}/deletion-preview` → `POST /v1/source-resources/{id}/deletion-confirm` (`202`) → **NO background worker / NO `DerivedUnitIngestionProcessor` call** → immediate `PostgresProvenanceMetadataStore`, `GET /v1/content-units/{id}/content`, `RefetchResolverService` gates, `runner._check_policy_gates`, and `verify_b004_invariants`:

| Adversarial Probe Measurement (0ms Post-`deletion-confirm`, Pre-Worker) | Round 1 (`69a15529`) — **FAIL** | Round 2 (`2a413a9d`) — **PASS** |
|---|---|---|
| `POST /deletion-confirm` HTTP status | `202` | `202` |
| `get_unit_resolution_target().deletion_fence_active` | `False` (`resource_status=ACTIVE`, `unit_policy_state=ACTIVE`) | **`True`** (`resource_status=ACTIVE`, `unit_policy_state=ACTIVE`) |
| `list_sibling_unit_anchors()` all `deletion_fence_active` | *(unwired)* | **`True` (`1/1` fenced)** |
| `check_policy_fence_current().deletion_fence_active` (`REF-07e`) | `False` | **`True`** |
| `GET /v1/content-units/{id}/content` HTTP status | **`200 OK`** | **`403 Forbidden`** |
| Raw canary exposed in viewer response (`raw_canary_exposed`) | **`True` (LEAK)** | **`False` (0 leak)** |
| Resolver `APPROVAL` gate (`allowed` / `status`) | **`True` / `MATCHED`** | **`False` / `POLICY_BLOCKED`** |
| Resolver `EXPORT_DOWNLOAD` gate (`allowed` / `status`) | **`True` / `MATCHED`** | **`False` / `POLICY_BLOCKED`** |
| `runner._check_policy_gates` (`refetch`, `ai`, `export`, `approval`) | `True, False, False, True` | **`False, False, False, False` (`POLICY_BLOCKED`)** |
| `verify_b004_invariants` violations | **`1` (`[INV-6]`)** | **`0` violations** |

---

## 4. 7-Axis Independent Audit Summary (Post-Re-Audit)

| Audit Axis | Round 1 | Round 2 (Final) | Key Findings |
|---|---|---|---|
| **1. `ADR-017` Real Production Path Authenticity** | **PASS** | **PASS** | All 16 scenarios invoke real M2 production classes (`InitialBackfillOrchestrator`, `InitialBackfillJobWorker`, `DerivedUnitIngestionProcessor`, `PostgresBackfillPageCommitter`, `PostgresSyncRunService`, `PostgresJobRuntime`, `PostgresGitHubConnectService`, `HttpGitHubImportClient`, `SafeGitHubHttpTransport`, `RefetchResolverService`, `PostgresProvenanceMetadataStore`, `contracts_api`) against real PG18 migrations `0010`..`0050` with `NOBYPASSRLS` roles. Zero shadow/mock importer implementations. |
| **2. 16-Scenario Matrix & Requirements 1:1 Audit** | **FAIL (`15/16`)** | **PASS (`16/16`)** | `S01`–`S15` verified in Round 1 and re-verified in Round 2. `S16` now passes all 4 sub-parts (Part A zero-window post-`SUCCESS` deletion fence + post-worker `BLOCKED_POLICY`; Part B pre-resume deletion fence + `/retry` `404` + 0 resurrection; Part C pre-resume `PRIVATE` flip halt; Part D live post-`SUCCESS` upstream `PRIVATE` flip immediate `403 POLICY_BLOCKED`). |
| **3. `T-005C` Comparator & Negative Mutation Tests** | **PASS** | **PASS** | Strictly read-only comparator; zero `--update-snapshots` / write flags. Re-verified in Round 2: in-memory comparator mutations caught `3/3` field diffs; PG18 negative mutations triggered all 6 invariants (`INV-1`..`INV-6`) and `3/3` canary hits (`pg:public.audit_events[5]`, `log[0]`, `api_response[0]`). |
| **4. Plan C (`REF-04/06`) & Redaction Audit** | **PASS** | **PASS** | Re-verified after `S16.json`, `b004_evidence_bundle.v1.json`, and `B004_FAULT_HARNESS_REPORT.md` updates: `0` real secrets, `0` concrete canary markers, and `0` raw PR/commit/review text bodies. Schema versions (`cm-b004-scenario-v1`, `cm-b004-expected-v1`, `cm-b004-evidence-bundle-v1`) enforced. |
| **5. Independent Local PG18 & Web-Import Execution** | **PASS** | **PASS** | `run_b004_fault_harness.py --verify-bundle` (`16/16`, 0 drift), `pytest -q -m ""` (`311 passed`, `0` failed, `0` skipped), `run_conformance_ci.py --with-postgres` (`0` violations), `spikes/web-import` offline `typecheck` (`0` errors) & `vitest` (`76/76` passed), `0` lingering `postgres` processes. |
| **6. Hosted Staging / External API Waiver Honesty** | **PASS** | **PASS** | Explicitly documented as `Deferred / Waived for current gate by owner decision (not executed)` across `CONTRACT.md`, `b004_evidence_bundle.v1.json`, and `B004_FAULT_HARNESS_REPORT.md`. Zero external network calls. |
| **7. Immutable Contract Preservation** | **PASS** | **PASS** | `git rev-parse HEAD:contracts/import-v1/openapi.yaml` = `07594d75425744d6149250a9f7ff95ac24659f80`. |

---

## 5. Per-Scenario Code & PG18 Execution Audit Table (`S01` ~ `S16`)

| ID | Requirements | Branch | Live PG18 Harness (`core_sha`) | Independent Final Verdict | Audit Evidence & Notes |
|---|---|---|---|---|---|
| `S01` | `SYNC-01/02/05/20`, `REF-04` | `trunk` | PASSED (`1da53839188b`) | **PASS** | 10-page PR + 3-page commit + reviews/comments + optional issue (`#501` fetched, `#999` 404 excluded) -> `SUCCESS`, 1 `IMPORT_READY`, 11 `work_events` (10 PR + 1 `DIRECT_COMMIT`), 26 Plan C metadata-only `content_units`. |
| `S02` | `SYNC-04/10/14` | `trunk` | PASSED (`1da53839188b`, `s01_equiv=True`) | **PASS** | HTTP 503 on PRS page 8/10 -> `PARTIAL`, `last_committed_page=7`, `retry_allowed=True`, 0 promotions. Resumes via `POST /v1/sync-runs/{id}/retry` with `0` resume re-fetches of pages 1..7. Exact `core_convergence_sha256` match with `S01`. |
| `S03` | `SYNC-07`, `REPLAY-01` | `trunk` | PASSED (`1da53839188b`, `s01_equiv=True`) | **PASS** | Real `InsidePageTxFaultInjector` fires inside `PostgresBackfillPageCommitter.commit_page_batch` at `after_checkpoint_update` before PG18 `COMMIT`. Full rollback (`last_committed_page=4`, PR `#106` count=0, page-5 obs=0), lease reclaim at `+120s`, exact `S01` convergence. |
| `S04` | `SYNC-08`, `REPLAY-02` | `trunk` | PASSED (`1da53839188b`, `s01_equiv=True`) | **PASS** | Crash fires via `on_after_page` strictly after PG18 page-6 `COMMIT` and before `heartbeat_and_checkpoint` ack. Preserves `last_committed_page=6`, discards stale version-5 recommit (`STALE_CHECKPOINT_VERSION`), 0 duplicates, exact `S01` convergence. |
| `S05` | `SYNC-09/10`, `REPLAY-04` | `main` | PASSED (`ab29d19a0161`) | **PASS** (`NB-1` fixed) | 2-thread `Barrier(2)` concurrent `create_initial_import` -> `1x 202` + `1x 409 SYNC_ALREADY_RUNNING` + idempotent replay; 2-thread `Barrier(2)` concurrent `commit_page_batch(expected_checkpoint_version=0)` -> `1x COMMITTED` + `1x STALE_CHECKPOINT_VERSION` (`0` duplicate writes). Dead variable/branch removed (`NB-1`). |
| `S06` | `SYNC-11` | `main` | PASSED (`c2128893defb`) | **PASS** | HTTP 200 `Content-Length` truncation and missing required PR fields both halt in `PARTIAL / MALFORMED_OR_PARTIAL_PAGE` (`retry_allowed=False`, 0 `IMPORT_READY`, 0 promotions). |
| `S07` | `SYNC-12` | `main` | PASSED (`ffd615fb7104`) | **PASS** | Self-loop `Link: rel="next"` on PRS page 2 (`page 2 -> page 2`) caught immediately by `PaginationSafeguard` -> `PARTIAL / CURSOR_CYCLE_DETECTED` (`retry_allowed=False`, `prs_page2_requests=1`, 0 loops, 0 promotions). |
| `S08` | `SYNC-13` | `main` | PASSED (`268bf77842da`) | **PASS** | Attempt 1 hits `429 Retry-After: 30` on PRS page 2 -> `PARTIAL / TRANSIENT_PROVIDER_ERROR` (`retry_allowed=True`); Attempt 2 resumes and hits timeout on commits page 2 -> `PARTIAL / TRANSIENT_PROVIDER_ERROR` (`retry_allowed=True`, safe checkpoint preserved, 0 promotions). |
| `S09` | `SYNC-16/17` | `main` | PASSED (`1599072a19d1`) | **PASS** | Sub-case A (PR `#202` `updated_at`/`head.sha` mutated before 2nd-pass enumeration) & Sub-case B (`default_branch -> 'release'`, `head_sha` mutated) both halt in `PARTIAL / PLAN_DRIFT` (`source_drift=True`, `retry_allowed=False`, `/retry` -> `409`, 0 promotions). Non-assumption of global review/comment mutation detection explicitly disclosed. |
| `S10` | `SYNC-18/19` | `main` | PASSED (`0e0ce1d494f4`) | **PASS** | Sub-case A: 404 on commit-PR lookup -> `UNKNOWN` -> `PARTIAL / INCOMPLETE_PR_ASSOCIATION`, 0 `DIRECT_COMMIT`, 0 `SUCCESS`. Sub-case B: clean `SUCCESS` promotes 1 `NO_MATCH_OBSERVED` `DIRECT_COMMIT` (`requires_user_confirmation=True`), excluding out-of-window PR `#7` and squash commit (`PR #202`). |
| `S11` | `SYNC-20` | `master` | PASSED (`f2b23be2b806`) | **PASS** | Exercises `default_branch='master'`; all PR-linked commits (`head_sha`, `merge_commit_sha`, squash) attach strictly under PR `MUST_LINK` memberships (`event_memberships_count=9`, 3 PR events, `0` `DIRECT_COMMIT` events). |
| `S12` | `SYNC-21` | `main` | PASSED (`f64ed27f33fe`) | **PASS** | Sub-cases A (`max_objects=2`), B (`max_requests=2`), and C (`max_total_bytes=500`) terminate cleanly in `PARTIAL_WITH_LIMIT / LIMIT_EXCEEDED` (`retry_allowed=False`), populating `processed_range` & `remaining_range` with `0` promotions. |
| `S13` | `SYNC-22`, `POLICY-01` | `main` | PASSED (`22dcc16e1338`) | **PASS** | Sub-case A (mid-run `PUBLIC -> PRIVATE` -> `POLICY_REVIEW_REQUIRED`, `CANCELED`, 0 tombstones, raw/AI/Export blocked), Sub-case B (`401` revocation -> `REVOKED`, secret deleted, 0 tombstones, AI/Export blocked), Sub-case C (policy API `503` -> fail-closed `PARTIAL` + `503 GITHUB_PROVIDER_UNAVAILABLE`, `import_ready=False`). |
| `S14` | `SYNC-04/14/16` | `main` | PASSED (`9266d20b3485`) | **PASS** | Sub-case A: transient 503 retry preserves `frozen_plan_fingerprint`, reaches `SUCCESS`, rejects post-`SUCCESS` retry & create with `409`. Sub-case B: `PLAN_DRIFT` rejects `/retry` with `409`, confirms no HTTP `/abandon` route (`404/405`), DB-abandoned run stays isolated while new plan finishes cleanly to `SUCCESS`. |
| `S15` | `REPLAY-01/02/04`, `REF-04/06` | `main` | PASSED (`2fea374f49fe`) | **PASS** | Pre-`IMPORT_READY` derived-unit call blocked (`RUN_NOT_IMPORT_READY`); duplicate `execute_sync_run`, `consume_outbox_event_once`, and `process_import_ready_run` strictly idempotent; refetch `MATCHED` vs `REVISION_MISMATCH` (`BLOCKED_SOURCE` while preserving `SUCCESS`, `import_ready=True` and blocking raw/approval/export gates). |
| `S16` | `SYNC-22`, `OD-003`, `REF-07e` | `main` | PASSED (`50093471575c`, snapshot SHA `44be6660bf15...`) | **PASS** (`BLOCKER-1`, `NB-2`, `NB-3` fixed) | **Part A**: 0ms post-`deletion-confirm` (before worker) blocks `GET /v1/content-units/{id}/content` (`403`, 0 canary), AI, Approval, and Export (`POLICY_BLOCKED`, 0 `INV-6` violations); post-worker projects `BLOCKED_POLICY`. **Part B**: pre-resume deletion fence cancels queued resume AND returns `404` on `POST /retry` (`0` resurrection). **Part C**: pre-resume `PRIVATE` flip halts resume (`0` tombstones, `0` promotions). **Part D**: live post-`SUCCESS` upstream `PRIVATE` flip immediately blocks viewer (`403 POLICY_BLOCKED`) & Approval/Export gates before local `verify_resource_policy`. |

---

## 6. Round 2 Independent Verification Command Log & Results

All commands were executed directly in `/Users/eddy81848/orca/workspaces/Career Memory/m2-t005d-independent-review` after merging `2a413a9d2b71602f5825f73ab4e534a6d27b842f` (with zero production code edits by the reviewer):

| Verification Check | Command / Method | Round 2 Result |
|---|---|---|
| Clean local merge | `git merge --no-edit 2a413a9d2b71602f5825f73ab4e534a6d27b842f` | `Merge made by the 'ort' strategy.` (6 files, 0 conflicts) |
| OpenAPI immutability | `git rev-parse HEAD:contracts/import-v1/openapi.yaml` | `07594d75425744d6149250a9f7ff95ac24659f80` (**MATCH**) |
| Redaction & raw-text grep | `grep -rnE` over `contracts/fault-harness/` and `docs/b004/` | `0` concrete canaries, `0` secrets, `0` raw text fields |
| Full B-004 matrix + bundle parity | `cd apps/api && uv run python ../../scripts/run_b004_fault_harness.py --verify-bundle` | `16/16 PASSED`, `0` drift vs `b004_evidence_bundle.v1.json`, `0` invariant violations, `0` canary leaks (`S01==S02==S03==S04` core SHA `1da53839188b`) |
| Full API pytest suite (incl. PG18 & `BLOCKER-1` regression) | `cd apps/api && uv run pytest -q -m ""` | **`311 passed in 31.31s`** (`0` failed, `0` skipped) |
| M1/M2 Conformance CI (PG18 fresh & staged upgrade) | `cd apps/api && uv run python ../../scripts/run_conformance_ci.py --with-postgres` | **`PASSED (static + postgres-18 (fresh & staged upgrade)): 0 drift/parity violations.`** |
| `spikes/web-import` offline typecheck & unit tests | `cd spikes/web-import && npm ci --offline && npm run typecheck && npm test` | **`0` TS errors; `3` test files / `76/76` tests passed** |
| Negative mutation tests (comparator + PG18 invariants/canaries) | Scratch script (`0` repo edits) | Comparator caught `3/3` diffs; PG18 caught `6/6` invariants (`INV-1`..`INV-6`) + `3/3` canary hits |
| Adversarial `S16` 0ms post-`deletion-confirm` probe | Scratch script (`0` repo edits) | **`BLOCKER-1` CLOSED**: `deletion_fence_active=True` (target, sibling anchors, `check_policy_fence_current`), `HTTP 403`, `raw_canary_exposed=False`, `APPROVAL/EXPORT allowed=False`, `0` invariant violations |
| Lingering `postgres` check | `ps aux \| grep -E "postgres\|postmaster" \| grep -v grep` | `0` processes |

---

## 7. Non-Blocking Observations Disposition

- **`NB-1` (`_run_s05` dead variable / dead branch)**: **RESOLVED** in `2a413a9d` (`runner.py:1434-1466`).
- **`NB-2` (`_run_s16` Part B `/retry` `404` symmetry after `deletion-confirm`)**: **RESOLVED** in `2a413a9d` (`runner.py:2759-2766`, `S16.json:129`).
- **`NB-3` (`_run_s16` Part D live post-`SUCCESS` `PRIVATE` flip immediate blocking)**: **RESOLVED** in `2a413a9d` (`runner.py:2825-2860`, `S16.json:134-136`).
- **`NB-4` (`_run_s05` Part A transport)**: Open non-blocking note — concurrent duplicate creation in `_run_s05` invokes `PostgresSyncRunService.create_initial_import` directly across 2 threads rather than through FastAPI `TestClient`; exercises the real PG18 `FOR UPDATE` + partial unique index path cleanly and does not block `B-004`.

---

## 8. Residual Risks & Hosting Waiver Boundary

- **Hosted Staging & Live External GitHub/LLM Verification**: Explicitly **Deferred / Waived for current gate by owner decision (not executed)**. Local PG18 verification does not cover Cloud Run runtime networking, live GitHub OAuth/REST rate-limit headers, or Supabase connection pooler (`PgBouncer`) transaction-pooling interactions with `SET LOCAL` RLS GUCs; those remain deferred to the hosted staging verification gate.
- **`S09` Completion Gate Scope**: As explicitly documented in `S09`, the completion gate detects default-branch/HEAD changes and 2nd-pass merged PR cue drift (`pr_number`, `head_sha`, `updated_at`, `merge_commit_sha`) only; global mutation detection across all PR review/comment threads is intentionally not assumed.
- **Final `B-004` Local PG18 Gate Release Decision**: **APPROVED (`VERDICT: PASS`)** — all 16 mandatory scenarios (`S01`–`S16`), `BLOCKER-1` zero-window `OD-003` deletion fence enforcement, `T-005C` comparator & negative mutation guarantees, Plan C redaction, M1/M2 conformance CI, and `spikes/web-import` checks pass cleanly.
