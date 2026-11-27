-- Migration 0020: Source & Provenance Metadata-Only Schema (T-102, Plan C / 방안 C)
--
-- Design guarantees:
-- 1. Standalone + 0010 (M1-T101) sequential compatibility:
--    - Reuses NOLOGIN NOBYPASSRLS roles (cm_migrator, cm_api_app, cm_worker_app).
--    - Defines auth_ctx.current_workspace_id(), auth_ctx.current_user_id() (aligned
--      with 0010), compatibility alias auth_ctx.current_subject_id(), and
--      auth_ctx.allow_revision_purge().
--    - Fallback IF NOT EXISTS definitions for users, workspaces, workspace_memberships,
--      and source_accounts match 0010 column/PK structures and preserve any policies
--      already created by 0010.
-- 2. Plan C (방안 C) raw-text non-persistence enforced at the DB constraint layer:
--    - source_accounts.token_secret_ref forbids raw token prefixes (ghp_/gho_/github_pat_/bearer).
--    - source_resources.raw_storage_mode = 'NONE' and raw_retention_seconds IS NULL.
--    - content_units.text_content IS NULL, content_ref IS NULL,
--      retention_class = 'NONE', expires_at IS NULL.
--    - JSONB columns (normalized_metadata, anchor) reject forbidden raw-text keys
--      at both top-level and arbitrarily nested objects/arrays.
-- 3. DATA-01 revision immutability via BEFORE UPDATE OR DELETE trigger on
--    source_object_revisions (DELETE permitted only under explicit transaction-local
--    purge context `SET LOCAL app.allow_revision_purge = 'on'`).
-- 4. Multi-tenant isolation via NOBYPASSRLS runtime roles, ENABLE + FORCE ROW LEVEL
--    SECURITY, fail-closed auth_ctx.current_workspace_id(), and Composite Foreign Keys
--    (workspace_id, parent_id) -> parent(workspace_id, id) across every parent-child pair.

-- =============================================================================
-- 1. Idempotent Role & Schema Bootstrap (1:1 compatible with 0010_*)
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
-- 2. Fail-Closed Transaction-Local Context Helpers & Plan C JSONB Guard
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

-- Compatibility alias for callers referencing current_subject_id().
CREATE OR REPLACE FUNCTION auth_ctx.current_subject_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(
        auth_ctx.current_user_id(),
        CASE
            WHEN COALESCE(current_setting('app.current_subject_id', true), '')
                 ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            THEN current_setting('app.current_subject_id', true)::uuid
            ELSE NULL
        END
    );
$$;

CREATE OR REPLACE FUNCTION auth_ctx.allow_revision_purge()
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(current_setting('app.allow_revision_purge', true), '') = 'on';
$$;

REVOKE ALL ON FUNCTION auth_ctx.current_workspace_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_ctx.current_user_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_ctx.current_subject_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_ctx.allow_revision_purge() FROM PUBLIC;

GRANT EXECUTE ON FUNCTION auth_ctx.current_workspace_id() TO cm_api_app, cm_worker_app;
GRANT EXECUTE ON FUNCTION auth_ctx.current_user_id() TO cm_api_app;
GRANT EXECUTE ON FUNCTION auth_ctx.current_subject_id() TO cm_api_app;
GRANT EXECUTE ON FUNCTION auth_ctx.allow_revision_purge() TO cm_api_app, cm_worker_app;

-- Recursive Plan C (REF-04) forbidden raw-text JSONB key detector.
-- Used directly in CHECK constraints so no raw source body, diff, quote, or prompt
-- can be smuggled into JSONB metadata at top level or inside nested objects/arrays.
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

REVOKE ALL ON FUNCTION public.cm_jsonb_contains_forbidden_raw_key(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cm_jsonb_contains_forbidden_raw_key(jsonb) TO cm_api_app, cm_worker_app;

-- =============================================================================
-- 3. Minimal Base Tenant Tables (0010-aligned IF NOT EXISTS fallback)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.users (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    auth_issuer  text NOT NULL DEFAULT 'local' CHECK (btrim(auth_issuer) <> ''),
    auth_subject text NOT NULL DEFAULT gen_random_uuid()::text CHECK (btrim(auth_subject) <> ''),
    email        text NULL,
    name         text NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_auth_issuer_subject UNIQUE (auth_issuer, auth_subject)
);

CREATE TABLE IF NOT EXISTS public.workspaces (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_user_id uuid NULL REFERENCES public.users (id),
    name          text NOT NULL DEFAULT 'default' CHECK (btrim(name) <> ''),
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

ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users FORCE ROW LEVEL SECURITY;

ALTER TABLE public.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workspaces FORCE ROW LEVEL SECURITY;

ALTER TABLE public.workspace_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workspace_memberships FORCE ROW LEVEL SECURITY;

-- Fallback SELECT policy on workspaces for cm_api_app only when 0010 did not define one.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'workspaces'
          AND 'cm_api_app' = ANY(roles)
    ) THEN
        CREATE POLICY workspaces_api_select
            ON public.workspaces
            FOR SELECT
            TO cm_api_app
            USING (id = auth_ctx.current_workspace_id());
    END IF;
END
$$;

-- =============================================================================
-- 4. Source & Plan C Provenance Metadata Tables (Data Model §3.2 - §3.4)
-- =============================================================================

-- 4.1 source_accounts (0010-compatible + raw token prefix guard)
CREATE TABLE IF NOT EXISTS public.source_accounts (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    provider            text NOT NULL,
    external_account_id text NOT NULL,
    token_secret_ref    text NOT NULL,
    scopes              text[] NOT NULL DEFAULT ARRAY[]::text[],
    status              text NOT NULL DEFAULT 'ACTIVE',
    created_at          timestamptz NOT NULL DEFAULT now(),
    revoked_at          timestamptz NULL,

    CONSTRAINT ck_source_accounts_provider
        CHECK (provider IN ('GITHUB')),
    CONSTRAINT ck_source_accounts_external_id_non_empty
        CHECK (length(btrim(external_account_id)) > 0),
    CONSTRAINT ck_source_accounts_token_secret_ref_format
        CHECK (
            length(btrim(token_secret_ref)) BETWEEN 1 AND 255
            AND token_secret_ref ~ '^[a-zA-Z0-9._:/-]+$'
            AND token_secret_ref !~* '^(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|bearer )'
        ),
    CONSTRAINT ck_source_accounts_status
        CHECK (status IN ('ACTIVE', 'REVOKED')),
    CONSTRAINT chk_source_accounts_status_revoked
        CHECK (
            (status = 'REVOKED' AND revoked_at IS NOT NULL)
            OR (status = 'ACTIVE' AND revoked_at IS NULL)
        ),
    CONSTRAINT uq_source_accounts_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_source_accounts_workspace_provider_external
        UNIQUE (workspace_id, provider, external_account_id)
);

-- Ensure raw-token prefix guard and default id exist even when 0010 created source_accounts first.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.source_accounts'::regclass
          AND conname IN (
              'ck_source_accounts_token_secret_ref_format',
              'ck_source_accounts_no_raw_token_prefix'
          )
    ) THEN
        ALTER TABLE public.source_accounts
            ADD CONSTRAINT ck_source_accounts_no_raw_token_prefix
            CHECK (
                length(btrim(token_secret_ref)) BETWEEN 1 AND 255
                AND token_secret_ref ~ '^[a-zA-Z0-9._:/-]+$'
                AND token_secret_ref !~* '^(ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|bearer )'
            );
    END IF;
END
$$;

-- 4.2 source_resources
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
    -- Plan C (방안 C) MVP absolute invariants:
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

-- 4.3 source_objects
CREATE TABLE IF NOT EXISTS public.source_objects (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id         uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    source_resource_id   uuid NOT NULL,
    object_type          text NOT NULL,
    provider_object_id   text NOT NULL,
    lifecycle_state      text NOT NULL DEFAULT 'ACTIVE',
    pr_association_state text NULL,
    first_seen_at        timestamptz NOT NULL DEFAULT now(),
    last_seen_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_source_objects_type
        CHECK (object_type IN ('PR', 'COMMIT', 'ISSUE', 'REVIEW', 'COMMENT')),
    CONSTRAINT ck_source_objects_provider_id_non_empty
        CHECK (length(btrim(provider_object_id)) > 0),
    CONSTRAINT ck_source_objects_lifecycle_state
        CHECK (lifecycle_state IN ('ACTIVE', 'DELETED', 'ORPHANED', 'POLICY_REVIEW_REQUIRED')),
    CONSTRAINT ck_source_objects_pr_association_state
        CHECK (
            pr_association_state IS NULL
            OR pr_association_state IN ('MATCHED_PR', 'NO_MATCH_OBSERVED', 'UNKNOWN')
        ),
    CONSTRAINT ck_source_objects_seen_order
        CHECK (last_seen_at >= first_seen_at),
    CONSTRAINT uq_source_objects_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_source_objects_resource_type_provider_id
        UNIQUE (source_resource_id, object_type, provider_object_id),
    CONSTRAINT fk_source_objects_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE CASCADE
);

-- 4.4 source_object_revisions (Immutable Revision - DATA-01)
CREATE TABLE IF NOT EXISTS public.source_object_revisions (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id         uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    source_object_id     uuid NOT NULL,
    provider_revision_id text NULL,
    content_hash         text NOT NULL,
    hash_policy_version  text NOT NULL,
    observed_at          timestamptz NOT NULL DEFAULT now(),
    provider_updated_at  timestamptz NULL,
    is_tombstone         boolean NOT NULL DEFAULT false,
    normalized_metadata  jsonb NOT NULL DEFAULT '{}'::jsonb,
    connector_version    text NOT NULL,
    schema_version       text NOT NULL,

    CONSTRAINT ck_source_object_revisions_content_hash_format
        CHECK (content_hash ~ '^(sha256:)?[0-9a-f]{64}$'),
    CONSTRAINT ck_source_object_revisions_hash_policy_non_empty
        CHECK (length(btrim(hash_policy_version)) > 0),
    CONSTRAINT ck_source_object_revisions_connector_version_non_empty
        CHECK (length(btrim(connector_version)) > 0),
    CONSTRAINT ck_source_object_revisions_schema_version_non_empty
        CHECK (length(btrim(schema_version)) > 0),
    CONSTRAINT ck_source_object_revisions_metadata_object
        CHECK (jsonb_typeof(normalized_metadata) = 'object'),
    CONSTRAINT ck_source_object_revisions_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(normalized_metadata)),
    CONSTRAINT uq_source_object_revisions_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_source_object_revisions_object_content_hash
        UNIQUE (source_object_id, content_hash),
    CONSTRAINT fk_source_object_revisions_same_workspace_object
        FOREIGN KEY (workspace_id, source_object_id)
        REFERENCES public.source_objects (workspace_id, id)
        ON DELETE CASCADE
);

-- DATA-01 Immutability Trigger:
-- Revisions are append-only historical snapshots. UPDATE is always rejected so a
-- historical revision ID can never be overwritten with a new hash or timestamp.
-- DELETE is rejected unless the caller explicitly enables transaction-local purge
-- context (`SET LOCAL app.allow_revision_purge = 'on'`) inside a deletion/purge job.
CREATE OR REPLACE FUNCTION public.enforce_source_object_revision_immutability()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'source_object_revisions rows are immutable (DATA-01): UPDATE is prohibited on revision %',
            OLD.id
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF NOT auth_ctx.allow_revision_purge() THEN
            RAISE EXCEPTION
                'source_object_revisions DELETE requires explicit purge context (app.allow_revision_purge = on) for revision %',
                OLD.id
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_source_object_revisions_immutability
    ON public.source_object_revisions;

CREATE TRIGGER trg_source_object_revisions_immutability
    BEFORE UPDATE OR DELETE
    ON public.source_object_revisions
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_source_object_revision_immutability();

-- 4.5 content_units (Plan C Raw Non-Persistence - DATA-03, REF-04)
CREATE TABLE IF NOT EXISTS public.content_units (
    id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id              uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    source_object_revision_id uuid NOT NULL,
    unit_type                 text NOT NULL,
    anchor                    jsonb NOT NULL,
    text_content              text NULL,
    content_ref               text NULL,
    content_hash              text NOT NULL,
    canonicalization_version  text NOT NULL,
    retention_class           text NOT NULL DEFAULT 'NONE',
    expires_at                timestamptz NULL,
    language                  text NULL,
    ordinal                   integer NOT NULL,
    policy_state              text NOT NULL DEFAULT 'ACTIVE',
    created_at                timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_content_units_unit_type
        CHECK (
            unit_type IN (
                'PR_TITLE',
                'PR_BODY_SECTION',
                'ISSUE_SECTION',
                'COMMENT',
                'REVIEW_COMMENT',
                'COMMIT_MESSAGE',
                'DIFF_HUNK',
                'SYMBOL_CHANGE'
            )
        ),
    CONSTRAINT ck_content_units_anchor_object
        CHECK (jsonb_typeof(anchor) = 'object'),
    CONSTRAINT ck_content_units_anchor_parser_version
        CHECK (
            anchor ? 'parser_version'
            AND jsonb_typeof(anchor -> 'parser_version') = 'string'
            AND length(btrim(anchor ->> 'parser_version')) > 0
        ),
    CONSTRAINT ck_content_units_anchor_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(anchor)),
    -- Plan C (방안 C) MVP absolute invariants:
    CONSTRAINT ck_content_units_text_content_null
        CHECK (text_content IS NULL),
    CONSTRAINT ck_content_units_content_ref_null
        CHECK (content_ref IS NULL),
    CONSTRAINT ck_content_units_retention_class_none
        CHECK (retention_class = 'NONE'),
    CONSTRAINT ck_content_units_expires_at_null
        CHECK (expires_at IS NULL),
    CONSTRAINT ck_content_units_content_hash_format
        CHECK (content_hash ~ '^(sha256:)?[0-9a-f]{64}$'),
    CONSTRAINT ck_content_units_canonicalization_version_non_empty
        CHECK (length(btrim(canonicalization_version)) > 0),
    CONSTRAINT ck_content_units_ordinal_non_negative
        CHECK (ordinal >= 0),
    CONSTRAINT ck_content_units_policy_state
        CHECK (policy_state IN ('ACTIVE', 'REDACTED', 'PURGED', 'BLOCKED')),
    CONSTRAINT uq_content_units_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_content_units_revision_ordinal
        UNIQUE (source_object_revision_id, ordinal),
    CONSTRAINT fk_content_units_same_workspace_revision
        FOREIGN KEY (workspace_id, source_object_revision_id)
        REFERENCES public.source_object_revisions (workspace_id, id)
        ON DELETE CASCADE
);

-- =============================================================================
-- 5. Supporting Indexes
-- =============================================================================
CREATE INDEX IF NOT EXISTS idx_source_resources_workspace_account
    ON public.source_resources (workspace_id, source_account_id);

CREATE INDEX IF NOT EXISTS idx_source_objects_workspace_resource
    ON public.source_objects (workspace_id, source_resource_id);

CREATE INDEX IF NOT EXISTS idx_source_object_revisions_workspace_object
    ON public.source_object_revisions (workspace_id, source_object_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_content_units_workspace_revision
    ON public.content_units (workspace_id, source_object_revision_id);

-- =============================================================================
-- 6. Enable & FORCE Row Level Security + Fail-Closed Workspace Policies
-- =============================================================================
ALTER TABLE public.source_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_accounts FORCE ROW LEVEL SECURITY;

ALTER TABLE public.source_resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_resources FORCE ROW LEVEL SECURITY;

ALTER TABLE public.source_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_objects FORCE ROW LEVEL SECURITY;

ALTER TABLE public.source_object_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_object_revisions FORCE ROW LEVEL SECURITY;

ALTER TABLE public.content_units ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_units FORCE ROW LEVEL SECURITY;

-- source_accounts: preserve stricter 0010 policies (live active membership check) if present.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'source_accounts'
          AND 'cm_api_app' = ANY(roles)
    ) THEN
        CREATE POLICY source_accounts_api_workspace_isolation
            ON public.source_accounts
            FOR ALL
            TO cm_api_app
            USING (workspace_id = auth_ctx.current_workspace_id())
            WITH CHECK (workspace_id = auth_ctx.current_workspace_id());
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_policies
        WHERE schemaname = 'public'
          AND tablename = 'source_accounts'
          AND 'cm_worker_app' = ANY(roles)
    ) THEN
        CREATE POLICY source_accounts_worker_workspace_select
            ON public.source_accounts
            FOR SELECT
            TO cm_worker_app
            USING (workspace_id = auth_ctx.current_workspace_id());
    END IF;
END
$$;

-- source_resources
DROP POLICY IF EXISTS source_resources_api_workspace_isolation ON public.source_resources;
CREATE POLICY source_resources_api_workspace_isolation
    ON public.source_resources
    FOR ALL
    TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS source_resources_worker_workspace_select ON public.source_resources;
CREATE POLICY source_resources_worker_workspace_select
    ON public.source_resources
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS source_resources_worker_workspace_update ON public.source_resources;
CREATE POLICY source_resources_worker_workspace_update
    ON public.source_resources
    FOR UPDATE
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- source_objects
DROP POLICY IF EXISTS source_objects_api_workspace_isolation ON public.source_objects;
CREATE POLICY source_objects_api_workspace_isolation
    ON public.source_objects
    FOR ALL
    TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS source_objects_worker_workspace_select ON public.source_objects;
CREATE POLICY source_objects_worker_workspace_select
    ON public.source_objects
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS source_objects_worker_workspace_insert ON public.source_objects;
CREATE POLICY source_objects_worker_workspace_insert
    ON public.source_objects
    FOR INSERT
    TO cm_worker_app
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS source_objects_worker_workspace_update ON public.source_objects;
CREATE POLICY source_objects_worker_workspace_update
    ON public.source_objects
    FOR UPDATE
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- source_object_revisions
DROP POLICY IF EXISTS source_object_revisions_api_workspace_isolation ON public.source_object_revisions;
CREATE POLICY source_object_revisions_api_workspace_isolation
    ON public.source_object_revisions
    FOR ALL
    TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS source_object_revisions_worker_workspace_select ON public.source_object_revisions;
CREATE POLICY source_object_revisions_worker_workspace_select
    ON public.source_object_revisions
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS source_object_revisions_worker_workspace_insert ON public.source_object_revisions;
CREATE POLICY source_object_revisions_worker_workspace_insert
    ON public.source_object_revisions
    FOR INSERT
    TO cm_worker_app
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- content_units
DROP POLICY IF EXISTS content_units_api_workspace_isolation ON public.content_units;
CREATE POLICY content_units_api_workspace_isolation
    ON public.content_units
    FOR ALL
    TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS content_units_worker_workspace_select ON public.content_units;
CREATE POLICY content_units_worker_workspace_select
    ON public.content_units
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS content_units_worker_workspace_insert ON public.content_units;
CREATE POLICY content_units_worker_workspace_insert
    ON public.content_units
    FOR INSERT
    TO cm_worker_app
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS content_units_worker_workspace_update ON public.content_units;
CREATE POLICY content_units_worker_workspace_update
    ON public.content_units
    FOR UPDATE
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- =============================================================================
-- 7. Minimal Privilege GRANT Matrix (preserves 0010 grants on shared tables)
-- =============================================================================
REVOKE ALL ON TABLE
    public.users,
    public.workspaces,
    public.workspace_memberships,
    public.source_accounts,
    public.source_resources,
    public.source_objects,
    public.source_object_revisions,
    public.content_units
FROM PUBLIC;

-- Worker MUST NOT access users, workspaces, or workspace_memberships.
REVOKE ALL ON TABLE
    public.users,
    public.workspaces,
    public.workspace_memberships
FROM cm_worker_app;

-- cm_api_app privileges (aligned with 0010: no DELETE on source_accounts)
GRANT SELECT ON public.workspaces TO cm_api_app;
GRANT SELECT, INSERT, UPDATE ON public.source_accounts TO cm_api_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON
    public.source_resources,
    public.source_objects,
    public.source_object_revisions,
    public.content_units
TO cm_api_app;

-- cm_worker_app privileges (least privilege: no DELETE, no auth/membership tables)
GRANT SELECT ON public.source_accounts TO cm_worker_app;
GRANT SELECT, UPDATE ON public.source_resources TO cm_worker_app;
GRANT SELECT, INSERT, UPDATE ON
    public.source_objects,
    public.content_units
TO cm_worker_app;
GRANT SELECT, INSERT ON public.source_object_revisions TO cm_worker_app;

RESET ROLE;
