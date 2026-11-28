-- Migration 0080: M5 Permission-Safe FTS Search (T-501/T-501A), Deletion Receipts & Fence
-- (T-502/T-502A), and Wave 2 Runtime Quotas & Pause Controls
--
-- Prerequisites: Migrations 0010, 0020, 0030, 0040, 0050, 0060, and 0070.
-- Idempotent: safe to run on fresh PostgreSQL 18 (0010..0080), staged upgrade (0010..0070 -> 0080),
--             and repeated re-apply (0080 twice).
--
-- Design guarantees:
--   * Owned by cm_migrator (NOLOGIN, NOBYPASSRLS); runtime roles never own tables.
--   * Every tenant table enforces workspace_id, Composite Foreign Keys
--     (workspace_id, parent_id) -> parent(workspace_id, id), ENABLE + FORCE ROW LEVEL SECURITY,
--     active workspace_memberships check (revoked_at IS NULL) for cm_api_app, and
--     auth_ctx.current_workspace_id() isolation for cm_worker_app.
--   * Plan C (방안 C) raw-text non-persistence + secret redaction CHECK constraints enforced
--     recursively on every JSONB column; public.search_runs stores query_hash ONLY (zero raw query).
--   * Single active deletion_jobs per (workspace_id, source_resource_id) enforced by partial
--     unique index uq_deletion_jobs_single_active_per_resource (DELETE-01).

SET ROLE cm_migrator;

-- =============================================================================
-- 1. public.search_entries (T-501 / T-501A Non-Raw FTS Projection)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.search_entries (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    source_resource_id  uuid NULL,
    entity_type         text NOT NULL,
    entity_id           uuid NOT NULL,
    entity_version      integer NOT NULL,
    status_projection   text NOT NULL,
    title_or_summary    text NOT NULL,
    search_tokens_text  text NOT NULL,
    search_tsv          tsvector GENERATED ALWAYS AS (
        setweight(to_tsvector('simple', coalesce(title_or_summary, '')), 'A')
        || setweight(to_tsvector('english', coalesce(search_tokens_text, '')), 'B')
    ) STORED,
    index_version       text NOT NULL,
    projection_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_search_entries_entity_type
        CHECK (entity_type IN ('WORK_EVENT', 'FACT', 'CLAIM')),
    CONSTRAINT ck_search_entries_entity_version_positive
        CHECK (entity_version >= 1),
    CONSTRAINT ck_search_entries_status_non_empty
        CHECK (length(btrim(status_projection)) > 0),
    CONSTRAINT ck_search_entries_title_bounds
        CHECK (length(btrim(title_or_summary)) > 0 AND length(title_or_summary) <= 2000),
    CONSTRAINT ck_search_entries_tokens_bounds
        CHECK (length(search_tokens_text) <= 8000),
    CONSTRAINT ck_search_entries_index_version_non_empty
        CHECK (length(btrim(index_version)) > 0),
    CONSTRAINT ck_search_entries_metadata_object
        CHECK (jsonb_typeof(projection_metadata) = 'object'),
    CONSTRAINT ck_search_entries_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(projection_metadata)),
    CONSTRAINT ck_search_entries_metadata_no_secret
        CHECK (NOT public.cm_jsonb_contains_forbidden_secret_key(projection_metadata)),
    CONSTRAINT uq_search_entries_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_search_entries_workspace_entity
        UNIQUE (workspace_id, entity_type, entity_id),
    CONSTRAINT fk_search_entries_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_search_entries_tsv
    ON public.search_entries USING GIN (search_tsv);

CREATE INDEX IF NOT EXISTS idx_search_entries_workspace_resource_type
    ON public.search_entries (workspace_id, source_resource_id, entity_type);

-- =============================================================================
-- 2. public.search_runs (SEARCH-04 / T-501A Metadata-Only Search Audit)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.search_runs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    source_resource_id  uuid NULL,
    query_hash          text NOT NULL,
    entity_types        text[] NOT NULL DEFAULT '{}'::text[],
    index_version       text NOT NULL,
    ranking_version     text NOT NULL,
    candidate_limit     integer NOT NULL,
    page_limit          integer NOT NULL,
    cursor_offset       integer NOT NULL DEFAULT 0,
    raw_candidate_count integer NOT NULL DEFAULT 0,
    lag_filtered_count  integer NOT NULL DEFAULT 0,
    returned_count      integer NOT NULL DEFAULT 0,
    degraded_mode       boolean NOT NULL DEFAULT false,
    run_metadata        jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_search_runs_query_hash_sha256
        CHECK (query_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT ck_search_runs_index_version_non_empty
        CHECK (length(btrim(index_version)) > 0),
    CONSTRAINT ck_search_runs_ranking_version_non_empty
        CHECK (length(btrim(ranking_version)) > 0),
    CONSTRAINT ck_search_runs_candidate_limit_positive
        CHECK (candidate_limit >= 1),
    CONSTRAINT ck_search_runs_page_limit_bounds
        CHECK (page_limit >= 1 AND page_limit <= 100),
    CONSTRAINT ck_search_runs_cursor_offset_non_negative
        CHECK (cursor_offset >= 0),
    CONSTRAINT ck_search_runs_counts_non_negative
        CHECK (
            raw_candidate_count >= 0
            AND lag_filtered_count >= 0
            AND returned_count >= 0
        ),
    CONSTRAINT ck_search_runs_metadata_object
        CHECK (jsonb_typeof(run_metadata) = 'object'),
    CONSTRAINT ck_search_runs_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(run_metadata)),
    CONSTRAINT ck_search_runs_metadata_no_secret
        CHECK (NOT public.cm_jsonb_contains_forbidden_secret_key(run_metadata)),
    CONSTRAINT uq_search_runs_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_search_runs_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_search_runs_workspace_created
    ON public.search_runs (workspace_id, created_at DESC);

-- =============================================================================
-- 3. public.deletion_component_receipts & deletion_jobs / idempotency Guards
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.deletion_component_receipts (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    deletion_job_id  uuid NOT NULL,
    component        text NOT NULL,
    status           text NOT NULL DEFAULT 'PENDING',
    attempt          integer NOT NULL DEFAULT 0,
    purged_count     integer NOT NULL DEFAULT 0,
    last_error_code  text NULL,
    receipt_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz NULL,

    CONSTRAINT ck_deletion_component_receipts_component
        CHECK (
            component IN (
                'AUTHORITATIVE_DOMAIN',
                'FTS_INDEX',
                'EPHEMERAL_CACHE',
                'EXPORTS',
                'JOBS_AND_OUTBOX'
            )
        ),
    CONSTRAINT ck_deletion_component_receipts_status
        CHECK (status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT ck_deletion_component_receipts_attempt_non_negative
        CHECK (attempt >= 0),
    CONSTRAINT ck_deletion_component_receipts_purged_count_non_negative
        CHECK (purged_count >= 0),
    CONSTRAINT ck_deletion_component_receipts_completed_at_consistency
        CHECK (
            (status = 'SUCCEEDED' AND completed_at IS NOT NULL)
            OR (status <> 'SUCCEEDED' AND completed_at IS NULL)
        ),
    CONSTRAINT ck_deletion_component_receipts_metadata_object
        CHECK (jsonb_typeof(receipt_metadata) = 'object'),
    CONSTRAINT ck_deletion_component_receipts_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(receipt_metadata)),
    CONSTRAINT ck_deletion_component_receipts_metadata_no_secret
        CHECK (NOT public.cm_jsonb_contains_forbidden_secret_key(receipt_metadata)),
    CONSTRAINT uq_deletion_component_receipts_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_deletion_component_receipts_job_component
        UNIQUE (workspace_id, deletion_job_id, component),
    CONSTRAINT fk_deletion_component_receipts_same_workspace_job
        FOREIGN KEY (workspace_id, deletion_job_id)
        REFERENCES public.deletion_jobs (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_deletion_component_receipts_workspace_job
    ON public.deletion_component_receipts (workspace_id, deletion_job_id);

-- DELETE-01: Prevent concurrent deletion confirms from creating duplicate active deletion jobs.
CREATE UNIQUE INDEX IF NOT EXISTS uq_deletion_jobs_single_active_per_resource
    ON public.deletion_jobs (workspace_id, source_resource_id)
    WHERE status IN ('PENDING', 'RUNNING', 'PARTIAL');

-- Extend public.api_idempotency_records idempotently to record CONFIRM_SOURCE_DELETION 24h receipts.
ALTER TABLE public.api_idempotency_records
    ALTER COLUMN target_sync_run_id DROP NOT NULL;

ALTER TABLE public.api_idempotency_records
    ADD COLUMN IF NOT EXISTS target_deletion_job_id uuid NULL;

ALTER TABLE public.api_idempotency_records
    DROP CONSTRAINT IF EXISTS ck_api_idempotency_operation_scope;

ALTER TABLE public.api_idempotency_records
    ADD CONSTRAINT ck_api_idempotency_operation_scope
    CHECK (
        operation_scope IN (
            'SYNC_RUN_CREATE',
            'SYNC_RUN_RETRY',
            'CONFIRM_SOURCE_DELETION'
        )
    );

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_api_idempotency_target_consistency'
          AND conrelid = 'public.api_idempotency_records'::regclass
    ) THEN
        ALTER TABLE public.api_idempotency_records
            ADD CONSTRAINT ck_api_idempotency_target_consistency
            CHECK (
                (
                    operation_scope IN ('SYNC_RUN_CREATE', 'SYNC_RUN_RETRY')
                    AND target_sync_run_id IS NOT NULL
                    AND target_deletion_job_id IS NULL
                )
                OR (
                    operation_scope = 'CONFIRM_SOURCE_DELETION'
                    AND target_deletion_job_id IS NOT NULL
                    AND target_sync_run_id IS NULL
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_api_idempotency_same_workspace_deletion_job'
          AND conrelid = 'public.api_idempotency_records'::regclass
    ) THEN
        ALTER TABLE public.api_idempotency_records
            ADD CONSTRAINT fk_api_idempotency_same_workspace_deletion_job
            FOREIGN KEY (workspace_id, target_deletion_job_id)
            REFERENCES public.deletion_jobs (workspace_id, id)
            ON DELETE CASCADE;
    END IF;
END
$$;

-- =============================================================================
-- 4. Wave 2 Provisioning: public.workspace_quota_ledgers & runtime_pause_controls
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.workspace_quota_ledgers (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id       uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    period_key         text NOT NULL,
    metric_kind        text NOT NULL,
    used_value         bigint NOT NULL DEFAULT 0,
    soft_warning_limit bigint NOT NULL,
    hard_cutoff_limit  bigint NOT NULL,
    ledger_metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_workspace_quota_ledgers_period_key_non_empty
        CHECK (length(btrim(period_key)) > 0),
    CONSTRAINT ck_workspace_quota_ledgers_metric_kind
        CHECK (
            metric_kind IN (
                'IMPORT_OBJECTS',
                'PROVIDER_REQUESTS',
                'REFETCH_BYTES',
                'EXPORT_BUILDS',
                'SEARCH_QUERIES',
                'ESTIMATED_COST_KRW',
                'LLM_TOKENS'
            )
        ),
    CONSTRAINT ck_workspace_quota_ledgers_used_non_negative
        CHECK (used_value >= 0),
    CONSTRAINT ck_workspace_quota_ledgers_limits_order
        CHECK (soft_warning_limit >= 0 AND hard_cutoff_limit >= soft_warning_limit),
    CONSTRAINT ck_workspace_quota_ledgers_metadata_object
        CHECK (jsonb_typeof(ledger_metadata) = 'object'),
    CONSTRAINT ck_workspace_quota_ledgers_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(ledger_metadata)),
    CONSTRAINT ck_workspace_quota_ledgers_metadata_no_secret
        CHECK (NOT public.cm_jsonb_contains_forbidden_secret_key(ledger_metadata)),
    CONSTRAINT uq_workspace_quota_ledgers_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_workspace_quota_ledgers_workspace_period_metric
        UNIQUE (workspace_id, period_key, metric_kind)
);

CREATE INDEX IF NOT EXISTS idx_workspace_quota_ledgers_workspace_period
    ON public.workspace_quota_ledgers (workspace_id, period_key);

CREATE TABLE IF NOT EXISTS public.runtime_pause_controls (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id       uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    scope_kind         text NOT NULL,
    source_resource_id uuid NULL,
    pause_state        text NOT NULL DEFAULT 'ACTIVE',
    reason_code        text NULL,
    paused_by_actor_id uuid NULL,
    control_metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    paused_at          timestamptz NULL,
    resumed_at         timestamptz NULL,
    updated_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_runtime_pause_controls_scope_kind
        CHECK (scope_kind IN ('WORKSPACE', 'SOURCE_RESOURCE')),
    CONSTRAINT ck_runtime_pause_controls_scope_resource_consistency
        CHECK (
            (scope_kind = 'WORKSPACE' AND source_resource_id IS NULL)
            OR (scope_kind = 'SOURCE_RESOURCE' AND source_resource_id IS NOT NULL)
        ),
    CONSTRAINT ck_runtime_pause_controls_pause_state
        CHECK (
            pause_state IN (
                'ACTIVE',
                'PAUSED_QUOTA',
                'PAUSED_CIRCUIT_BREAKER',
                'PAUSED_OPERATOR'
            )
        ),
    CONSTRAINT ck_runtime_pause_controls_metadata_object
        CHECK (jsonb_typeof(control_metadata) = 'object'),
    CONSTRAINT ck_runtime_pause_controls_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(control_metadata)),
    CONSTRAINT ck_runtime_pause_controls_metadata_no_secret
        CHECK (NOT public.cm_jsonb_contains_forbidden_secret_key(control_metadata)),
    CONSTRAINT uq_runtime_pause_controls_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_runtime_pause_controls_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_pause_controls_workspace_scope
    ON public.runtime_pause_controls (workspace_id)
    WHERE scope_kind = 'WORKSPACE';

CREATE UNIQUE INDEX IF NOT EXISTS uq_runtime_pause_controls_resource_scope
    ON public.runtime_pause_controls (workspace_id, source_resource_id)
    WHERE scope_kind = 'SOURCE_RESOURCE';

-- =============================================================================
-- 5. ENABLE + FORCE Row Level Security & Fail-Closed Workspace Policies
-- =============================================================================
DO $$
DECLARE
    tbl text;
    m5_tables text[] := ARRAY[
        'search_entries',
        'search_runs',
        'deletion_component_receipts',
        'workspace_quota_ledgers',
        'runtime_pause_controls'
    ];
BEGIN
    FOREACH tbl IN ARRAY m5_tables LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tbl);

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_api_workspace_isolation', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO cm_api_app '
            'USING ('
            '    workspace_id = auth_ctx.current_workspace_id() '
            '    AND EXISTS ('
            '        SELECT 1 FROM public.workspace_memberships m '
            '        WHERE m.workspace_id = %I.workspace_id '
            '          AND m.user_id = auth_ctx.current_user_id() '
            '          AND m.revoked_at IS NULL'
            '    )'
            ') '
            'WITH CHECK ('
            '    workspace_id = auth_ctx.current_workspace_id() '
            '    AND EXISTS ('
            '        SELECT 1 FROM public.workspace_memberships m '
            '        WHERE m.workspace_id = %I.workspace_id '
            '          AND m.user_id = auth_ctx.current_user_id() '
            '          AND m.revoked_at IS NULL'
            '    )'
            ')',
            tbl || '_api_workspace_isolation',
            tbl,
            tbl,
            tbl
        );

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_worker_workspace_isolation', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR ALL TO cm_worker_app '
            'USING (workspace_id = auth_ctx.current_workspace_id()) '
            'WITH CHECK (workspace_id = auth_ctx.current_workspace_id())',
            tbl || '_worker_workspace_isolation',
            tbl
        );
    END LOOP;
END
$$;

-- =============================================================================
-- 6. Least-Privilege GRANT Matrix (scoped strictly to 0080 tables)
-- =============================================================================
REVOKE ALL ON TABLE
    public.search_entries,
    public.search_runs,
    public.deletion_component_receipts,
    public.workspace_quota_ledgers,
    public.runtime_pause_controls
FROM PUBLIC, cm_api_app, cm_worker_app;

-- search_entries: mutable FTS projection (sync + delete on purge)
GRANT SELECT, INSERT, UPDATE, DELETE ON public.search_entries TO cm_api_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON public.search_entries TO cm_worker_app;

-- search_runs: append-only metadata audit (no UPDATE/DELETE)
GRANT SELECT, INSERT ON public.search_runs TO cm_api_app;
GRANT SELECT, INSERT ON public.search_runs TO cm_worker_app;

-- deletion_component_receipts, workspace_quota_ledgers, runtime_pause_controls: no DELETE
GRANT SELECT, INSERT, UPDATE ON
    public.deletion_component_receipts,
    public.workspace_quota_ledgers,
    public.runtime_pause_controls
TO cm_api_app;

GRANT SELECT, INSERT, UPDATE ON
    public.deletion_component_receipts,
    public.workspace_quota_ledgers,
    public.runtime_pause_controls
TO cm_worker_app;

RESET ROLE;
