# M2-T206 Initial Import Orchestrator & Safe Checkpoint Resume Contract

## 1. Production Entry Points (for M2 Runtime & B-004 `T-005A~D` Fault Harness)

### `InitialBackfillOrchestrator` (`career_memory_api.initial_import`)
```python
class InitialBackfillOrchestrator:
    def __init__(
        self,
        *,
        worker_connection_factory: ConnectionFactory,
        github_client: GitHubImportClientPort,
        page_committer: PostgresBackfillPageCommitter | None = None,
        github_connect_service: PostgresGitHubConnectService | None = None,
        lifecycle_hook: BackfillLifecycleHookPort | None = None,
        completion_gate: CompletionGateEvaluator | None = None,
        association_classifier: CommitPrAssociationClassifier | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None: ...

    def execute_sync_run(
        self,
        *,
        workspace_id: str,
        sync_run_id: str,
        access: WorkspaceAccessContext | None = None,
        attempt: int = 1,
        on_page_committed: Callable[[PageHookContext], None] | None = None,
        now: datetime | None = None,
    ) -> InitialBackfillExecutionResult: ...
```

### `BackfillLifecycleHookPort` (`career_memory_api.initial_import.hooks`)
Default implementation: `NoopBackfillLifecycleHook()`.

```python
class BackfillLifecycleHookPort(Protocol):
    def before_page_commit(self, ctx: PageHookContext) -> None: ...
    def after_page_commit(self, ctx: PageHookContext) -> None: ...
    def before_finalize(self, ctx: FinalizeHookContext) -> None: ...
    def after_finalize(self, ctx: FinalizeHookContext) -> None: ...
```

### `InitialBackfillJobWorker` (`career_memory_api.initial_import.worker_runner`)
```python
worker = InitialBackfillJobWorker(
    job_runtime=job_runtime,
    orchestrator=orchestrator,
    derived_unit_processor=derived_processor,
)
step = worker.claim_and_execute_next(
    workspace_id,
    worker_id="worker-1",
    access=access,
    run_derived_units=True,
)
```

### `DerivedUnitIngestionProcessor` (`career_memory_api.initial_import.derived_units`)
```python
processor = DerivedUnitIngestionProcessor(
    worker_connection_factory=worker_conn_factory,
    content_fetcher=GitHubResolverContentFetcher(transport=transport),
    resolver_service=resolver_service,
)
res = processor.process_import_ready_run(
    workspace_id=workspace_id,
    sync_run_id=sync_run_id,
)
```

## 2. Core Invariants (`T-206`, `SYNC-04/10/12/14/21`, `REPLAY-01/02/04`, Plan C §3.4)

1. **Pre-Flight Gate**: Verifies live actor membership & resource policy (`assert_resource_processing_allowed`), active GitHub credential (`resolve_active_credential_for_resource`), absence of deletion fence, and live `PUBLIC` visibility (`fetch_repository_snapshot`).
2. **Immutable Frozen Plan**: Persists `FrozenImportPlan` (`default_branch`, `start_head_sha`, `backfill_from`, `cutoff_at`, `limits`) into `sync_runs.plan_spec` on first run; every resume loads the exact frozen plan verbatim and never re-anchors HEAD or time bounds.
3. **Checkpoint Resume Convergence**: Reads `sync_checkpoints` per stream (`PRS`, `PR_REVIEWS`, `DEFAULT_BRANCH_COMMITS`, `COMMIT_PR_ASSOCIATIONS`, optional `CONTEXT_ISSUES`), skips completed streams, and resumes strictly after `last_committed_page`. Clean execution and interrupted-then-resumed execution converge to identical logical PostgreSQL snapshots with 0 duplicate objects, revisions, events, or `IMPORT_READY` outbox rows.
4. **Fail-Closed Non-Promotion**: `PLAN_DRIFT`, `PARTIAL_WITH_LIMIT`, `CURSOR_CYCLE_DETECTED`, `MALFORMED_OR_PARTIAL_PAGE`, `INCOMPLETE_PR_ASSOCIATION` (`UNKNOWN`), non-`PUBLIC` visibility flips, `401` revocation, and deletion fences set `retry_allowed=False`, emit 0 `IMPORT_READY` outbox rows, and promote 0 `work_events`.
5. **Post-`IMPORT_READY` Derived Units**: `DerivedUnitIngestionProcessor` runs strictly when `sync_runs.status='SUCCESS'`, `import_ready=true`, and the `IMPORT_READY` outbox row exists; writes Plan C metadata (`text_content=NULL`, `content_ref=NULL`, `retention_class='NONE'`). Refetch mismatch or visibility/policy block projects ONLY `sync_runs.processing_status` (`BLOCKED_SOURCE` / `BLOCKED_POLICY`) without mutating historical `sync_runs.status='SUCCESS'`.
