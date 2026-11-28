# `career_memory_api.search` Contract (M5 T-501 / T-501A / OD-008 / SEARCH-01..05)

## Purpose
Provides permission-safe PostgreSQL 18 Full-Text Search over non-raw derived Career Memory
entities (`WORK_EVENT`, `FACT`, `CLAIM`) with mandatory authoritative pre-response rechecks,
zero-window deletion/policy fence enforcement, versioned opaque cursors, and metadata-only
`public.search_runs` audit logging.

## Key Invariants
1. **Plan C Non-Raw Projection (`public.search_entries`)**:
   - Only non-raw `WorkEvent` summaries/identifiers, `Fact` subject/predicate/object text, and
     `Claim` text/atoms are projected. Raw source bodies, diffs, quotes, and secrets are never
     stored or indexed.
   - `public.search_runs` records `query_hash` (`sha256:...`), `index_version`, `ranking_version`,
     `candidate_limit`, `page_limit`, `cursor_offset`, `raw_candidate_count`, `lag_filtered_count`,
     `returned_count`, and `degraded_mode` with **zero raw query text**.
2. **`OD-008` Workspace + Optional `source_resource_id` Scope**:
   - `project_id` is globally rejected (`422 PROHIBITED_PROJECT_ID_SCOPE`).
   - Explicit `source_resource_id` is verified against `public.source_resources` inside
     `workspace_scoped_transaction`; nonexistent and cross-workspace IDs return identical
     `404 RESOURCE_NOT_FOUND`.
   - When `source_resource_id` is omitted and exactly 1 active, public, unfenced repository exists
     in the workspace, the server auto-selects it in `SearchResponse.scope.source_resource_id`.
3. **Authoritative Pre-Response Recheck (`T-501A`, `SEARCH-01`, `SEARCH-02`, `SEARCH-05`)**:
   - Never trusts `search_entries` status/version. Every FTS candidate is re-verified against
     authoritative tables (`work_events` + `collapse_event_ids_for_search_and_export`, `facts` +
     `fact_support` + `identities`, `claims` + live `VerificationCalculator`, `source_resources`,
     `source_accounts`, and `deletion_jobs` fence).
   - Stale, rejected, superseded, policy-blocked, revoked, or deletion-fenced entries are strictly
     excluded and counted in `lag_filtered_count`.
4. **Versioned Cursor & Degraded Rebuild Mode (`SEARCH-05`)**:
   - Cursors are HMAC-bound to `(workspace_id, source_resource_id, query_hash, index_version,
     ranking_version)` and reject any drift with `400 INVALID_SEARCH_CURSOR`.
   - During index rebuild / version mismatch or `force_degraded_mode=True`, search falls back to
     authoritative non-raw table scans (`degraded_mode=True`) without failing.
