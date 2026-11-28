# Cross-Subsystem Fault & Race Matrix Contract (`T-504` / `T-504A`)

## Scope
`career_memory_api.fault_matrix` provides a deterministic, real PostgreSQL 18 (`0010..0080`, `cm_migrator`, `cm_api_app`, `cm_worker_app` all `NOBYPASSRLS` + `FORCE RLS`) cross-subsystem fault injection and concurrency race verification harness spanning:

`Sync -> Evidence -> WorkEvent -> Fact -> Claim -> Search -> Export -> Deletion -> Quota/Circuit-Breaker -> Edge Guard -> Backup/Restore`

## Hard Invariants
1. **Zero `time.sleep` Race Timing**: All concurrency interleavings use `threading.Barrier` and explicit production service hooks (`before_lock_hook`, `before_publish_hook`, `before_commit_hook`, `fault_injector`).
2. **Zero External Network Calls (`₩0` spend)**: Runs 100% locally against an ephemeral PostgreSQL 18 cluster and deterministic offline adapters/fetchers.
3. **Full 46-Table Plan C & Secret Audit**: Every scenario verifies 0 raw source persistence (`content_units.text_content IS NULL`, `content_ref IS NULL`, `fact_support.extracted_span IS NULL`), 0 forbidden JSONB raw/secret keys, and 0 canary/XSS/secret leaks in DB, logs, exports, and evidence outputs.
4. **Zero-Drift Reproducibility**: `scripts/run_t504a_fault_matrix.py --verify` reproduces `contracts/fault-matrix/t504a_evidence_bundle.v1.json` with 0 drift and verifies `contracts/import-v1/openapi.yaml` SHA `07594d75425744d6149250a9f7ff95ac24659f80`.

## Scenarios (`XFLT-01` .. `XFLT-12`)
- `XFLT-01`: `REL-02` Poison Job & Outbox Terminal Isolation + Safe Operator Replay (`uq_jobs_active_or_succeeded_logical_effect`).
- `XFLT-02`: `CLAIM-09` Approval vs Concurrent Identity Revocation & Source `PRIVATE` Policy Barrier Race.
- `XFLT-03`: `EXPORT-02` Export Build/Download vs Concurrent `ER-012` Fact Edit & Provider `REF-03` Revision SHA Drift Barrier Race.
- `XFLT-04`: `DELETE-07` End-to-End Deletion Fence vs 4 Concurrent Workers (Derived Units + Claim Draft + Search Indexer + Export Publish) synchronized via 5-party `threading.Barrier`.
- `XFLT-05`: `DELETE-05` & `DELETE-06` Partial Component Failure (`FTS_INDEX` -> `AUTHORITATIVE_DOMAIN`) -> Degraded Block -> Idempotent Convergence preserving independent sibling Repo B.
- `XFLT-06`: `SEARCH-05` FTS Index Lag + Stale/Cross-Repo/Cross-Workspace Cursor Rejection + `OD-008` `project_id` Block under Live Mutations.
- `XFLT-07`: `SEC-01` & `SEC-02` Hostile Prompt-Injection, XSS Payload, Oversized/Binary Input, SSRF/Redirect Block (`REF-05`), Read-Only Verification Guard & Cross-Tenant Isolation.
- `XFLT-08`: `INF-03` & `INF-06` Provider Circuit Breaker + Ambiguous Billable Timeout (`auto_retry_allowed=False`) + `95%` Quota Cutoff Preserving `disconnect` & `OD-003` Deletion Rights.
- `XFLT-09`: `INF-07` Edge Origin Bypass (`*.run.app`), Private `/internal/worker/**` Isolation, Session Cache Isolation & Manual Cost Pause/Resume (`budget_verified=False` rejected).
- `XFLT-10`: `REF-01..08` Cross-Gate Refetch Matrix (`VIEW`, `APPROVAL`, `EXPORT_DOWNLOAD`) across `MATCHED`, `REVISION_MISMATCH`, `UNAVAILABLE`, `POLICY_BLOCKED`, and Budget Exhaustion.
- `XFLT-11`: `INF-10` / `REL-01` Encrypted Backup -> Isolated Restore -> Active Deletion Fence Survival & Replay Safety.
- `XFLT-12`: Clean Baseline vs Multi-Subsystem Fault-Injected Recovery Canonical Snapshot Convergence (exact SHA-256 hash equality).
