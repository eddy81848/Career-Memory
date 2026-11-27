# B-004 Initial Backfill Fault Harness Contract (`T-005A` → `T-005B` → `T-005C` + `T-005D` Evidence)

## 1. Non-Negotiable Architecture Rules (`ADR-017`, `T-005C`)
1. **Real Production Import Stack Only**: Drives `career_memory_api.initial_import.InitialBackfillOrchestrator`, `InitialBackfillJobWorker`, `DerivedUnitIngestionProcessor`, `PostgresBackfillPageCommitter`, `PostgresSyncRunService`, `PostgresJobRuntime`, `PostgresGitHubConnectService`, `HttpGitHubImportClient`, `RefetchResolverService`, and `contracts_api` over real PostgreSQL 18 migrations `0010`..`0050` with `NOBYPASSRLS` roles. Zero shadow/mock importer implementations.
2. **Strictly Read-Only Snapshot Comparator (`T-005C`)**: `compare_logical_snapshots` and `load_expected_snapshot` are strictly read-only. No `--update-snapshots` flag or runtime expected-snapshot overwrite mechanism exists.
3. **Zero External Network I/O**: `DeterministicFakeGitHubProvider` operates in-memory behind `SafeGitHubHttpTransport(wire_transport=InMemoryWireTransport(...))`. Hosted staging (Cloud Run / Supabase) and live GitHub OAuth/API verification are explicitly documented as **Deferred / Waived for current gate by owner decision (not executed)**.

## 2. Versioned Schemas (`T-005A`)
- Scenario Spec Schema: `SCENARIO_SCHEMA_VERSION = "cm-b004-scenario-v1"`
- Expected Logical Snapshot Schema: `EXPECTED_SNAPSHOT_SCHEMA_VERSION = "cm-b004-expected-v1"`
- Redacted Evidence Bundle Schema: `EVIDENCE_BUNDLE_SCHEMA_VERSION = "cm-b004-evidence-bundle-v1"`

## 3. Explicit Failure & Race Injection Points (`T-005B`)
- `BEFORE_PAGE_TX`
- `INSIDE_PAGE_TX_BEFORE_COMMIT`
- `AFTER_PAGE_COMMIT_BEFORE_ACK`
- `BEFORE_FINALIZE_SUCCESS`
- `AFTER_FINALIZE_BEFORE_DERIVED_UNITS`
- `CONCURRENT_REQUEST_AND_WORKER_RACE`

## 4. Six Global Invariants Verified (`T-005C`)
- `INV-1`: `sync_checkpoints.last_committed_page <= max(sync_run_page_observations.page_number)`
- `INV-2`: `0` `IMPORT_READY` outbox rows before `SUCCESS` (and `<= 1` per succeeded run)
- `INV-3`: `0` domain promotions (`work_events`, `content_units`, `facts`, `claims`) before `SUCCESS`
- `INV-4`: `0` duplicate concurrent `checkpoint_version` writes
- `INV-5`: `0` `DIRECT_COMMIT` candidates for `UNKNOWN`, `MATCHED_PR`, out-of-window PR, or squash/rebase/merge commits; every `NO_MATCH_OBSERVED` `DIRECT_COMMIT` has `requires_user_confirmation=True`
- `INV-6`: `0` unauthorized raw/secret exposures (`CANARY_B004_*` hit count == `0` across all `public.*` PostgreSQL tables, logs, non-ephemeral API responses, and reports; `content_units` raw-null Plan C compliant; blocked/deleted/private resources block raw viewer, AI, and Export gates)
