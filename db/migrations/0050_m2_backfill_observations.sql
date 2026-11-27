-- Migration 0050: M2 Backfill Engine Observations, PR Association State & Event Promotions
-- (M2-T203: T-203 + T-203A + T-203B)
--
-- Prerequisites: Migrations 0010, 0020, 0030, and 0040.
-- Idempotent: safe to re-apply without mutating existing rows or M1 tables.
--
-- Security & Plan C invariants:
--   * Owned by cm_migrator (NOLOGIN, NOBYPASSRLS); runtime roles never own tables.
--   * Every tenant table enforces workspace_id, Composite Foreign Keys
--     (workspace_id, parent_id) -> parent(workspace_id, id), ENABLE + FORCE RLS,
--     and fail-closed auth_ctx.current_workspace_id() policies.
--   * Every JSONB column enforces object type and rejects forbidden Plan C raw-text
--     keys recursively via public.cm_jsonb_contains_forbidden_raw_key().
--   * cm_worker_app receives least-privilege grants with zero DELETE privileges.

SET ROLE cm_migrator;

-- =============================================================================
-- 1. sync_item_outcomes (Approved skips, exclusions, failures & UNKNOWN markers)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.sync_item_outcomes (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id       uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    sync_run_id        uuid NOT NULL,
    source_resource_id uuid NOT NULL,
    source_object_id   uuid NULL,
    stream_name        text NOT NULL,
    object_type        text NOT NULL,
    provider_object_id text NOT NULL,
    outcome_kind       text NOT NULL,
    reason_code        text NOT NULL,
    affected_range     text NULL,
    retryable          boolean NOT NULL DEFAULT false,
    outcome_metadata   jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at        timestamptz NOT NULL DEFAULT now(),
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_sync_item_outcomes_stream_name
        CHECK (
            stream_name IN (
                'PRS',
                'PR_REVIEWS',
                'DEFAULT_BRANCH_COMMITS',
                'COMMIT_PR_ASSOCIATIONS',
                'CONTEXT_ISSUES'
            )
        ),
    CONSTRAINT ck_sync_item_outcomes_object_type
        CHECK (object_type IN ('PR', 'COMMIT', 'ISSUE', 'REVIEW', 'COMMENT', 'FILE', 'STREAM')),
    CONSTRAINT ck_sync_item_outcomes_provider_id_non_empty
        CHECK (length(btrim(provider_object_id)) > 0),
    CONSTRAINT ck_sync_item_outcomes_outcome_kind
        CHECK (outcome_kind IN ('SKIPPED', 'FAILED', 'EXCLUDED', 'ASSOCIATION_UNKNOWN')),
    CONSTRAINT ck_sync_item_outcomes_reason_code_non_empty
        CHECK (length(btrim(reason_code)) > 0),
    CONSTRAINT ck_sync_item_outcomes_metadata_object
        CHECK (jsonb_typeof(outcome_metadata) = 'object'),
    CONSTRAINT ck_sync_item_outcomes_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(outcome_metadata)),
    CONSTRAINT uq_sync_item_outcomes_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_sync_item_outcomes_run_item_outcome
        UNIQUE (
            sync_run_id,
            stream_name,
            object_type,
            provider_object_id,
            outcome_kind,
            reason_code
        ),
    CONSTRAINT fk_sync_item_outcomes_same_workspace_run
        FOREIGN KEY (workspace_id, sync_run_id)
        REFERENCES public.sync_runs (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_sync_item_outcomes_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_sync_item_outcomes_same_workspace_object
        FOREIGN KEY (workspace_id, source_object_id)
        REFERENCES public.source_objects (workspace_id, id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_item_outcomes_workspace_run_stream
    ON public.sync_item_outcomes (workspace_id, sync_run_id, stream_name);

CREATE INDEX IF NOT EXISTS idx_sync_item_outcomes_workspace_run_kind
    ON public.sync_item_outcomes (workspace_id, sync_run_id, outcome_kind);

-- =============================================================================
-- 2. sync_commit_pr_associations (T-203B 3-state completeness ledger)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.sync_commit_pr_associations (
    id                      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    sync_run_id             uuid NOT NULL,
    source_resource_id      uuid NOT NULL,
    commit_source_object_id uuid NOT NULL,
    commit_sha              text NOT NULL,
    default_branch_name     text NOT NULL,
    pr_association_state    text NOT NULL,
    matched_pr_provider_id  text NULL,
    matched_pr_in_window    boolean NULL,
    match_mechanism         text NULL,
    unknown_reason_code     text NULL,
    reachable_at_start      boolean NOT NULL DEFAULT true,
    reachable_at_end        boolean NOT NULL DEFAULT true,
    lookup_completed        boolean NOT NULL DEFAULT false,
    observation_metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    observed_at             timestamptz NOT NULL DEFAULT now(),
    updated_at              timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_sync_commit_pr_assoc_sha_format
        CHECK (commit_sha ~ '^[0-9a-f]{7,64}$'),
    CONSTRAINT ck_sync_commit_pr_assoc_branch_non_empty
        CHECK (length(btrim(default_branch_name)) > 0),
    CONSTRAINT ck_sync_commit_pr_assoc_state
        CHECK (pr_association_state IN ('MATCHED_PR', 'NO_MATCH_OBSERVED', 'UNKNOWN')),
    CONSTRAINT ck_sync_commit_pr_assoc_mechanism
        CHECK (
            match_mechanism IS NULL
            OR match_mechanism IN (
                'IN_WINDOW_PR_COMMIT',
                'PR_HEAD_SHA',
                'PR_MERGE_COMMIT_SHA',
                'SQUASH_COMMIT_SHA',
                'REBASE_COMMIT_SHA',
                'PROVIDER_COMMIT_PR_API',
                'OUT_OF_WINDOW_PR_API'
            )
        ),
    CONSTRAINT ck_sync_commit_pr_assoc_matched_pr_fields
        CHECK (
            pr_association_state <> 'MATCHED_PR'
            OR (
                matched_pr_provider_id IS NOT NULL
                AND length(btrim(matched_pr_provider_id)) > 0
                AND unknown_reason_code IS NULL
            )
        ),
    CONSTRAINT ck_sync_commit_pr_assoc_no_match_fields
        CHECK (
            pr_association_state <> 'NO_MATCH_OBSERVED'
            OR (
                matched_pr_provider_id IS NULL
                AND unknown_reason_code IS NULL
                AND lookup_completed = true
                AND reachable_at_start = true
                AND reachable_at_end = true
            )
        ),
    CONSTRAINT ck_sync_commit_pr_assoc_unknown_fields
        CHECK (
            pr_association_state <> 'UNKNOWN'
            OR (
                unknown_reason_code IS NOT NULL
                AND length(btrim(unknown_reason_code)) > 0
            )
        ),
    CONSTRAINT ck_sync_commit_pr_assoc_metadata_object
        CHECK (jsonb_typeof(observation_metadata) = 'object'),
    CONSTRAINT ck_sync_commit_pr_assoc_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(observation_metadata)),
    CONSTRAINT uq_sync_commit_pr_assoc_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_sync_commit_pr_assoc_run_commit
        UNIQUE (sync_run_id, commit_source_object_id),
    CONSTRAINT fk_sync_commit_pr_assoc_same_workspace_run
        FOREIGN KEY (workspace_id, sync_run_id)
        REFERENCES public.sync_runs (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_sync_commit_pr_assoc_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_sync_commit_pr_assoc_same_workspace_commit
        FOREIGN KEY (workspace_id, commit_source_object_id)
        REFERENCES public.source_objects (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sync_commit_pr_assoc_workspace_run_state
    ON public.sync_commit_pr_associations (workspace_id, sync_run_id, pr_association_state);

-- =============================================================================
-- 3. sync_run_page_observations (Committed page cursor/fingerprint audit trail)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.sync_run_page_observations (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    sync_run_id         uuid NOT NULL,
    stream_name         text NOT NULL,
    page_number         integer NOT NULL,
    request_cursor      text NULL,
    next_cursor         text NULL,
    page_fingerprint    text NOT NULL,
    checkpoint_version  integer NOT NULL,
    item_count          integer NOT NULL DEFAULT 0,
    unique_object_count integer NOT NULL DEFAULT 0,
    committed_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_sync_run_page_obs_stream_name
        CHECK (
            stream_name IN (
                'PRS',
                'PR_REVIEWS',
                'DEFAULT_BRANCH_COMMITS',
                'COMMIT_PR_ASSOCIATIONS',
                'CONTEXT_ISSUES'
            )
        ),
    CONSTRAINT ck_sync_run_page_obs_page_positive
        CHECK (page_number >= 1),
    CONSTRAINT ck_sync_run_page_obs_fingerprint_sha256
        CHECK (page_fingerprint ~ '^(sha256:)?[0-9a-f]{64}$'),
    CONSTRAINT ck_sync_run_page_obs_checkpoint_version_positive
        CHECK (checkpoint_version >= 1),
    CONSTRAINT ck_sync_run_page_obs_counts_non_negative
        CHECK (item_count >= 0 AND unique_object_count >= 0),
    CONSTRAINT uq_sync_run_page_obs_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_sync_run_page_obs_run_stream_page
        UNIQUE (sync_run_id, stream_name, page_number),
    CONSTRAINT fk_sync_run_page_obs_same_workspace_run
        FOREIGN KEY (workspace_id, sync_run_id)
        REFERENCES public.sync_runs (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sync_run_page_obs_workspace_run_stream
    ON public.sync_run_page_observations (workspace_id, sync_run_id, stream_name, page_number DESC);

-- =============================================================================
-- 4. sync_run_event_promotions (Idempotent 1-time RULE candidate promotion map)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.sync_run_event_promotions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    sync_run_id      uuid NOT NULL,
    source_object_id uuid NOT NULL,
    anchor_mode      text NOT NULL,
    work_event_id    uuid NOT NULL,
    promoted_at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_sync_run_event_promotions_anchor_mode
        CHECK (anchor_mode IN ('PR', 'DIRECT_COMMIT')),
    CONSTRAINT uq_sync_run_event_promotions_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_sync_run_event_promotions_workspace_object_mode
        UNIQUE (workspace_id, source_object_id, anchor_mode),
    CONSTRAINT uq_sync_run_event_promotions_run_object_mode
        UNIQUE (sync_run_id, source_object_id, anchor_mode),
    CONSTRAINT fk_sync_run_event_promotions_same_workspace_run
        FOREIGN KEY (workspace_id, sync_run_id)
        REFERENCES public.sync_runs (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_sync_run_event_promotions_same_workspace_object
        FOREIGN KEY (workspace_id, source_object_id)
        REFERENCES public.source_objects (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_sync_run_event_promotions_same_workspace_event
        FOREIGN KEY (workspace_id, work_event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sync_run_event_promotions_workspace_run
    ON public.sync_run_event_promotions (workspace_id, sync_run_id, anchor_mode);

-- =============================================================================
-- 5. ENABLE + FORCE Row Level Security & Fail-Closed Workspace Policies
-- =============================================================================
DO $$
DECLARE
    tbl text;
    m2_tables text[] := ARRAY[
        'sync_item_outcomes',
        'sync_commit_pr_associations',
        'sync_run_page_observations',
        'sync_run_event_promotions'
    ];
BEGIN
    FOREACH tbl IN ARRAY m2_tables LOOP
        EXECUTE format('ALTER TABLE public.%I ENABLE ROW LEVEL SECURITY', tbl);
        EXECUTE format('ALTER TABLE public.%I FORCE ROW LEVEL SECURITY', tbl);

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_api_isolation', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR SELECT TO cm_api_app '
            'USING ('
            '    workspace_id = auth_ctx.current_workspace_id() '
            '    AND EXISTS ('
            '        SELECT 1 FROM public.workspace_memberships m '
            '        WHERE m.workspace_id = %I.workspace_id '
            '          AND m.user_id = auth_ctx.current_user_id() '
            '          AND m.revoked_at IS NULL'
            '    )'
            ')',
            tbl || '_api_isolation',
            tbl,
            tbl
        );

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_worker_select', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR SELECT TO cm_worker_app '
            'USING (workspace_id = auth_ctx.current_workspace_id())',
            tbl || '_worker_select',
            tbl
        );

        EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_worker_insert', tbl);
        EXECUTE format(
            'CREATE POLICY %I ON public.%I FOR INSERT TO cm_worker_app '
            'WITH CHECK (workspace_id = auth_ctx.current_workspace_id())',
            tbl || '_worker_insert',
            tbl
        );

        IF tbl <> 'sync_run_event_promotions' THEN
            EXECUTE format('DROP POLICY IF EXISTS %I ON public.%I', tbl || '_worker_update', tbl);
            EXECUTE format(
                'CREATE POLICY %I ON public.%I FOR UPDATE TO cm_worker_app '
                'USING (workspace_id = auth_ctx.current_workspace_id()) '
                'WITH CHECK (workspace_id = auth_ctx.current_workspace_id())',
                tbl || '_worker_update',
                tbl
            );
        END IF;
    END LOOP;
END
$$;

-- =============================================================================
-- 6. Least-Privilege GRANT Matrix (scoped strictly to 0050 tables)
-- =============================================================================
REVOKE ALL ON TABLE
    public.sync_item_outcomes,
    public.sync_commit_pr_associations,
    public.sync_run_page_observations,
    public.sync_run_event_promotions
FROM PUBLIC, cm_api_app, cm_worker_app;

-- cm_api_app: read-only visibility on backfill observations and promotions
GRANT SELECT ON
    public.sync_item_outcomes,
    public.sync_commit_pr_associations,
    public.sync_run_page_observations,
    public.sync_run_event_promotions
TO cm_api_app;

-- cm_worker_app: least privilege (no DELETE; append-only promotions)
GRANT SELECT, INSERT, UPDATE ON
    public.sync_item_outcomes,
    public.sync_commit_pr_associations,
    public.sync_run_page_observations
TO cm_worker_app;

GRANT SELECT, INSERT ON public.sync_run_event_promotions TO cm_worker_app;

RESET ROLE;
