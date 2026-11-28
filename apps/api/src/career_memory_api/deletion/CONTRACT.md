# `career_memory_api.deletion` Contract (M5 T-502 / T-502A / OD-003 / DELETE-01..07)

## Purpose
Implements the authoritative OD-003 source deletion workflow:
- Live deletion impact preview (`POST /v1/source-resources/{id}/deletion-preview`)
- Atomic deletion confirmation & immediate zero-window fence activation (`POST /v1/source-resources/{id}/deletion-confirm`)
- Read-only deletion job status & component receipt endpoint (`GET /v1/deletion-jobs/{id}`)
- 5-component durable cleanup worker (`JOBS_AND_OUTBOX`, `EXPORTS`, `FTS_INDEX`, `EPHEMERAL_CACHE`, `AUTHORITATIVE_DOMAIN`) with partial failure isolation (`PARTIAL` -> `DELETION_PENDING`) and idempotent retry convergence
- Universal pre-commit deletion fence helpers (`assert_resources_not_deletion_fenced`, `assert_event_not_deletion_fenced`) preventing late worker resurrection (`DELETE-07`).

## Key Invariants
1. **Preview-to-Confirm Scope Binding (`DELETE-01`, `DELETE-02`)**:
   - Preview computes exact counts across all 10 categories plus deterministic `resource_version` and `impact_scope_version` signed into `preview_token`.
   - Confirm locks `source_resources FOR NO KEY UPDATE`, verifies 24h `api_idempotency_records`, deduplicates active `deletion_jobs` (`uq_deletion_jobs_single_active_per_resource`), and rejects expired or scope-drifted previews with `409` and **0 mutated rows**.
2. **Zero-Window Immediate Blocking (`DELETE-03`, `DELETE-04`)**:
   - Immediately inside the `202` `deletion-confirm` transaction: fence is persisted, queued jobs/runs are canceled, affected `exports` are transitioned to `DELETED` with `artifact_payload = NULL`, and in-process ephemeral cache entries are evicted.
3. **Partial Failure & Idempotent Retry (`DELETE-05`, `DELETE-06`)**:
   - Each of the 5 components commits its own receipt in `public.deletion_component_receipts`.
   - Failure in any component transitions `deletion_jobs.status = 'PARTIAL'` (`user_visible_state = 'DELETION_PENDING'`, `fence_active = True`), keeping search, resolver, approval, and exports 100% blocked.
   - Re-running `execute_deletion_job` executes ONLY non-`SUCCEEDED` components and converges to `SUCCEEDED` with 0 resurrected rows; independent `source_resources` and shared `source_accounts` remain 100% intact.
4. **Universal Pre-Commit No-Resurrection Fence (`DELETE-07`)**:
   - In-flight Sync/Import, Event Candidate, WorkEvent, Fact, Claim, Search Indexer, and Export workers re-check the deletion fence right before commit and fail closed with 0 resurrected rows.
