# M2-T203 Backfill Engine Contract (`T-203` + `T-203A` + `T-203B`)

## 1. Migration `db/migrations/0050_m2_backfill_observations.sql`

Loaded via `apply_backfill_engine_migration(conn)` on top of M1 `0010 -> 0020 -> 0030 -> 0040` without modifying any M1 migration or catalog snapshot. All 4 tables are owned by `cm_migrator`, enforce `ENABLE + FORCE ROW LEVEL SECURITY`, Composite FKs on `(workspace_id, parent_id)`, Plan C JSONB raw-key blocking (`cm_jsonb_contains_forbidden_raw_key`), and least-privilege `cm_worker_app` grants (no `DELETE`).

| Table | Purpose | Key Constraints / Unique Indexes |
| --- | --- | --- |
| `public.sync_item_outcomes` | Per-item skips (`BINARY_FILE`, `OVERSIZED_FILE`, `GENERATED_FILE`), exclusions (`OUT_OF_WINDOW_PR_LINKED`, `OPTIONAL_CONTEXT_UNAVAILABLE`, `LIMIT_EXCEEDED`), failures, and `ASSOCIATION_UNKNOWN` records | `UNIQUE (sync_run_id, stream_name, object_type, provider_object_id, outcome_kind, reason_code)`, Composite FKs to `sync_runs`, `source_resources`, `source_objects` |
| `public.sync_commit_pr_associations` | 3-state (`MATCHED_PR`, `NO_MATCH_OBSERVED`, `UNKNOWN`) completeness ledger for default-branch commits | `UNIQUE (sync_run_id, commit_source_object_id)`, strict state-field `CHECK` invariants (`NO_MATCH_OBSERVED` requires `lookup_completed=true` and `reachable_at_start/end=true`; `UNKNOWN` requires non-empty `unknown_reason_code`) |
| `public.sync_run_page_observations` | Committed page cursor & SHA-256 fingerprint trail enforcing `checkpoint <= committed page` | `UNIQUE (sync_run_id, stream_name, page_number)` |
| `public.sync_run_event_promotions` | Idempotent 1-time mapping from promoted `(source_object_id, anchor_mode)` to `work_events.id` | `UNIQUE (workspace_id, source_object_id, anchor_mode)` & `UNIQUE (sync_run_id, source_object_id, anchor_mode)` |

## 2. Core Components & Signatures

### `PaginationSafeguard` (`T-203A`)
- `PaginationSafeguard(limits: BackfillLimits | None = None, ...)`
- `evaluate_page(envelope: ProviderPageEnvelope) -> PageValidationDecision`
- `export_stream_cursor_state(stream_name: BackfillStreamName) -> dict[str, Any]`
- **Invariants**:
  - Immediate `A -> A` and multi-step `A -> B -> A` cursor cycles plus identical-page loops -> `PARTIAL` / `CURSOR_CYCLE_DETECTED` (`retry_allowed=False`), no checkpoint progression.
  - HTTP 200 missing identifiers/commit_sha/content_hash/timestamp, `declared_item_count` mismatch, `is_truncated=True`, missing `next_cursor` when `has_next_page=True`, or Plan C forbidden raw keys -> `PARTIAL` / `MALFORMED_OR_PARTIAL_PAGE` (`retry_allowed=False`).
  - `CONTEXT_ISSUES` optional access errors (403/404/410 or `optional_access_error`) -> explicit `EXCLUDED` (`OPTIONAL_CONTEXT_UNAVAILABLE`) without blocking required streams.
  - `max_objects` / `max_requests` / `max_bytes` overflow -> `PARTIAL_WITH_LIMIT` / `LIMIT_EXCEEDED` (`retry_allowed=False`) with non-empty `processed_range` & `remaining_range` and zero promotion.

### `CommitPrAssociationClassifier` (`T-203B`)
- `evaluate_plan_integrity(start_snapshot: RepositoryHeadSnapshot, end_snapshot: RepositoryHeadSnapshot) -> PlanIntegrityEvaluation`
- `classify_batch(*, start_snapshot, end_snapshot, pull_requests, default_branch_commits, lookup_observations) -> AssociationBatchResult`
- **Invariants**:
  - Uses `start_snapshot.default_branch` without assuming `master` (`main`, `master`, `trunk`, etc.).
  - Default-branch rename, HEAD movement, force-push (`start_head_reachable=False`), or secondary PR enumeration digest mismatch -> `PLAN_DRIFT` (`retry_allowed=False`), blocks `SUCCESS`.
  - Cross-checks PR commits, `head_sha`, `merge_commit_sha`, `squash_commit_sha`, `rebase_commit_shas`, and provider commit-to-PR API (including out-of-window PRs -> `MATCHED_PR` + `OUT_OF_WINDOW_PR_LINKED` exclusion, never `DIRECT_COMMIT`).
  - `NO_MATCH_OBSERVED` requires start/end reachability + clean empty lookup -> 1:1 `DIRECT_COMMIT` candidate with `{"user_review_required": True, "association_note": "NO_MATCH_OBSERVED_REQUIRES_CONFIRMATION"}` (no clustering).
  - Any `UNKNOWN` commit produces 0 `DIRECT_COMMIT` candidates and blocks whole-run `SUCCESS` (`INCOMPLETE_PR_ASSOCIATION`).

### `PostgresBackfillPageCommitter` (`T-203`)
- `commit_page_batch(request: PageCommitRequest, *, now: datetime | None = None) -> PageCommitResult`
- `record_non_success_terminal_state(*, workspace_id, sync_run_id, source_resource_id, stream_name, target_status, failure_reason_code, ...) -> SyncRunStatus`
- `finalize_import_run(request: FinalizeRunRequest, *, now: datetime | None = None) -> FinalizeRunResult`
- `get_stream_checkpoint(*, workspace_id, sync_run_id, stream_name) -> StreamCheckpointSnapshot | None`
- **Transaction & Fencing Invariants**:
  - Locks `sync_runs` and `sync_checkpoints` with `SELECT ... FOR UPDATE`; stale workers (`expected_checkpoint_version` or `expected_cursor` mismatch) abort with `STALE_CHECKPOINT_VERSION` and perform **0 DB writes**.
  - Active `deletion_jobs` fence or `DELETED` resource blocks page commits (`BLOCKED_BY_DELETION_FENCE`) and cancels finalization.
  - Intermediate page commits never create `IMPORT_READY` outbox rows or `work_events`/`facts`/`claims`.
  - Only `finalize_import_run` (after verifying all required streams, 0 `UNKNOWN` associations, no drift, no deletion/policy fence) transitions `sync_runs.status = 'SUCCESS'`, emits strictly 1 `IMPORT_READY` outbox row, and promotes PR + 1:1 `DIRECT_COMMIT` `work_events` idempotently in one atomic transaction.
