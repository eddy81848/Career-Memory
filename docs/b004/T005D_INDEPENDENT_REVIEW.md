# T-005D Independent Adversarial Review Report (`B-004` Initial Import 16-Scenario Fault Matrix)

```text
VERDICT: FAIL_WITH_BLOCKERS
B-004 GATE RELEASE (LOCAL PG18): BLOCKED (1 BLOCKER — S16 / PostgresProvenanceMetadataStore deletion fence window)
```

## 1. Audit Metadata & Scope

| Field | Value |
|---|---|
| **Track / Task ID** | `M2-T005D-REVIEW` (`T-005D — Execute initial import matrix + B-004 review`) |
| **Reviewer Role** | Independent Adversarial Reviewer (strictly separated from `M2-B004-HARNESS`) |
| **Reviewed Branch & Commit** | `eddy81848/m2-b004-fault-harness` @ `69a15529c9ccb434369574af3812637222109e86` |
| **Immutable OpenAPI Contract (`contracts/import-v1/openapi.yaml`)** | `07594d75425744d6149250a9f7ff95ac24659f80` (**VERIFIED UNCHANGED**) |
| **Execution Environment** | Local ephemeral PostgreSQL 18 (`migrations 0010..0050`, `NOBYPASSRLS` roles `cm_api_app` & `cm_worker_app`), offline `DeterministicFakeGitHubProvider` (0 external network calls) |
| **Hosted Staging / Live GitHub / Live LLM** | **Deferred / Waived for current gate by owner decision (not executed)** — verified honestly disclosed without false pass claims |

---

## 2. Executive Verdict & Blocker Summary

While **15 of 16 scenarios (`S01`–`S15`)**, the **`ADR-017` real production stack architecture**, the **`T-005C` strictly read-only comparator**, the **6 global invariant checkers**, and **Plan C (`REF-04/06`) redaction** are well-engineered and pass both code audit and live local PostgreSQL 18 execution, **independent adversarial audit uncovered 1 concrete `BLOCKER` in `S16` (production `OD-003` deletion fence enforcement in `PostgresProvenanceMetadataStore` + harness masking in `_run_s16`)**.

Under Notion `B-004` release rules (*"모든 필수 시나리오가 통과한 뒤에만 B-004를 Resolved로 바꾼다"*), this review issues **`VERDICT: FAIL_WITH_BLOCKERS`** and **blocks marking `B-004` as `Resolved`** until `BLOCKER-1` is fixed in production code, verified immediately after `deletion-confirm` (before any background job runs) in `S16`, and re-audited.

### BLOCKER-1: Zero-Window `OD-003` Deletion Fence Bypass in `PostgresProvenanceMetadataStore` Masked by `S16` Harness (`SYNC-22`, `OD-003`, `REF-07e`, `INV-6`)

- **Severity**: **BLOCKER** (Production security/policy-fence defect + false-green masking in `S16`)
- **Affected Files & Lines**:
  1. `apps/api/src/career_memory_api/resolver/postgres_store.py` (`_row_to_target` L30–L71, `_SELECT_TARGET_COLUMNS` L74–L124, `get_unit_resolution_target` L138–L164, `check_policy_fence_current` L193–L198)
  2. `apps/api/src/career_memory_api/jobs/postgres_runtime.py` (`request_source_deletion` L1052–L1152)
  3. `apps/api/src/career_memory_api/fault_harness/runner.py` (`_run_s16` L2677–L2706, L2818–L2834)
  4. `docs/b004/B004_FAULT_HARNESS_REPORT.md` (Section 4 item 2, L74–L75 — acknowledged the exposure window as an M5 `T-502` footnote instead of closing it)

#### Exact Technical Root Cause
1. `PersistedUnitResolutionTarget` (`apps/api/src/career_memory_api/resolver/models.py:325`) defines `deletion_fence_active: bool = False`, and `evaluate_target_policy` (`apps/api/src/career_memory_api/resolver/policy.py:217-222`) explicitly enforces:
   ```python
   if (
       target.deletion_fence_active
       or target.resource_status in ("DELETED", "DELETION_PENDING")
       or target.object_lifecycle_state == "DELETED"
   ):
       return False, "RESOURCE_DELETED_OR_FENCED"
   ```
2. However, `PostgresProvenanceMetadataStore._SELECT_TARGET_COLUMNS` (`resolver/postgres_store.py:74-124`) **never queries `public.deletion_jobs`**, and `_row_to_target` (`resolver/postgres_store.py:30-71`) never passes `deletion_fence_active`. Every target loaded from PostgreSQL 18 — including inside the `REF-07e` pre-commit TOCTOU guard `check_policy_fence_current()` — permanently receives `deletion_fence_active=False`.
   - Notably, `cm_api_app` already has `SELECT` + RLS access to `public.deletion_jobs` (used by `PostgresSyncRunService` and `PostgresGitHubConnectService` via `is_deletion_fence_active`), so there is no privilege barrier.
3. When `POST /v1/source-resources/{id}/deletion-confirm` returns `202 Accepted` via `PostgresJobRuntime.request_source_deletion()` (`jobs/postgres_runtime.py:1052-1152`), PostgreSQL inserts `public.deletion_jobs(status='PENDING')` while leaving `source_resources.status = 'ACTIVE'` and `content_units.policy_state = 'ACTIVE'`.
4. **How `S16` Masked the Defect**: In `runner.py:2666-2706`, `step_a` had already completed `SUCCESS` and run `DerivedUnitIngestionProcessor` (`processing_status=COMPLETE`). After `POST /v1/source-resources/{id}/deletion-confirm` (`202`), `_run_s16` manually invoked `stack_a.derived_processor.process_import_ready_run(...)` (L2699–L2703) **before** calling `_check_policy_gates` (L2818). That manual background-worker call triggered the `UPDATE public.content_units SET policy_state = 'BLOCKED'` added to `derived_units.py:244-259`, hiding the live exposure window between `deletion-confirm` and background execution.

#### Live PostgreSQL 18 Adversarial Reproduction Output
Executing the exact `S16` Part A sequence up to `POST /v1/source-resources/{id}/deletion-confirm` (`202`) and invoking `GET /v1/content-units/{id}/content`, `RefetchResolverService` gates, and `verify_b004_invariants` **immediately before** any background `DerivedUnitIngestionProcessor` re-run produced:

```text
POST /deletion-confirm -> HTTP 202, deletion_job_id=b6c10b9f-6a6a-46f7-b450-afdebc0bb7d4
PostgresProvenanceMetadataStore target immediately after deletion-confirm:
  deletion_fence_active=False, resource_status=ACTIVE, unit_policy_state=ACTIVE
Immediate GET /v1/content-units/<id>/content -> HTTP 200, raw_canary_exposed=True
Immediate Resolver APPROVAL gate -> allowed=True, status=MATCHED
Immediate Resolver EXPORT_DOWNLOAD gate -> allowed=True, status=MATCHED
Immediate runner._check_policy_gates ->
  {'refetch_content_allowed': True, 'refetch_resolution_status': 'MATCHED',
   'external_ai_gate_allowed': False, 'export_gate_allowed': False,
   'claim_approval_gate_allowed': True, 'concurrent_stale_fence_discarded': 0}
Immediate verify_b004_invariants violations (1):
  - [INV-6] Gate allowed raw/AI/Export exposure despite blocked resource policy
    (acct=ACTIVE, res=ACTIVE, vis=PUBLIC, del_fence=True).
```

#### Required Remediation (Minimal Scope to Clear `BLOCKER-1`)
1. **`apps/api/src/career_memory_api/resolver/postgres_store.py`**: Extend `_SELECT_TARGET_COLUMNS` with a live `EXISTS (SELECT 1 FROM public.deletion_jobs dj WHERE dj.workspace_id = res.workspace_id AND dj.source_resource_id = res.id AND dj.status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED'))` column and wire it into `PersistedUnitResolutionTarget(..., deletion_fence_active=bool(row[35]))` in `_row_to_target`. This immediately closes the zero-window gap for `GET /v1/content-units/{id}/content`, `APPROVAL` gate, `EXPORT_DOWNLOAD` gate, and `REF-07e` `check_policy_fence_current` TOCTOU checks.
2. **`apps/api/src/career_memory_api/fault_harness/runner.py` (`_run_s16`)**: Assert `_check_policy_gates` (including HTTP `GET /v1/content-units/{id}/content` == `403`, `claim_approval_gate_allowed is False`, `export_gate_allowed is False`, `external_ai_gate_allowed is False`) **immediately after `POST /deletion-confirm` returns `202` and BEFORE calling `derived_processor.process_import_ready_run()`**, and run `verify_b004_invariants` at that intermediate pre-worker boundary as well.
3. Add a dedicated unit/PG18 regression test in `apps/api/tests/resolver/` or `apps/api/tests/fault_harness/` proving zero-window rejection immediately after `request_source_deletion`.

---

## 3. 7-Axis Independent Audit Summary

| Audit Axis | Status | Key Findings |
|---|---|---|
| **1. `ADR-017` Real Production Path Authenticity** | **PASS** | All 16 scenarios invoke real M2 production classes (`InitialBackfillOrchestrator`, `InitialBackfillJobWorker`, `DerivedUnitIngestionProcessor`, `PostgresBackfillPageCommitter`, `PostgresSyncRunService`, `PostgresJobRuntime`, `PostgresGitHubConnectService`, `HttpGitHubImportClient`, `SafeGitHubHttpTransport`, `RefetchResolverService`, `contracts_api`) against real PG18 migrations `0010`..`0050` with `NOBYPASSRLS` roles. Zero shadow/mock importer implementations. |
| **2. 16-Scenario Matrix & Requirements 1:1 Audit** | **FAIL (`15/16` PASS, `S16` BLOCKER)** | `S01`–`S15` verified line-by-line and empirically on PG18. `S16` Part A masks `BLOCKER-1` (post-`deletion-confirm` immediate raw viewer & resolver gate bypass). |
| **3. `T-005C` Comparator & Negative Mutation Tests** | **PASS** | Strictly read-only comparator; zero `--update-snapshots` / write flags in `fault_harness/` and `scripts/run_b004_fault_harness.py`. In-memory comparator mutations caught `3/3` field diffs; PG18 negative mutations triggered all 6 invariants (`INV-1`..`INV-6`) and `3/3` canary hits (`pg`, `log`, `api_response`). |
| **4. Plan C (`REF-04/06`) & Redaction Audit** | **PASS** | `0` real secrets, `0` concrete canary markers, and `0` raw PR/commit/review text bodies across `contracts/fault-harness/expected_snapshots/S01.json`..`S16.json`, `contracts/fault-harness/b004_evidence_bundle.v1.json`, and `docs/b004/B004_FAULT_HARNESS_REPORT.md`. Schema versions (`cm-b004-scenario-v1`, `cm-b004-expected-v1`, `cm-b004-evidence-bundle-v1`) enforced. |
| **5. Independent Local PG18 Re-Execution** | **PASS** | `run_b004_fault_harness.py --verify-bundle` (`16/16`, 0 drift), `run_conformance_ci.py --with-postgres` (`0` drift/parity violations), `pytest -q -m ""` (`310 passed`), `0` lingering `postgres` processes. |
| **6. Hosted Staging / External API Waiver Honesty** | **PASS** | Explicitly documented as `Deferred / Waived for current gate by owner decision (not executed)` across `CONTRACT.md`, `b004_evidence_bundle.v1.json`, and `B004_FAULT_HARNESS_REPORT.md`. Zero external network calls. |
| **7. Immutable Contract Preservation** | **PASS** | `git rev-parse HEAD:contracts/import-v1/openapi.yaml` = `07594d75425744d6149250a9f7ff95ac24659f80`. |

---

## 4. Per-Scenario Code & PG18 Execution Audit Table (`S01` ~ `S16`)

| ID | Requirements | Branch | Live PG18 Harness | Independent Code & Adversarial Verdict | Audit Evidence & Notes |
|---|---|---|---|---|---|
| `S01` | `SYNC-01/02/05/20`, `REF-04` | `trunk` | PASSED (`1da53839188b`) | **PASS** | 10-page PR + 3-page commit + reviews/comments + optional issue (`#501` fetched, `#999` 404 excluded) -> `SUCCESS`, 1 `IMPORT_READY`, 11 `work_events` (10 PR + 1 `DIRECT_COMMIT`), 26 Plan C metadata-only `content_units`. |
| `S02` | `SYNC-04/10/14` | `trunk` | PASSED (`1da53839188b`, `s01_equiv=True`) | **PASS** | HTTP 503 on PRS page 8/10 -> `PARTIAL`, `last_committed_page=7`, `retry_allowed=True`, 0 promotions. Resumes via `POST /v1/sync-runs/{id}/retry` with `0` resume re-fetches of pages 1..7 (verified `request_counts == 2` = 1 initial + 1 2nd-pass completion gate). Exact `core_convergence_sha256` match with `S01`. |
| `S03` | `SYNC-07`, `REPLAY-01` | `trunk` | PASSED (`1da53839188b`, `s01_equiv=True`) | **PASS** | Real `InsidePageTxFaultInjector` fires inside `PostgresBackfillPageCommitter.commit_page_batch` at `after_checkpoint_update` (`postgres_page_committer.py:719`) before PG18 `COMMIT`. Verified full rollback (`last_committed_page=4`, PR `#106` count=0, page-5 observation=0), lease expiry reclaim at `+120s`, and exact `S01` convergence. |
| `S04` | `SYNC-08`, `REPLAY-02` | `trunk` | PASSED (`1da53839188b`, `s01_equiv=True`) | **PASS** | Crash fires via `on_after_page` (`orchestrator.py:670`) strictly after PG18 page-6 `COMMIT` and before `heartbeat_and_checkpoint` ack (`orchestrator.py:672`). Verified `last_committed_page=6` preserved, stale version-5 recommit discarded (`STALE_CHECKPOINT_VERSION`), lease reclaim at `+120s`, 0 duplicates, and exact `S01` convergence. |
| `S05` | `SYNC-09/10`, `REPLAY-04` | `main` | PASSED (`ab29d19a0161`) | **PASS** (see `NB-1`, `NB-4`) | 2-thread `Barrier(2)` concurrent `create_initial_import` -> `1x 202` + `1x 409 SYNC_ALREADY_RUNNING` + idempotent replay; 2-thread `Barrier(2)` concurrent `PostgresBackfillPageCommitter.commit_page_batch(expected_checkpoint_version=0)` -> `1x COMMITTED` + `1x STALE_CHECKPOINT_VERSION` (`0` duplicate writes). |
| `S06` | `SYNC-11` | `main` | PASSED (`c2128893defb`) | **PASS** | Both HTTP 200 `Content-Length` truncation (`SafeGitHubHttpTransport:551`) and missing required PR fields (`_build_pr_cue:311`) halt in `PARTIAL / MALFORMED_OR_PARTIAL_PAGE` (`retry_allowed=False`, 0 `IMPORT_READY`, 0 promotions). |
| `S07` | `SYNC-12` | `main` | PASSED (`ffd615fb7104`) | **PASS** | Self-loop `Link: rel="next"` on PRS page 2 (`page 2 -> page 2`) caught immediately by `PaginationSafeguard` (`pagination_guard.py:438`) -> `PARTIAL / CURSOR_CYCLE_DETECTED` (`retry_allowed=False`, `prs_page2_requests=1`, 0 loops, 0 promotions). |
| `S08` | `SYNC-13` | `main` | PASSED (`268bf77842da`) | **PASS** | Attempt 1 hits `429 Retry-After: 30` on PRS page 2 -> `PARTIAL / TRANSIENT_PROVIDER_ERROR` (`retry_allowed=True`, safe checkpoint); Attempt 2 resumes and hits timeout on commits page 2 -> `PARTIAL / TRANSIENT_PROVIDER_ERROR` (`retry_allowed=True`, safe checkpoint preserved, 0 promotions). |
| `S09` | `SYNC-16/17` | `main` | PASSED (`1599072a19d1`) | **PASS** | Sub-case A (PR `#202` `updated_at` & `head.sha` mutated before 2nd-pass enumeration) & Sub-case B (`default_branch -> 'release'`, `head_sha` mutated) both halt in `PARTIAL / PLAN_DRIFT` (`source_drift=True`, `retry_allowed=False`, `/retry` -> `409`, 0 promotions). Non-assumption of global review/comment mutation detection explicitly disclosed in spec, snapshot, bundle, and report. |
| `S10` | `SYNC-18/19` | `main` | PASSED (`0e0ce1d494f4`) | **PASS** | Sub-case A: 404 on commit-PR lookup -> `UNKNOWN` -> `PARTIAL / INCOMPLETE_PR_ASSOCIATION`, 0 `DIRECT_COMMIT`, 0 `SUCCESS`. Sub-case B: clean `SUCCESS` promotes 1 `NO_MATCH_OBSERVED` `DIRECT_COMMIT` (`requires_user_confirmation=True`), while out-of-window PR `#7` (`OUT_OF_WINDOW_PR_LINKED`) and squash commit (`PR #202`) produce `0` `DIRECT_COMMIT` candidates. |
| `S11` | `SYNC-20` | `master` | PASSED (`f2b23be2b806`) | **PASS** | Exercises `default_branch='master'`; all PR-linked commits (`head_sha`, `merge_commit_sha`, squash) attach strictly under PR `MUST_LINK` memberships (`event_memberships_count=9`, 3 PR events, `0` `DIRECT_COMMIT` events). |
| `S12` | `SYNC-21` | `main` | PASSED (`f64ed27f33fe`) | **PASS** | Sub-cases A (`max_objects=2`), B (`max_requests=2`), and C (`max_total_bytes=500`) all terminate cleanly in `PARTIAL_WITH_LIMIT / LIMIT_EXCEEDED` (`retry_allowed=False`), populating `processed_range` & `remaining_range` with `0` promotions. |
| `S13` | `SYNC-22`, `POLICY-01` | `main` | PASSED (`22dcc16e1338`) | **PASS** | Sub-case A (mid-run `PUBLIC -> PRIVATE` -> `POLICY_REVIEW_REQUIRED`, `CANCELED`, 0 tombstones, raw/AI/Export blocked), Sub-case B (`401` revocation -> `REVOKED`, secret deleted, 0 tombstones, AI/Export blocked), Sub-case C (policy API `503` -> fail-closed `PARTIAL` + `503 GITHUB_PROVIDER_UNAVAILABLE`, `import_ready=False`). |
| `S14` | `SYNC-04/14/16` | `main` | PASSED (`9266d20b3485`) | **PASS** | Sub-case A: transient 503 retry preserves `frozen_plan_fingerprint`, reaches `SUCCESS`, rejects post-`SUCCESS` retry & create with `409`. Sub-case B: `PLAN_DRIFT` rejects `/retry` with `409`, confirms no HTTP `/abandon` route (`404/405`), DB-abandoned run stays isolated (`import_ready=False`, `0` `IMPORT_READY` outbox) while new plan finishes cleanly to `SUCCESS`. |
| `S15` | `REPLAY-01/02/04`, `REF-04/06` | `main` | PASSED (`2fea374f49fe`) | **PASS** | Pre-`IMPORT_READY` derived-unit call blocked (`RUN_NOT_IMPORT_READY`); duplicate `execute_sync_run` (0 extra fetches), duplicate `consume_outbox_event_once` (1st `True`, 2nd `False`), duplicate `process_import_ready_run` (`0` duplicate units); refetch `MATCHED` -> allowed vs mutated PR body `REVISION_MISMATCH` -> `processing_status=BLOCKED_SOURCE` while preserving `status='SUCCESS'`, `import_ready=True` and blocking raw viewer, approval, and export gates. |
| `S16` | `SYNC-22`, `OD-003` | `main` | PASSED (`50093471575c` — **masked**) | **FAIL (`BLOCKER-1`)** | Part B (pre-resume deletion fence cancels queued resume, 0 resurrection) & Part C (pre-resume `PRIVATE` flip halts resume, 0 tombstones, 0 promotions) pass. **Part A fails independent adversarial check**: immediately after `POST /deletion-confirm` (`202`) and before any background worker runs, `PostgresProvenanceMetadataStore` returns `deletion_fence_active=False`, exposing raw PR body + canary on `GET /v1/content-units/{id}/content` (`200 OK`) and returning `allowed=True` on resolver `APPROVAL` & `EXPORT_DOWNLOAD` gates (`INV-6` violation). |

---

## 5. Independent Verification Command Log & Results

All verification commands were executed directly in `/Users/eddy81848/orca/workspaces/Career Memory/m2-t005d-independent-review` against local PostgreSQL 18 with zero worktree code modifications:

| Verification Check | Command / Method | Result |
|---|---|---|
| OpenAPI immutability | `git rev-parse HEAD:contracts/import-v1/openapi.yaml` | `07594d75425744d6149250a9f7ff95ac24659f80` (MATCH) |
| Redaction & raw-text grep | `grep -rnE` over `contracts/fault-harness/` and `docs/b004/` | `0` concrete canaries, `0` secrets, `0` raw text bodies |
| Read-only snapshot enforcement | `grep -rnE` over `fault_harness/` & `scripts/run_b004_fault_harness.py` | `0` write/auto-update code paths |
| Full B-004 matrix + bundle parity | `cd apps/api && uv run python ../../scripts/run_b004_fault_harness.py --verify-bundle` | `16/16 PASSED`, `0` drift vs `b004_evidence_bundle.v1.json` (note `S16` masking per `BLOCKER-1`) |
| M1/M2 Conformance CI (PG18) | `cd apps/api && uv run python ../../scripts/run_conformance_ci.py --with-postgres` | `PASSED (static + postgres-18 (fresh & staged upgrade)): 0 drift/parity violations.` |
| Full pytest suite (including `@pytest.mark.postgres`) | `cd apps/api && uv run pytest -q -m ""` | `310 passed in 31.86s` |
| Negative mutation tests (comparator + PG18 invariants/canaries) | In-memory & ephemeral PG18 scratch script (0 repo edits) | Comparator caught `3/3` diffs; PG18 caught `INV-1`, `INV-2`, `INV-3`, `INV-4`, `INV-5`, `INV-6` + `3/3` canary hits |
| Adversarial `S16` immediate post-`deletion-confirm` probe | Ephemeral PG18 scratch script (0 repo edits) | **REPRODUCED `BLOCKER-1`**: `HTTP 200`, `raw_canary_exposed=True`, `APPROVAL/EXPORT allowed=True`, `INV-6` violation |
| Lingering `postgres` check | `ps aux \| grep -E "postgres\|postmaster" \| grep -v grep` | `0` processes |

---

## 6. Non-Blocking Observations (Recommended Cleanup During Blocker Fix)

- **`NB-1` (`apps/api/src/career_memory_api/fault_harness/runner.py:1438, 1463-1472`)**: `_run_s05` contains an unused variable `winning_key` (L1438) and a dead branch `if False else None` for `token_secret_ref` (L1466–L1470). Replace with the resolved `token_secret_ref` and remove the dead variable.
- **`NB-2` (`_run_s16` Part B symmetry)**: In addition to testing `deletion-confirm` after `POST /retry`, consider asserting that calling `POST /v1/sync-runs/{id}/retry` *after* `deletion-confirm` on a `PARTIAL` run immediately returns `404 RESOURCE_NOT_FOUND` (already implemented in `PostgresSyncRunService.retry_sync_run:696-699`).
- **`NB-3` (`_run_s16` Part A post-`SUCCESS` `PRIVATE` symmetry)**: Consider also verifying immediate `GET /v1/content-units/{id}/content` blocking (`403 POLICY_BLOCKED`) after a post-`SUCCESS` `PUBLIC -> PRIVATE` flip before `verify_resource_policy` runs.
- **`NB-4` (`_run_s05` Part A transport)**: Concurrent duplicate creation in `_run_s05` exercises `PostgresSyncRunService.create_initial_import` directly across 2 threads rather than through FastAPI `TestClient`; this exercises the real PG18 `FOR UPDATE` + partial unique index path and is acceptable, noted here for completeness.

---

## 7. Residual Risks & Hosting Waiver Boundary

- **Hosted Staging & Live External GitHub/LLM Verification**: Explicitly **Deferred / Waived for current gate by owner decision (not executed)**. Local PG18 verification does not cover Cloud Run runtime networking, live GitHub OAuth/REST rate-limit headers, or Supabase connection pooler (`PgBouncer`) transaction-pooling interactions with `SET LOCAL` RLS GUCs; those remain deferred to the hosted staging verification gate.
- **`S09` Completion Gate Scope**: As explicitly documented in `S09`, the completion gate detects default-branch/HEAD changes and 2nd-pass merged PR cue drift (`pr_number`, `head_sha`, `updated_at`, `merge_commit_sha`) only; global mutation detection across all PR review/comment threads is intentionally not assumed.
- **B-004 Release Gate Decision**: **HOLD (`NOT RESOLVED`)** until `BLOCKER-1` is resolved in `PostgresProvenanceMetadataStore` and verified immediately after `deletion-confirm` (pre-background-job) in `S16`, followed by independent re-verification.
