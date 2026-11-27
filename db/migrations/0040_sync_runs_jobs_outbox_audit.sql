-- Migration 0040: SyncRuns, Jobs, Outbox, Audit, Idempotency Receipts, and Deletion Fences
-- (M1-T105: T-105 + T-105A / REL-02)
--
-- Design guarantees:
-- 1. Standalone + 0010 / 0020 / 0030 compatible idempotent bootstrap.
-- 2. Single-transaction initial import persistence (sync_runs + jobs + outbox_events +
--    api_idempotency_records + audit_events) with 24h non-extending idempotency records.
-- 3. One active SyncRun per (workspace_id, source_resource_id) enforced by partial unique index
--    and CONTRACT@87f1515 ImportStatus invariants enforced via DB CHECK constraints.
-- 4. T-105A (REL-02) durable DB Job state machine (PENDING / RUNNING / RETRY_WAIT / SUCCEEDED /
--    FAILED_TERMINAL / CANCELED), lease fencing, poison-work terminal preservation, replay lineage
--    (execution_parent_id), and 1-time logical side-effect ledgers (job_side_effect_ledger,
--    outbox_consumed_effects) + single IMPORT_READY outbox event per SyncRun.
-- 5. Plan C (방안 C) raw-text non-persistence + secret redaction CHECK constraints across all
--    JSONB columns.
-- 6. Multi-tenant isolation via NOBYPASSRLS runtime roles, ENABLE + FORCE ROW LEVEL SECURITY,
--    fail-closed auth_ctx.current_workspace_id(), live membership verification for cm_api_app,
--    and Composite Foreign Keys across all parent-child relationships.

-- =============================================================================
-- 1. Idempotent Role & Schema Bootstrap (compatible with 0010 / 0020)
-- =============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cm_migrator') THEN
        CREATE ROLE cm_migrator
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cm_api_app') THEN
        CREATE ROLE cm_api_app
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'cm_worker_app') THEN
        CREATE ROLE cm_worker_app
            NOLOGIN
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;
END
$$;

DO $$
BEGIN
    IF current_setting('is_superuser') = 'on' THEN
        REVOKE CREATE ON SCHEMA public FROM PUBLIC;
        GRANT USAGE, CREATE ON SCHEMA public TO cm_migrator;
        GRANT USAGE ON SCHEMA public TO cm_api_app, cm_worker_app;

        IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'auth_ctx') THEN
            CREATE SCHEMA auth_ctx AUTHORIZATION cm_migrator;
        END IF;
    END IF;
END
$$;

SET ROLE cm_migrator;

REVOKE ALL ON SCHEMA auth_ctx FROM PUBLIC;
GRANT USAGE ON SCHEMA auth_ctx TO cm_api_app, cm_worker_app;

-- =============================================================================
-- 2. Fail-Closed Context Helpers & Plan C / Secret JSONB Guards
-- =============================================================================
CREATE OR REPLACE FUNCTION auth_ctx.current_workspace_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN COALESCE(current_setting('app.current_workspace_id', true), '')
             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN current_setting('app.current_workspace_id', true)::uuid
        ELSE NULL
    END;
$$;

CREATE OR REPLACE FUNCTION auth_ctx.current_user_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN COALESCE(current_setting('app.current_user_id', true), '')
             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN current_setting('app.current_user_id', true)::uuid
        ELSE NULL
    END;
$$;

REVOKE ALL ON FUNCTION auth_ctx.current_workspace_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_ctx.current_user_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_ctx.current_workspace_id() TO cm_api_app, cm_worker_app;
GRANT EXECUTE ON FUNCTION auth_ctx.current_user_id() TO cm_api_app;

-- Identical signature and forbidden raw-text key set as 0020_source_provenance_metadata.sql.
CREATE OR REPLACE FUNCTION public.cm_jsonb_contains_forbidden_raw_key(payload jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    WITH RECURSIVE walk(val) AS (
        SELECT payload
        UNION ALL
        SELECT sub.child
        FROM walk
        CROSS JOIN LATERAL (
            SELECT e.value AS child
            FROM jsonb_each(walk.val) AS e(key, value)
            WHERE jsonb_typeof(walk.val) = 'object'
            UNION ALL
            SELECT a.elem AS child
            FROM jsonb_array_elements(walk.val) AS a(elem)
            WHERE jsonb_typeof(walk.val) = 'array'
        ) AS sub
    )
    SELECT EXISTS (
        SELECT 1
        FROM walk
        CROSS JOIN LATERAL jsonb_object_keys(walk.val) AS key_name
        WHERE jsonb_typeof(walk.val) = 'object'
          AND lower(key_name) IN (
              'body',
              'raw_body',
              'raw_text',
              'text_content',
              'diff',
              'patch',
              'comment_body',
              'message_body',
              'quote',
              'extracted_span',
              'prompt',
              'raw_payload',
              'raw_content',
              'full_text',
              'source_text',
              'raw_diff',
              'commit_message_body',
              'prompt_text',
              'system_prompt',
              'user_prompt',
              'completion',
              'raw_response',
              'content_text',
              'excerpt',
              'snippet',
              'verbatim_quote',
              'pr_body',
              'issue_body',
              'review_body',
              'patch_text',
              'diff_text',
              'hunk_text'
          )
    );
$$;

-- Recursive secret/credential key detector for audit_events.redacted_metadata.
CREATE OR REPLACE FUNCTION public.cm_jsonb_contains_forbidden_secret_key(payload jsonb)
RETURNS boolean
LANGUAGE sql
IMMUTABLE
STRICT
AS $$
    WITH RECURSIVE walk(val) AS (
        SELECT payload
        UNION ALL
        SELECT sub.child
        FROM walk
        CROSS JOIN LATERAL (
            SELECT e.value AS child
            FROM jsonb_each(walk.val) AS e(key, value)
            WHERE jsonb_typeof(walk.val) = 'object'
            UNION ALL
            SELECT a.elem AS child
            FROM jsonb_array_elements(walk.val) AS a(elem)
            WHERE jsonb_typeof(walk.val) = 'array'
        ) AS sub
    )
    SELECT EXISTS (
        SELECT 1
        FROM walk
        CROSS JOIN LATERAL jsonb_object_keys(walk.val) AS key_name
        WHERE jsonb_typeof(walk.val) = 'object'
          AND lower(key_name) IN (
              'access_token',
              'refresh_token',
              'bearer_token',
              'authorization',
              'client_secret',
              'password',
              'api_key',
              'private_key',
              'pkce_code_verifier',
              'oauth_token',
              'github_token',
              'secret_value',
              'session_token',
              'cookie',
              'set_cookie'
          )
    );
$$;

REVOKE ALL ON FUNCTION public.cm_jsonb_contains_forbidden_raw_key(jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION public.cm_jsonb_contains_forbidden_secret_key(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cm_jsonb_contains_forbidden_raw_key(jsonb) TO cm_api_app, cm_worker_app;
GRANT EXECUTE ON FUNCTION public.cm_jsonb_contains_forbidden_secret_key(jsonb) TO cm_api_app, cm_worker_app;

-- =============================================================================
-- 3. Standalone / 0010 / 0020 Compatible Fallback Base Tables
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.users (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_issuer  text NOT NULL CHECK (btrim(auth_issuer) <> ''),
    auth_subject text NOT NULL CHECK (btrim(auth_subject) <> ''),
    email        text NULL,
    name         text NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_auth_issuer_subject UNIQUE (auth_issuer, auth_subject)
);

CREATE TABLE IF NOT EXISTS public.workspaces (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id uuid NULL REFERENCES public.users (id),
    name          text NOT NULL DEFAULT 'default',
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.workspace_memberships (
    workspace_id uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    user_id      uuid NOT NULL REFERENCES public.users (id) ON DELETE CASCADE,
    role         text NOT NULL DEFAULT 'OWNER' CHECK (role IN ('OWNER', 'MEMBER', 'VIEWER')),
    revoked_at   timestamptz NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);

ALTER TABLE public.workspace_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workspace_memberships FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'workspace_memberships'
          AND 'cm_api_app' = ANY(roles)
    ) THEN
        CREATE POLICY workspace_memberships_api_self_active_select
            ON public.workspace_memberships
            FOR SELECT
            TO cm_api_app
            USING (
                user_id = auth_ctx.current_user_id()
                AND revoked_at IS NULL
            );
    END IF;
END
$$;

GRANT SELECT ON public.workspace_memberships TO cm_api_app;
REVOKE ALL ON TABLE public.users, public.workspaces, public.workspace_memberships FROM cm_worker_app;

CREATE TABLE IF NOT EXISTS public.source_accounts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    provider            text NOT NULL CHECK (provider IN ('GITHUB')),
    external_account_id text NOT NULL CHECK (btrim(external_account_id) <> ''),
    token_secret_ref    text NOT NULL CHECK (btrim(token_secret_ref) <> ''),
    scopes              text[] NOT NULL DEFAULT ARRAY[]::text[],
    status              text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'REVOKED')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    revoked_at          timestamptz NULL,
    CONSTRAINT uq_source_accounts_workspace_provider_external
        UNIQUE (workspace_id, provider, external_account_id),
    CONSTRAINT uq_source_accounts_workspace_id_id
        UNIQUE (workspace_id, id)
);

-- Exact 0020 structure for source_resources when 0020 has not yet been applied.
CREATE TABLE IF NOT EXISTS public.source_resources (
    id                         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id               uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    source_account_id          uuid NOT NULL,
    provider_resource_id       text NOT NULL,
    resource_type              text NOT NULL,
    name                       text NOT NULL,
    canonical_url              text NOT NULL,
    visibility                 text NOT NULL,
    processing_mode            text NOT NULL,
    raw_storage_mode           text NOT NULL DEFAULT 'NONE',
    raw_retention_seconds      integer NULL,
    status                     text NOT NULL DEFAULT 'ACTIVE',
    policy_version             text NOT NULL,
    consent_version            text NULL,
    last_visibility_checked_at timestamptz NULL,
    created_at                 timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_source_resources_provider_id_non_empty
        CHECK (length(btrim(provider_resource_id)) > 0),
    CONSTRAINT ck_source_resources_type
        CHECK (resource_type IN ('GITHUB_REPOSITORY')),
    CONSTRAINT ck_source_resources_name_non_empty
        CHECK (length(btrim(name)) > 0),
    CONSTRAINT ck_source_resources_canonical_url_https
        CHECK (canonical_url ~ '^https://[^[:space:]@]+/.+'),
    CONSTRAINT ck_source_resources_visibility
        CHECK (visibility IN ('PUBLIC', 'PRIVATE', 'INTERNAL')),
    CONSTRAINT ck_source_resources_processing_mode
        CHECK (processing_mode IN ('PUBLIC_CLOUD', 'PRIVATE_LOCAL', 'PRIVATE_CLOUD_OPT_IN')),
    CONSTRAINT ck_source_resources_raw_storage_mode_none
        CHECK (raw_storage_mode = 'NONE'),
    CONSTRAINT ck_source_resources_raw_retention_null
        CHECK (raw_retention_seconds IS NULL),
    CONSTRAINT ck_source_resources_status
        CHECK (status IN ('ACTIVE', 'PAUSED', 'POLICY_REVIEW_REQUIRED', 'DELETED')),
    CONSTRAINT ck_source_resources_policy_version_non_empty
        CHECK (length(btrim(policy_version)) > 0),
    CONSTRAINT uq_source_resources_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_source_resources_account_provider_id
        UNIQUE (source_account_id, provider_resource_id),
    CONSTRAINT fk_source_resources_same_workspace_account
        FOREIGN KEY (workspace_id, source_account_id)
        REFERENCES public.source_accounts (workspace_id, id)
        ON DELETE CASCADE
);

ALTER TABLE public.source_resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_resources FORCE ROW LEVEL SECURITY;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'source_resources'
          AND 'cm_api_app' = ANY(roles)
    ) THEN
        CREATE POLICY source_resources_api_workspace_isolation
            ON public.source_resources
            FOR ALL
            TO cm_api_app
            USING (
                workspace_id = auth_ctx.current_workspace_id()
                AND EXISTS (
                    SELECT 1
                    FROM public.workspace_memberships m
                    WHERE m.workspace_id = source_resources.workspace_id
                      AND m.user_id = auth_ctx.current_user_id()
                      AND m.revoked_at IS NULL
                )
            )
            WITH CHECK (
                workspace_id = auth_ctx.current_workspace_id()
                AND EXISTS (
                    SELECT 1
                    FROM public.workspace_memberships m
                    WHERE m.workspace_id = source_resources.workspace_id
                      AND m.user_id = auth_ctx.current_user_id()
                      AND m.revoked_at IS NULL
                )
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'source_resources'
          AND 'cm_worker_app' = ANY(roles)
    ) THEN
        CREATE POLICY source_resources_worker_workspace_select
            ON public.source_resources
            FOR SELECT
            TO cm_worker_app
            USING (workspace_id = auth_ctx.current_workspace_id());

        CREATE POLICY source_resources_worker_workspace_update
            ON public.source_resources
            FOR UPDATE
            TO cm_worker_app
            USING (workspace_id = auth_ctx.current_workspace_id())
            WITH CHECK (workspace_id = auth_ctx.current_workspace_id());
    END IF;
END
$$;

GRANT SELECT, INSERT, UPDATE ON public.source_resources TO cm_api_app;
GRANT SELECT, UPDATE ON public.source_resources TO cm_worker_app;

-- =============================================================================
-- 4. SyncRuns & Checkpoints (CONTRACT@87f1515 + Snapshot Import §1-3)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.sync_runs (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    source_resource_id          uuid NOT NULL,
    plan_id                     uuid NOT NULL DEFAULT gen_random_uuid(),
    mode                        text NOT NULL,
    run_version                 integer NOT NULL DEFAULT 1,
    status                      text NOT NULL DEFAULT 'PENDING',
    processing_status           text NOT NULL DEFAULT 'NOT_STARTED',
    import_ready                boolean NOT NULL DEFAULT false,
    plan_spec                   jsonb NOT NULL DEFAULT '{}'::jsonb,
    retry_allowed               boolean NOT NULL DEFAULT false,
    failure_reason_code         text NULL,
    has_safe_checkpoint         boolean NOT NULL DEFAULT false,
    source_drift                boolean NOT NULL DEFAULT false,
    observed_at                 timestamptz NULL,
    processed_range             text NULL,
    remaining_range             text NULL,
    is_abandoned                boolean NOT NULL DEFAULT false,
    started_at                  timestamptz NULL,
    last_terminal_transition_at timestamptz NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    updated_at                  timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_sync_runs_mode
        CHECK (mode = 'INITIAL_BACKFILL'),
    CONSTRAINT ck_sync_runs_run_version_positive
        CHECK (run_version >= 1),
    CONSTRAINT ck_sync_runs_status
        CHECK (status IN (
            'PENDING', 'RUNNING', 'SUCCESS', 'PARTIAL',
            'PARTIAL_WITH_LIMIT', 'FAILED', 'CANCELED'
        )),
    CONSTRAINT ck_sync_runs_processing_status
        CHECK (processing_status IN (
            'NOT_STARTED', 'RUNNING', 'BLOCKED_POLICY',
            'BLOCKED_SOURCE', 'FAILED', 'COMPLETE'
        )),
    CONSTRAINT ck_sync_runs_plan_spec_object
        CHECK (jsonb_typeof(plan_spec) = 'object'),
    CONSTRAINT ck_sync_runs_plan_spec_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(plan_spec)),
    -- CONTRACT@87f1515 ImportStatus invariants:
    CONSTRAINT ck_sync_runs_import_ready_and_processing
        CHECK (
            (status = 'SUCCESS' AND import_ready = true)
            OR (status <> 'SUCCESS' AND import_ready = false AND processing_status = 'NOT_STARTED')
        ),
    CONSTRAINT ck_sync_runs_failure_reason_presence
        CHECK (
            (
                status IN ('PARTIAL', 'PARTIAL_WITH_LIMIT', 'FAILED')
                AND failure_reason_code IS NOT NULL
                AND length(btrim(failure_reason_code)) > 0
            )
            OR (
                status NOT IN ('PARTIAL', 'PARTIAL_WITH_LIMIT', 'FAILED')
                AND failure_reason_code IS NULL
            )
        ),
    CONSTRAINT ck_sync_runs_partial_with_limit_remaining_range
        CHECK (
            status <> 'PARTIAL_WITH_LIMIT'
            OR (remaining_range IS NOT NULL AND length(btrim(remaining_range)) > 0)
        ),
    CONSTRAINT ck_sync_runs_plan_drift_consistency
        CHECK (
            (failure_reason_code = 'PLAN_DRIFT' AND status = 'PARTIAL' AND source_drift = true)
            OR ((failure_reason_code IS NULL OR failure_reason_code <> 'PLAN_DRIFT') AND source_drift = false)
        ),
    CONSTRAINT ck_sync_runs_retry_allowed_invariants
        CHECK (
            NOT retry_allowed
            OR (
                status = 'PARTIAL'
                AND failure_reason_code = 'TRANSIENT_PROVIDER_ERROR'
                AND source_drift = false
                AND has_safe_checkpoint = true
                AND is_abandoned = false
            )
        ),
    CONSTRAINT uq_sync_runs_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_sync_runs_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE CASCADE
);

-- At most one active (PENDING or RUNNING) SyncRun per repository in a Workspace.
CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_runs_one_active_per_resource
    ON public.sync_runs (workspace_id, source_resource_id)
    WHERE status IN ('PENDING', 'RUNNING');

CREATE INDEX IF NOT EXISTS idx_sync_runs_resource_recovery
    ON public.sync_runs (
        workspace_id,
        source_resource_id,
        last_terminal_transition_at DESC NULLS LAST,
        created_at DESC
    );

CREATE OR REPLACE FUNCTION public.enforce_sync_run_invariants()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id THEN
        RAISE EXCEPTION 'sync_runs.workspace_id is immutable' USING ERRCODE = '23000';
    END IF;
    IF NEW.source_resource_id IS DISTINCT FROM OLD.source_resource_id THEN
        RAISE EXCEPTION 'sync_runs.source_resource_id is immutable' USING ERRCODE = '23000';
    END IF;
    IF NEW.plan_id IS DISTINCT FROM OLD.plan_id THEN
        RAISE EXCEPTION 'sync_runs.plan_id is immutable' USING ERRCODE = '23000';
    END IF;
    IF NEW.mode IS DISTINCT FROM OLD.mode THEN
        RAISE EXCEPTION 'sync_runs.mode is immutable' USING ERRCODE = '23000';
    END IF;
    IF NEW.run_version < OLD.run_version THEN
        RAISE EXCEPTION 'sync_runs.run_version must be monotonic' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_sync_runs_invariants ON public.sync_runs;
CREATE TRIGGER trg_sync_runs_invariants
    BEFORE UPDATE
    ON public.sync_runs
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_sync_run_invariants();

-- 4.2 sync_checkpoints
CREATE TABLE IF NOT EXISTS public.sync_checkpoints (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    sync_run_id         uuid NOT NULL,
    stream_name         text NOT NULL,
    stream_status       text NOT NULL DEFAULT 'PENDING',
    last_committed_page integer NULL,
    unresolved_failures integer NULL,
    checkpoint_version  integer NOT NULL DEFAULT 1,
    cursor_metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at          timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_sync_checkpoints_stream_name_non_empty
        CHECK (length(btrim(stream_name)) > 0),
    CONSTRAINT ck_sync_checkpoints_stream_status
        CHECK (stream_status IN (
            'PENDING', 'RUNNING', 'SUCCESS', 'PARTIAL',
            'PARTIAL_WITH_LIMIT', 'FAILED', 'CANCELED'
        )),
    CONSTRAINT ck_sync_checkpoints_page_non_negative
        CHECK (last_committed_page IS NULL OR last_committed_page >= 0),
    CONSTRAINT ck_sync_checkpoints_failures_non_negative
        CHECK (unresolved_failures IS NULL OR unresolved_failures >= 0),
    CONSTRAINT ck_sync_checkpoints_version_positive
        CHECK (checkpoint_version >= 1),
    CONSTRAINT ck_sync_checkpoints_cursor_object
        CHECK (jsonb_typeof(cursor_metadata) = 'object'),
    CONSTRAINT ck_sync_checkpoints_cursor_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(cursor_metadata)),
    CONSTRAINT uq_sync_checkpoints_run_stream
        UNIQUE (sync_run_id, stream_name),
    CONSTRAINT uq_sync_checkpoints_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_sync_checkpoints_same_workspace_run
        FOREIGN KEY (workspace_id, sync_run_id)
        REFERENCES public.sync_runs (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sync_checkpoints_workspace_run
    ON public.sync_checkpoints (workspace_id, sync_run_id);

-- =============================================================================
-- 5. 24-Hour Immutable API Idempotency Records (Non-Extending Replay Receipts)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.api_idempotency_records (
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    operation_scope     text NOT NULL,
    idempotency_key     text NOT NULL,
    request_fingerprint text NOT NULL,
    target_sync_run_id  uuid NOT NULL,
    response_status     integer NOT NULL,
    response_body       jsonb NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL,

    PRIMARY KEY (workspace_id, operation_scope, idempotency_key),

    CONSTRAINT ck_api_idempotency_operation_scope
        CHECK (operation_scope IN ('SYNC_RUN_CREATE', 'SYNC_RUN_RETRY')),
    CONSTRAINT ck_api_idempotency_key_uuid
        CHECK (idempotency_key ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
    CONSTRAINT ck_api_idempotency_fingerprint_sha256
        CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_api_idempotency_response_status_202
        CHECK (response_status = 202),
    CONSTRAINT ck_api_idempotency_response_body_object
        CHECK (jsonb_typeof(response_body) = 'object'),
    CONSTRAINT ck_api_idempotency_response_body_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(response_body)),
    CONSTRAINT ck_api_idempotency_24h_exact
        CHECK (expires_at = created_at + interval '24 hours'),
    CONSTRAINT fk_api_idempotency_same_workspace_run
        FOREIGN KEY (workspace_id, target_sync_run_id)
        REFERENCES public.sync_runs (workspace_id, id)
        ON DELETE CASCADE
);

CREATE OR REPLACE FUNCTION public.prevent_idempotency_record_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'api_idempotency_records rows are immutable; replay must never extend expires_at'
        USING ERRCODE = '23000';
END;
$$;

DROP TRIGGER IF EXISTS trg_api_idempotency_records_immutable ON public.api_idempotency_records;
CREATE TRIGGER trg_api_idempotency_records_immutable
    BEFORE UPDATE
    ON public.api_idempotency_records
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_idempotency_record_update();

-- =============================================================================
-- 6. Jobs & Logical Side-Effect Ledger (Data Model §3.10 + T-105A / REL-02)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.jobs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    job_type            text NOT NULL,
    target_sync_run_id  uuid NULL,
    dedupe_key          text NULL,
    input_hash          text NOT NULL,
    input_manifest      jsonb NOT NULL DEFAULT '{}'::jsonb,
    status              text NOT NULL DEFAULT 'PENDING',
    attempt             integer NOT NULL DEFAULT 0,
    max_attempts        integer NOT NULL DEFAULT 3,
    lease_owner         text NULL,
    lease_token         uuid NULL,
    lease_until         timestamptz NULL,
    next_run_at         timestamptz NOT NULL DEFAULT now(),
    checkpoint          jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_class         text NULL,
    error_code          text NULL,
    execution_parent_id uuid NULL,
    logical_effect_key  text NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    completed_at        timestamptz NULL,

    CONSTRAINT ck_jobs_job_type_non_empty
        CHECK (length(btrim(job_type)) > 0),
    CONSTRAINT ck_jobs_input_hash_sha256
        CHECK (input_hash ~ '^(sha256:)?[0-9a-f]{64}$'),
    CONSTRAINT ck_jobs_input_manifest_object
        CHECK (jsonb_typeof(input_manifest) = 'object'),
    CONSTRAINT ck_jobs_input_manifest_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(input_manifest)),
    CONSTRAINT ck_jobs_status
        CHECK (status IN (
            'PENDING', 'RUNNING', 'RETRY_WAIT',
            'SUCCEEDED', 'FAILED_TERMINAL', 'CANCELED'
        )),
    CONSTRAINT ck_jobs_attempt_bounds
        CHECK (attempt >= 0 AND max_attempts >= 1 AND attempt <= max_attempts),
    CONSTRAINT ck_jobs_lease_consistency
        CHECK (
            (
                status = 'RUNNING'
                AND lease_owner IS NOT NULL
                AND length(btrim(lease_owner)) > 0
                AND lease_token IS NOT NULL
                AND lease_until IS NOT NULL
            )
            OR (
                status <> 'RUNNING'
                AND lease_owner IS NULL
                AND lease_token IS NULL
                AND lease_until IS NULL
            )
        ),
    CONSTRAINT ck_jobs_checkpoint_object
        CHECK (jsonb_typeof(checkpoint) = 'object'),
    CONSTRAINT ck_jobs_checkpoint_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(checkpoint)),
    CONSTRAINT ck_jobs_error_class
        CHECK (
            error_class IS NULL
            OR error_class IN (
                'RETRYABLE',
                'NON_RETRYABLE',
                'POISON_BUDGET_EXHAUSTED',
                'CANCELED_BY_FENCE'
            )
        ),
    CONSTRAINT ck_jobs_terminal_error_consistency
        CHECK (
            status <> 'FAILED_TERMINAL'
            OR (
                error_class IN ('NON_RETRYABLE', 'POISON_BUDGET_EXHAUSTED')
                AND error_code IS NOT NULL
                AND length(btrim(error_code)) > 0
            )
        ),
    CONSTRAINT ck_jobs_completed_at_consistency
        CHECK (
            (status IN ('SUCCEEDED', 'FAILED_TERMINAL', 'CANCELED') AND completed_at IS NOT NULL)
            OR (status IN ('PENDING', 'RUNNING', 'RETRY_WAIT') AND completed_at IS NULL)
        ),
    CONSTRAINT ck_jobs_logical_effect_key_non_empty
        CHECK (length(btrim(logical_effect_key)) > 0),
    CONSTRAINT ck_jobs_no_self_parent
        CHECK (execution_parent_id IS NULL OR execution_parent_id <> id),
    CONSTRAINT uq_jobs_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_jobs_same_workspace_sync_run
        FOREIGN KEY (workspace_id, target_sync_run_id)
        REFERENCES public.sync_runs (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_jobs_same_workspace_parent
        FOREIGN KEY (workspace_id, execution_parent_id)
        REFERENCES public.jobs (workspace_id, id)
        ON DELETE RESTRICT
);

-- Prevents duplicate active or already-succeeded executions for the same logical effect.
-- Manual replay of a FAILED_TERMINAL job succeeds because the parent is FAILED_TERMINAL,
-- while a second concurrent/duplicate replay is blocked by this index.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_or_succeeded_logical_effect
    ON public.jobs (workspace_id, logical_effect_key)
    WHERE status IN ('PENDING', 'RUNNING', 'RETRY_WAIT', 'SUCCEEDED');

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_workspace_dedupe_key
    ON public.jobs (workspace_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_claimable
    ON public.jobs (workspace_id, next_run_at ASC, created_at ASC)
    WHERE status IN ('PENDING', 'RETRY_WAIT');

CREATE INDEX IF NOT EXISTS idx_jobs_running_lease_until
    ON public.jobs (workspace_id, lease_until ASC)
    WHERE status = 'RUNNING';

CREATE INDEX IF NOT EXISTS idx_jobs_workspace_sync_run
    ON public.jobs (workspace_id, target_sync_run_id)
    WHERE target_sync_run_id IS NOT NULL;

CREATE OR REPLACE FUNCTION public.enforce_job_state_transitions()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.job_type IS DISTINCT FROM OLD.job_type
       OR NEW.input_hash IS DISTINCT FROM OLD.input_hash
       OR NEW.logical_effect_key IS DISTINCT FROM OLD.logical_effect_key
       OR NEW.execution_parent_id IS DISTINCT FROM OLD.execution_parent_id THEN
        RAISE EXCEPTION 'Immutable job identity/lineage fields cannot be modified'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.status IN ('SUCCEEDED', 'FAILED_TERMINAL', 'CANCELED') THEN
        RAISE EXCEPTION 'Terminal job % (status=%) is immutable', OLD.id, OLD.status
            USING ERRCODE = '23000';
    END IF;

    IF OLD.status = 'PENDING' AND NEW.status NOT IN ('RUNNING', 'CANCELED') THEN
        RAISE EXCEPTION 'Invalid job transition from PENDING to %', NEW.status
            USING ERRCODE = '23000';
    END IF;

    IF OLD.status = 'RETRY_WAIT' AND NEW.status NOT IN ('RUNNING', 'CANCELED') THEN
        RAISE EXCEPTION 'Invalid job transition from RETRY_WAIT to %', NEW.status
            USING ERRCODE = '23000';
    END IF;

    IF OLD.status = 'RUNNING' AND NEW.status NOT IN (
        'RUNNING', 'RETRY_WAIT', 'SUCCEEDED', 'FAILED_TERMINAL', 'CANCELED'
    ) THEN
        RAISE EXCEPTION 'Invalid job transition from RUNNING to %', NEW.status
            USING ERRCODE = '23000';
    END IF;

    IF NEW.attempt < OLD.attempt THEN
        RAISE EXCEPTION 'jobs.attempt must be monotonic' USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_jobs_state_transitions ON public.jobs;
CREATE TRIGGER trg_jobs_state_transitions
    BEFORE UPDATE
    ON public.jobs
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_job_state_transitions();

-- 6.2 job_side_effect_ledger (Strictly 1-time logical side-effect guarantee)
CREATE TABLE IF NOT EXISTS public.job_side_effect_ledger (
    workspace_id       uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    logical_effect_key text NOT NULL,
    job_id             uuid NOT NULL,
    effect_metadata    jsonb NOT NULL DEFAULT '{}'::jsonb,
    committed_at       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (workspace_id, logical_effect_key),

    CONSTRAINT ck_job_side_effect_key_non_empty
        CHECK (length(btrim(logical_effect_key)) > 0),
    CONSTRAINT ck_job_side_effect_metadata_object
        CHECK (jsonb_typeof(effect_metadata) = 'object'),
    CONSTRAINT ck_job_side_effect_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(effect_metadata)),
    CONSTRAINT fk_job_side_effect_ledger_same_workspace_job
        FOREIGN KEY (workspace_id, job_id)
        REFERENCES public.jobs (workspace_id, id)
        ON DELETE RESTRICT
);

-- =============================================================================
-- 7. Outbox Events & Consumer Idempotency Ledger (Data Model §3.10 + T-105A)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.outbox_events (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    event_type      text NOT NULL,
    aggregate_type  text NOT NULL,
    aggregate_id    uuid NOT NULL,
    dedupe_key      text NOT NULL,
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    status          text NOT NULL DEFAULT 'PENDING',
    attempt         integer NOT NULL DEFAULT 0,
    max_attempts    integer NOT NULL DEFAULT 5,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    last_error_code text NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    published_at    timestamptz NULL,

    CONSTRAINT ck_outbox_event_type_non_empty
        CHECK (length(btrim(event_type)) > 0),
    CONSTRAINT ck_outbox_aggregate_type_non_empty
        CHECK (length(btrim(aggregate_type)) > 0),
    CONSTRAINT ck_outbox_dedupe_key_non_empty
        CHECK (length(btrim(dedupe_key)) > 0),
    CONSTRAINT ck_outbox_payload_object
        CHECK (jsonb_typeof(payload) = 'object'),
    CONSTRAINT ck_outbox_payload_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(payload)),
    CONSTRAINT ck_outbox_status
        CHECK (status IN ('PENDING', 'PUBLISHED', 'FAILED_TERMINAL')),
    CONSTRAINT ck_outbox_attempt_bounds
        CHECK (attempt >= 0 AND max_attempts >= 1 AND attempt <= max_attempts),
    CONSTRAINT ck_outbox_published_at_consistency
        CHECK (
            (status = 'PUBLISHED' AND published_at IS NOT NULL)
            OR (status <> 'PUBLISHED' AND published_at IS NULL)
        ),
    CONSTRAINT uq_outbox_events_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_outbox_events_workspace_dedupe_key
        UNIQUE (workspace_id, dedupe_key)
);

-- At most one IMPORT_READY event per SyncRun across the entire lifecycle.
CREATE UNIQUE INDEX IF NOT EXISTS uq_outbox_events_single_import_ready_per_run
    ON public.outbox_events (workspace_id, aggregate_id)
    WHERE event_type = 'IMPORT_READY' AND aggregate_type = 'SYNC_RUN';

CREATE INDEX IF NOT EXISTS idx_outbox_events_pending_dispatch
    ON public.outbox_events (workspace_id, next_attempt_at ASC, created_at ASC)
    WHERE status = 'PENDING';

CREATE OR REPLACE FUNCTION public.enforce_outbox_state_transitions()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id
       OR NEW.dedupe_key IS DISTINCT FROM OLD.dedupe_key
       OR NEW.event_type IS DISTINCT FROM OLD.event_type THEN
        RAISE EXCEPTION 'Immutable outbox identity fields cannot be modified'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.status IN ('PUBLISHED', 'FAILED_TERMINAL') THEN
        RAISE EXCEPTION 'Terminal outbox_event % (status=%) is immutable', OLD.id, OLD.status
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_outbox_events_state_transitions ON public.outbox_events;
CREATE TRIGGER trg_outbox_events_state_transitions
    BEFORE UPDATE
    ON public.outbox_events
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_outbox_state_transitions();

-- 7.2 outbox_consumed_effects (Consumer-side 1-time effect guarantee)
CREATE TABLE IF NOT EXISTS public.outbox_consumed_effects (
    workspace_id    uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    consumer_name   text NOT NULL,
    dedupe_key      text NOT NULL,
    outbox_event_id uuid NOT NULL,
    consumed_at     timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (workspace_id, consumer_name, dedupe_key),

    CONSTRAINT ck_outbox_consumed_consumer_non_empty
        CHECK (length(btrim(consumer_name)) > 0),
    CONSTRAINT ck_outbox_consumed_dedupe_non_empty
        CHECK (length(btrim(dedupe_key)) > 0),
    CONSTRAINT fk_outbox_consumed_same_workspace_event
        FOREIGN KEY (workspace_id, outbox_event_id)
        REFERENCES public.outbox_events (workspace_id, id)
        ON DELETE RESTRICT
);

-- =============================================================================
-- 8. Audit Events & Deletion Jobs (Data Model §3.10)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.audit_events (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    actor_type        text NOT NULL,
    actor_id          uuid NULL,
    action            text NOT NULL,
    target_type       text NOT NULL,
    target_id         uuid NOT NULL,
    redacted_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_audit_events_actor_type
        CHECK (actor_type IN ('USER', 'SYSTEM', 'WORKER', 'OPERATOR')),
    CONSTRAINT ck_audit_events_action_non_empty
        CHECK (length(btrim(action)) > 0),
    CONSTRAINT ck_audit_events_target_type_non_empty
        CHECK (length(btrim(target_type)) > 0),
    CONSTRAINT ck_audit_events_metadata_object
        CHECK (jsonb_typeof(redacted_metadata) = 'object'),
    CONSTRAINT ck_audit_events_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(redacted_metadata)),
    CONSTRAINT ck_audit_events_metadata_no_secret
        CHECK (NOT public.cm_jsonb_contains_forbidden_secret_key(redacted_metadata))
);

CREATE INDEX IF NOT EXISTS idx_audit_events_workspace_target
    ON public.audit_events (workspace_id, target_type, target_id, created_at DESC);

CREATE OR REPLACE FUNCTION public.prevent_audit_event_update()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'audit_events rows are append-only' USING ERRCODE = '23000';
END;
$$;

DROP TRIGGER IF EXISTS trg_audit_events_append_only ON public.audit_events;
CREATE TRIGGER trg_audit_events_append_only
    BEFORE UPDATE
    ON public.audit_events
    FOR EACH ROW
    EXECUTE FUNCTION public.prevent_audit_event_update();

CREATE TABLE IF NOT EXISTS public.deletion_jobs (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id       uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    source_resource_id uuid NOT NULL,
    status             text NOT NULL DEFAULT 'PENDING',
    checkpoint         jsonb NOT NULL DEFAULT '{}'::jsonb,
    failure_summary    jsonb NOT NULL DEFAULT '{}'::jsonb,
    requested_by       uuid NULL,
    requested_at       timestamptz NOT NULL DEFAULT now(),
    completed_at       timestamptz NULL,

    CONSTRAINT ck_deletion_jobs_status
        CHECK (status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT ck_deletion_jobs_checkpoint_object
        CHECK (jsonb_typeof(checkpoint) = 'object'),
    CONSTRAINT ck_deletion_jobs_checkpoint_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(checkpoint)),
    CONSTRAINT ck_deletion_jobs_failure_summary_object
        CHECK (jsonb_typeof(failure_summary) = 'object'),
    CONSTRAINT ck_deletion_jobs_failure_summary_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(failure_summary)),
    CONSTRAINT ck_deletion_jobs_completed_at_consistency
        CHECK (
            (status IN ('SUCCEEDED', 'FAILED') AND completed_at IS NOT NULL)
            OR (status IN ('PENDING', 'RUNNING', 'PARTIAL') AND completed_at IS NULL)
        ),
    CONSTRAINT uq_deletion_jobs_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_deletion_jobs_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_deletion_jobs_active_fence
    ON public.deletion_jobs (workspace_id, source_resource_id)
    WHERE status IN ('PENDING', 'RUNNING', 'PARTIAL', 'SUCCEEDED');

-- =============================================================================
-- 9. ENABLE + FORCE Row Level Security & Fail-Closed Workspace Policies
-- =============================================================================
ALTER TABLE public.sync_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_runs FORCE ROW LEVEL SECURITY;

ALTER TABLE public.sync_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sync_checkpoints FORCE ROW LEVEL SECURITY;

ALTER TABLE public.api_idempotency_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.api_idempotency_records FORCE ROW LEVEL SECURITY;

ALTER TABLE public.jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.jobs FORCE ROW LEVEL SECURITY;

ALTER TABLE public.job_side_effect_ledger ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.job_side_effect_ledger FORCE ROW LEVEL SECURITY;

ALTER TABLE public.outbox_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outbox_events FORCE ROW LEVEL SECURITY;

ALTER TABLE public.outbox_consumed_effects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.outbox_consumed_effects FORCE ROW LEVEL SECURITY;

ALTER TABLE public.audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.audit_events FORCE ROW LEVEL SECURITY;

ALTER TABLE public.deletion_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.deletion_jobs FORCE ROW LEVEL SECURITY;

-- sync_runs
DROP POLICY IF EXISTS sync_runs_api_isolation ON public.sync_runs;
CREATE POLICY sync_runs_api_isolation
    ON public.sync_runs
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = sync_runs.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = sync_runs.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS sync_runs_worker_isolation ON public.sync_runs;
CREATE POLICY sync_runs_worker_isolation
    ON public.sync_runs
    FOR ALL
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- sync_checkpoints
DROP POLICY IF EXISTS sync_checkpoints_api_isolation ON public.sync_checkpoints;
CREATE POLICY sync_checkpoints_api_isolation
    ON public.sync_checkpoints
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = sync_checkpoints.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = sync_checkpoints.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS sync_checkpoints_worker_isolation ON public.sync_checkpoints;
CREATE POLICY sync_checkpoints_worker_isolation
    ON public.sync_checkpoints
    FOR ALL
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- api_idempotency_records (API-only; worker has zero access)
DROP POLICY IF EXISTS api_idempotency_records_api_isolation ON public.api_idempotency_records;
CREATE POLICY api_idempotency_records_api_isolation
    ON public.api_idempotency_records
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = api_idempotency_records.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = api_idempotency_records.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

-- jobs
DROP POLICY IF EXISTS jobs_api_isolation ON public.jobs;
CREATE POLICY jobs_api_isolation
    ON public.jobs
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = jobs.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = jobs.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS jobs_worker_isolation ON public.jobs;
CREATE POLICY jobs_worker_isolation
    ON public.jobs
    FOR ALL
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- job_side_effect_ledger
DROP POLICY IF EXISTS job_side_effect_ledger_api_select ON public.job_side_effect_ledger;
CREATE POLICY job_side_effect_ledger_api_select
    ON public.job_side_effect_ledger
    FOR SELECT
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = job_side_effect_ledger.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS job_side_effect_ledger_worker_isolation ON public.job_side_effect_ledger;
CREATE POLICY job_side_effect_ledger_worker_isolation
    ON public.job_side_effect_ledger
    FOR ALL
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- outbox_events
DROP POLICY IF EXISTS outbox_events_api_isolation ON public.outbox_events;
CREATE POLICY outbox_events_api_isolation
    ON public.outbox_events
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = outbox_events.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = outbox_events.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS outbox_events_worker_isolation ON public.outbox_events;
CREATE POLICY outbox_events_worker_isolation
    ON public.outbox_events
    FOR ALL
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- outbox_consumed_effects
DROP POLICY IF EXISTS outbox_consumed_effects_api_select ON public.outbox_consumed_effects;
CREATE POLICY outbox_consumed_effects_api_select
    ON public.outbox_consumed_effects
    FOR SELECT
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = outbox_consumed_effects.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS outbox_consumed_effects_worker_isolation ON public.outbox_consumed_effects;
CREATE POLICY outbox_consumed_effects_worker_isolation
    ON public.outbox_consumed_effects
    FOR ALL
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- audit_events
DROP POLICY IF EXISTS audit_events_api_isolation ON public.audit_events;
CREATE POLICY audit_events_api_isolation
    ON public.audit_events
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = audit_events.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = audit_events.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS audit_events_worker_isolation ON public.audit_events;
CREATE POLICY audit_events_worker_isolation
    ON public.audit_events
    FOR ALL
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- deletion_jobs
DROP POLICY IF EXISTS deletion_jobs_api_isolation ON public.deletion_jobs;
CREATE POLICY deletion_jobs_api_isolation
    ON public.deletion_jobs
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = deletion_jobs.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = deletion_jobs.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS deletion_jobs_worker_isolation ON public.deletion_jobs;
CREATE POLICY deletion_jobs_worker_isolation
    ON public.deletion_jobs
    FOR ALL
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- =============================================================================
-- 10. Least-Privilege GRANT Matrix (scoped strictly to 0040 tables)
-- =============================================================================
REVOKE ALL ON TABLE
    public.sync_runs,
    public.sync_checkpoints,
    public.api_idempotency_records,
    public.jobs,
    public.job_side_effect_ledger,
    public.outbox_events,
    public.outbox_consumed_effects,
    public.audit_events,
    public.deletion_jobs
FROM PUBLIC, cm_api_app, cm_worker_app;

-- cm_api_app:
--   * No DELETE on jobs, outbox_events, audit_events, api_idempotency_records
--   * No UPDATE on api_idempotency_records or audit_events (immutability enforced by DB privileges)
GRANT SELECT, INSERT, UPDATE ON public.sync_runs TO cm_api_app;
GRANT SELECT, INSERT, UPDATE ON public.sync_checkpoints TO cm_api_app;
GRANT SELECT, INSERT ON public.api_idempotency_records TO cm_api_app;
GRANT SELECT, INSERT, UPDATE ON public.jobs TO cm_api_app;
GRANT SELECT ON public.job_side_effect_ledger TO cm_api_app;
GRANT SELECT, INSERT ON public.outbox_events TO cm_api_app;
GRANT SELECT ON public.outbox_consumed_effects TO cm_api_app;
GRANT SELECT, INSERT ON public.audit_events TO cm_api_app;
GRANT SELECT, INSERT, UPDATE ON public.deletion_jobs TO cm_api_app;

-- cm_worker_app:
--   * Zero access to api_idempotency_records (and users/workspaces/workspace_memberships)
--   * No DELETE on any table (poison FAILED_TERMINAL rows and audit logs permanently preserved)
GRANT SELECT, UPDATE ON public.sync_runs TO cm_worker_app;
GRANT SELECT, INSERT, UPDATE ON public.sync_checkpoints TO cm_worker_app;
GRANT SELECT, INSERT, UPDATE ON public.jobs TO cm_worker_app;
GRANT SELECT, INSERT ON public.job_side_effect_ledger TO cm_worker_app;
GRANT SELECT, INSERT, UPDATE ON public.outbox_events TO cm_worker_app;
GRANT SELECT, INSERT ON public.outbox_consumed_effects TO cm_worker_app;
GRANT SELECT, INSERT ON public.audit_events TO cm_worker_app;
GRANT SELECT, INSERT, UPDATE ON public.deletion_jobs TO cm_worker_app;

RESET ROLE;
