# M3 Event Candidate Builder Contract (`T-301` + `T-301A`, Issue #3)

## 1. Public Entrypoints (for `T-303`, `T-304`, `T-306A`, and `InitialBackfillJobWorker`)

### `PostgresEventCandidateBuilder` (`career_memory_api.event_candidates`)
```python
class PostgresEventCandidateBuilder:
    def __init__(
        self,
        *,
        worker_connection_factory: ConnectionFactory,
        clock: Callable[[], datetime] | None = None,
        before_commit_hook: BeforeCommitHook | None = None,
    ) -> None: ...

    def build_candidates_for_sync_run(
        self,
        *,
        workspace_id: str,
        sync_run_id: str,
        now: datetime | None = None,
    ) -> CandidateBuildResult: ...

    def build_candidates(
        self,
        request: CandidateBuildRequest,
        *,
        now: datetime | None = None,
    ) -> CandidateBuildResult: ...
```

### Pure Deterministic Flag Evaluator (`career_memory_api.event_candidates.flags`)
```python
flags: tuple[ReviewFlagCode, ...] = evaluate_review_flags(ReviewFlagInputs(...))
```

### Optional Post-Import Worker Integration (`career_memory_api.initial_import.worker_runner`)
`InitialBackfillJobWorker` accepts an optional `event_candidate_builder: PostgresEventCandidateBuilder | None = None` (default `None` so existing M2 / `B-004` callers and snapshots remain 100% invariant). When configured and `run_event_candidate_builder=True`, it executes strictly after `sync_runs.status == 'SUCCESS'` and `import_ready == True`, returning the result in `WorkerStepResult.event_candidates`.

## 2. Schema Scope & `OD-007` Boundary
- Operates strictly on existing PostgreSQL 18 migrations `0010`–`0050` (`public.work_events`, `public.event_memberships`, `public.sync_run_event_promotions`, `public.source_objects`, `public.source_object_revisions`, `public.content_units`, `public.sync_commit_pr_associations`, `public.sync_item_outcomes`, `public.identities`, `public.external_identity_mappings`, `public.outbox_events`, `public.deletion_jobs`).
- Introduces **0** new database migrations or tables (respecting `OD-007` deferral of Event-to-Event `CANNOT_LINK` physical storage).
- Runs under least-privilege `cm_worker_app` (`NOBYPASSRLS`) with `bind_worker_workspace_context`.

## 3. Domain Invariants

| Rule | Guarantee |
| --- | --- |
| **Import-Ready Gate (`T-301A`)** | Rejects (`SYNC_RUN_NOT_IMPORT_READY`, 0 writes) unless `sync_runs.status = 'SUCCESS'`, `import_ready = true`, `is_abandoned = false`, and an `IMPORT_READY` outbox event exists for the run. |
| **PR Association Completeness (`T-301A`, `EV-003`, `CAND-EV-020`)** | Rejects (`PR_ASSOCIATION_INCOMPLETE`, 0 writes) if any `sync_commit_pr_associations` or active `COMMIT` row is `UNKNOWN`, any unresolved `ASSOCIATION_UNKNOWN` outcome exists, or any default-branch commit lacks a completeness ledger observation. |
| **Policy & Deletion Fence (Start + Pre-Commit)** | Verifies `source_resources` (`status='ACTIVE'`, `visibility='PUBLIC'`, `raw_storage_mode='NONE'`), `source_accounts` (`status='ACTIVE'`), and absence of active `deletion_jobs` at transaction start AND again before releasing the write savepoint (`DELETION_FENCE_ACTIVE`, `POLICY_BLOCKED`, `SOURCE_ACCOUNT_REVOKED` with 0 persisted writes). |
| **`EVENT-01`** | Creates/enriches exactly 1 `anchor_mode='PR'` `CANDIDATE` per eligible merged PR targeting provider-reported `default_branch` within the import window. Never merges PRs automatically (`EV-005`, `EV-006`). |
| **`EVENT-02` & `EVENT-03`** | Attaches provider-confirmed PR commits (`PR_COMMIT`), PR reviews / inline review comments (`PR_REVIEW`), and PR comments (`PR_COMMENT`) strictly as `MUST_LINK` with `origin='SOURCE'`. Upgrades prior M2 `origin='RULE'` rows to `origin='SOURCE'` without creating duplicates. |
| **`EVENT-04`** | Attaches referenced `ISSUE` objects and shared ticket/label context clues strictly as `CONTEXT_ONLY` (`ISSUE_REFERENCE`, `origin='SOURCE'` or `'RULE'`). Never promotes to `MUST_LINK`, never creates standalone Issue events, never auto-merges PRs. |
| **`EVENT-05`** | Any active `CANNOT_LINK` row on `(event_id, source_object_id)` blocks automatic `MUST_LINK` / `CONTEXT_ONLY` linking for that object and is never overwritten or invalidated. |
| **`EVENT-07`** | Never auto-generates `PR_SEGMENT` or `USER_COMPOSITE` events (`auto_split_count = 0`, `total_user_composite_events = 0`). |
| **`EVENT-10` & `SYNC-20` (`T-301A`)** | Creates 1:1 `anchor_mode='DIRECT_COMMIT'` candidates strictly for `NO_MATCH_OBSERVED` commits verified `reachable_at_start=true`, `reachable_at_end=true`, `lookup_completed=true` on provider-reported `default_branch` (never hardcoded `main`/`master`). Excludes non-default branches (`EV-004`, `CAND-EV-019`), in-window/out-of-window PR commits, and squash/rebase/merge SHAs. Zero automatic commit clustering. |
| **Plan C (`REF-04`)** | Persists strictly non-raw metadata (`default_branch`, `abbreviated_sha`, `committed_at`, `default_branch_reachable=True`, `pr_association_state='NO_MATCH_OBSERVED'`, `no_match_observed_means_definitely_no_pr=False`, `requires_user_confirmation=True`, `badge_code='DIRECT_DEFAULT_BRANCH_NO_PR'`, `review_flags`). Every JSONB write passes `assert_no_forbidden_raw_keys`. |
| **Deterministic Review Flags (§7, `EV-007`)** | Pure deterministic evaluation (0 LLM calls) storing sorted codes in `work_events.summary_metadata["review_flags"]`: `AMBIGUOUS_AUTHOR_IDENTITY`, `DIRECT_COMMIT_ASSOCIATION_CONFIRMATION_NEEDED`, `GENERIC_TITLE_OR_SUMMARY`, `MIXED_PURPOSE_SIGNALS`, `MULTIPLE_LINKED_ISSUES`, `PARTIAL_SOURCE_SKIPPED`, `REVIEW_REQUIRED_MULTI_PARENT`. |
| **Idempotency & User Review Protection** | Repeated runs on the same `sync_run_id` produce 0 duplicate `work_events` or `event_memberships`. Existing events with `status IN ('CONFIRMED','REJECTED','SUPERSEDED')`, `version > 1`, or `created_by = 'USER'` are never modified. |
