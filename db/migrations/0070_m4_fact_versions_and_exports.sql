-- Migration 0070: M4 Fact Versioning (MVP API §6 / ER-012) & Exports Schema (MVP API §7 / T-407 / EXPORT-02 / OD-003)
--
-- Prerequisites: Migrations 0010, 0020, 0030, 0040, 0050, and 0060.
-- Idempotent: safe to run on fresh PostgreSQL 18 (0010..0070), staged upgrade (0010..0060 -> 0070),
--             and repeated re-apply (0070 twice).
--
-- Design guarantees:
--   * Owned by cm_migrator (NOLOGIN, NOBYPASSRLS); runtime roles never own tables.
--   * public.facts gains version + supersedes_fact_id + SUPERSEDED status with symmetric
--     Composite FK (workspace_id, supersedes_fact_id) -> public.facts(workspace_id, id).
--   * public.exports & public.export_claims enforce Plan C raw-text & secret JSONB guards,
--     Composite Foreign Keys, ENABLE + FORCE ROW LEVEL SECURITY, fail-closed workspace
--     policies (with active workspace_memberships check for cm_api_app), and least-privilege GRANTs.
--   * Zero LLM token/key storage tables (ADR-020 / OD-014 live LLM OFF).

SET ROLE cm_migrator;

-- =============================================================================
-- 1. public.facts Versioning (MVP API §6 & ER-012 symmetry with public.claims)
-- =============================================================================
ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1;

ALTER TABLE public.facts
    ADD COLUMN IF NOT EXISTS supersedes_fact_id uuid NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_facts_version_positive'
          AND conrelid = 'public.facts'::regclass
    ) THEN
        ALTER TABLE public.facts
            ADD CONSTRAINT ck_facts_version_positive
            CHECK (version >= 1);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'ck_facts_no_self_supersede'
          AND conrelid = 'public.facts'::regclass
    ) THEN
        ALTER TABLE public.facts
            ADD CONSTRAINT ck_facts_no_self_supersede
            CHECK (supersedes_fact_id IS NULL OR supersedes_fact_id <> id);
    END IF;

    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conname = 'fk_facts_same_workspace_supersedes'
          AND conrelid = 'public.facts'::regclass
    ) THEN
        ALTER TABLE public.facts
            ADD CONSTRAINT fk_facts_same_workspace_supersedes
            FOREIGN KEY (workspace_id, supersedes_fact_id)
            REFERENCES public.facts (workspace_id, id)
            ON DELETE SET NULL;
    END IF;
END
$$;

ALTER TABLE public.facts
    DROP CONSTRAINT IF EXISTS ck_facts_status;

ALTER TABLE public.facts
    ADD CONSTRAINT ck_facts_status
    CHECK (
        status IN (
            'CANDIDATE',
            'SUPPORTED',
            'USER_CONFIRMED',
            'CONFLICTED',
            'STALE',
            'REJECTED',
            'SUPERSEDED'
        )
    );

CREATE INDEX IF NOT EXISTS idx_facts_workspace_supersedes
    ON public.facts (workspace_id, supersedes_fact_id)
    WHERE supersedes_fact_id IS NOT NULL;

-- =============================================================================
-- 2. public.exports & public.export_claims (MVP API §7, T-407, EXPORT-02, OD-003)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.exports (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id         uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    source_resource_id   uuid NULL,
    requested_by_user_id uuid NULL,
    format               text NOT NULL,
    export_policy        text NOT NULL DEFAULT 'SOURCE_VERIFIED_ONLY',
    status               text NOT NULL DEFAULT 'PENDING',
    idempotency_key      text NOT NULL,
    input_hash           text NOT NULL,
    policy_version       text NOT NULL,
    manifest             jsonb NOT NULL DEFAULT '{}'::jsonb,
    artifact_payload     jsonb NULL,
    invalidation_reason  text NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    completed_at         timestamptz NULL,
    invalidated_at       timestamptz NULL,
    deleted_at           timestamptz NULL,
    updated_at           timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_exports_format
        CHECK (format IN ('MARKDOWN', 'JSON')),
    CONSTRAINT ck_exports_policy
        CHECK (export_policy IN ('SOURCE_VERIFIED_ONLY', 'ALLOW_USER_CONFIRMED')),
    CONSTRAINT ck_exports_status
        CHECK (status IN ('PENDING', 'READY', 'INVALIDATED', 'FAILED', 'DELETED')),
    CONSTRAINT ck_exports_idempotency_key_non_empty
        CHECK (length(btrim(idempotency_key)) > 0),
    CONSTRAINT ck_exports_input_hash_sha256
        CHECK (input_hash ~ '^(sha256:)?[0-9a-f]{64}$'),
    CONSTRAINT ck_exports_policy_version_non_empty
        CHECK (length(btrim(policy_version)) > 0),
    CONSTRAINT ck_exports_manifest_object
        CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT ck_exports_manifest_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(manifest)),
    CONSTRAINT ck_exports_manifest_no_secret
        CHECK (NOT public.cm_jsonb_contains_forbidden_secret_key(manifest)),
    CONSTRAINT ck_exports_artifact_payload_object
        CHECK (artifact_payload IS NULL OR jsonb_typeof(artifact_payload) = 'object'),
    CONSTRAINT ck_exports_artifact_payload_no_raw
        CHECK (
            artifact_payload IS NULL
            OR NOT public.cm_jsonb_contains_forbidden_raw_key(artifact_payload)
        ),
    CONSTRAINT ck_exports_artifact_payload_no_secret
        CHECK (
            artifact_payload IS NULL
            OR NOT public.cm_jsonb_contains_forbidden_secret_key(artifact_payload)
        ),
    CONSTRAINT uq_exports_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_exports_workspace_idempotency
        UNIQUE (workspace_id, idempotency_key),
    CONSTRAINT fk_exports_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_exports_workspace_status_created
    ON public.exports (workspace_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_exports_workspace_source_resource
    ON public.exports (workspace_id, source_resource_id)
    WHERE source_resource_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS public.export_claims (
    id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                    uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    export_id                       uuid NOT NULL,
    claim_id                        uuid NOT NULL,
    claim_version                   integer NOT NULL,
    computed_verification_at_export text NOT NULL,
    support_manifest_snapshot       jsonb NOT NULL DEFAULT '{}'::jsonb,
    ordinal                         integer NOT NULL DEFAULT 0,
    created_at                      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_export_claims_version_positive
        CHECK (claim_version >= 1),
    CONSTRAINT ck_export_claims_verification
        CHECK (computed_verification_at_export IN ('SOURCE_VERIFIED', 'USER_CONFIRMED', 'MIXED')),
    CONSTRAINT ck_export_claims_snapshot_object
        CHECK (jsonb_typeof(support_manifest_snapshot) = 'object'),
    CONSTRAINT ck_export_claims_snapshot_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(support_manifest_snapshot)),
    CONSTRAINT ck_export_claims_ordinal_non_negative
        CHECK (ordinal >= 0),
    CONSTRAINT uq_export_claims_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_export_claims_export_claim
        UNIQUE (export_id, claim_id),
    CONSTRAINT fk_export_claims_same_workspace_export
        FOREIGN KEY (workspace_id, export_id)
        REFERENCES public.exports (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_export_claims_same_workspace_claim
        FOREIGN KEY (workspace_id, claim_id)
        REFERENCES public.claims (workspace_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_export_claims_workspace_export
    ON public.export_claims (workspace_id, export_id);

CREATE INDEX IF NOT EXISTS idx_export_claims_workspace_claim
    ON public.export_claims (workspace_id, claim_id);

-- =============================================================================
-- 3. ENABLE + FORCE Row Level Security & Fail-Closed Workspace Policies
-- =============================================================================
ALTER TABLE public.exports ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.exports FORCE ROW LEVEL SECURITY;

ALTER TABLE public.export_claims ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.export_claims FORCE ROW LEVEL SECURITY;

-- exports
DROP POLICY IF EXISTS exports_api_workspace_isolation ON public.exports;
CREATE POLICY exports_api_workspace_isolation
    ON public.exports
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = exports.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = exports.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS exports_worker_workspace_select ON public.exports;
CREATE POLICY exports_worker_workspace_select
    ON public.exports
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS exports_worker_workspace_insert ON public.exports;
CREATE POLICY exports_worker_workspace_insert
    ON public.exports
    FOR INSERT
    TO cm_worker_app
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS exports_worker_workspace_update ON public.exports;
CREATE POLICY exports_worker_workspace_update
    ON public.exports
    FOR UPDATE
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- export_claims
DROP POLICY IF EXISTS export_claims_api_workspace_isolation ON public.export_claims;
CREATE POLICY export_claims_api_workspace_isolation
    ON public.export_claims
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = export_claims.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = export_claims.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS export_claims_worker_workspace_select ON public.export_claims;
CREATE POLICY export_claims_worker_workspace_select
    ON public.export_claims
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS export_claims_worker_workspace_insert ON public.export_claims;
CREATE POLICY export_claims_worker_workspace_insert
    ON public.export_claims
    FOR INSERT
    TO cm_worker_app
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS export_claims_worker_workspace_update ON public.export_claims;
CREATE POLICY export_claims_worker_workspace_update
    ON public.export_claims
    FOR UPDATE
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- =============================================================================
-- 4. Least-Privilege GRANT Matrix (scoped strictly to 0070 tables)
-- =============================================================================
REVOKE ALL ON TABLE
    public.exports,
    public.export_claims
FROM PUBLIC, cm_api_app, cm_worker_app;

GRANT SELECT, INSERT, UPDATE, DELETE ON
    public.exports,
    public.export_claims
TO cm_api_app;

GRANT SELECT, INSERT, UPDATE ON
    public.exports,
    public.export_claims
TO cm_worker_app;

RESET ROLE;
