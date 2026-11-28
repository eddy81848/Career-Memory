-- Migration 0060: M3 Pairwise Event Constraints (OD-007) & Immutable WorkEvent Version Snapshots (T-305)
--
-- Prerequisites: Migrations 0010, 0020, 0030, 0040, and 0050.
-- Idempotent: safe to run on a fresh PostgreSQL 18 database and safe to re-apply.
--
-- Design guarantees:
--   * Owned by cm_migrator (NOLOGIN, NOBYPASSRLS); runtime roles never own tables.
--   * OD-007 (Option B):
--       - WorkEvent <-> SourceObject relations remain in public.event_memberships
--         (MUST_LINK | CONTEXT_ONLY | CANNOT_LINK).
--       - WorkEvent <-> WorkEvent pairwise CANNOT_LINK constraints live in
--         public.event_constraints with canonical ordering (left_event_id < right_event_id)
--         and a partial unique index for ACTIVE pairs.
--   * T-305 Immutable EventVersion Snapshots:
--       - public.work_event_versions stores append-only immutable snapshots (v1..vN)
--         including Plan C non-raw membership_snapshot metadata.
--       - BEFORE UPDATE OR DELETE trigger blocks all UPDATEs and blocks DELETEs
--         unless auth_ctx.allow_revision_purge() is true (approved OD-003 purge).
--   * Multi-tenant isolation via ENABLE + FORCE ROW LEVEL SECURITY, fail-closed
--     auth_ctx.current_workspace_id() policies, and Composite Foreign Keys
--     (workspace_id, parent_id) -> parent(workspace_id, id).
--   * Plan C (방안 C) raw-text non-persistence enforced on every JSONB column via
--     public.cm_jsonb_contains_forbidden_raw_key().

SET ROLE cm_migrator;

-- =============================================================================
-- 1. event_constraints (OD-007 Pairwise WorkEvent <-> WorkEvent Constraints)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.event_constraints (
    id                          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    left_event_id               uuid NOT NULL,
    right_event_id              uuid NOT NULL,
    constraint_type             text NOT NULL DEFAULT 'CANNOT_LINK',
    origin                      text NOT NULL,
    status                      text NOT NULL DEFAULT 'ACTIVE',
    rationale_code              text NULL,
    constraint_metadata         jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by_review_action_id uuid NULL,
    created_at                  timestamptz NOT NULL DEFAULT now(),
    revoked_at                  timestamptz NULL,

    CONSTRAINT ck_event_constraints_canonical_pair
        CHECK (left_event_id < right_event_id),
    CONSTRAINT ck_event_constraints_type
        CHECK (constraint_type IN ('CANNOT_LINK')),
    CONSTRAINT ck_event_constraints_origin
        CHECK (origin IN ('USER', 'SPLIT', 'POLICY')),
    CONSTRAINT ck_event_constraints_status
        CHECK (status IN ('ACTIVE', 'REVOKED')),
    CONSTRAINT ck_event_constraints_revoked_at_consistency
        CHECK (status = 'REVOKED' OR revoked_at IS NULL),
    CONSTRAINT ck_event_constraints_metadata_object
        CHECK (jsonb_typeof(constraint_metadata) = 'object'),
    CONSTRAINT ck_event_constraints_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(constraint_metadata)),
    CONSTRAINT uq_event_constraints_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT fk_event_constraints_same_workspace_left
        FOREIGN KEY (workspace_id, left_event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_event_constraints_same_workspace_right
        FOREIGN KEY (workspace_id, right_event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_event_constraints_same_workspace_review_action
        FOREIGN KEY (workspace_id, created_by_review_action_id)
        REFERENCES public.review_actions (workspace_id, id)
        ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_event_constraints_active_pair
    ON public.event_constraints (workspace_id, left_event_id, right_event_id, constraint_type)
    WHERE status = 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_event_constraints_workspace_left
    ON public.event_constraints (workspace_id, left_event_id);

CREATE INDEX IF NOT EXISTS idx_event_constraints_workspace_right
    ON public.event_constraints (workspace_id, right_event_id);

-- =============================================================================
-- 2. work_event_versions (T-305 Append-Only Immutable EventVersion Snapshots)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.work_event_versions (
    id                              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                    uuid NOT NULL REFERENCES public.workspaces (id) ON DELETE CASCADE,
    event_id                        uuid NOT NULL,
    version                         integer NOT NULL,
    anchor_mode                     text NOT NULL,
    primary_anchor_source_object_id uuid NULL,
    anchor_scope                    jsonb NULL,
    title                           text NOT NULL,
    problem                         text NULL,
    action                          text NULL,
    outcome                         text NULL,
    summary_metadata                jsonb NOT NULL DEFAULT '{}'::jsonb,
    status                          text NOT NULL,
    created_by                      text NOT NULL,
    created_by_review_action_id     uuid NULL,
    membership_snapshot             jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_start_at               timestamptz NULL,
    occurred_end_at                 timestamptz NULL,
    created_at                      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT ck_work_event_versions_anchor_mode
        CHECK (anchor_mode IN ('PR', 'PR_SEGMENT', 'DIRECT_COMMIT', 'USER_COMPOSITE')),
    CONSTRAINT ck_work_event_versions_version_positive
        CHECK (version >= 1),
    CONSTRAINT ck_work_event_versions_title_non_empty
        CHECK (length(btrim(title)) > 0),
    CONSTRAINT ck_work_event_versions_status
        CHECK (status IN ('CANDIDATE', 'CONFIRMED', 'REJECTED', 'STALE', 'SUPERSEDED')),
    CONSTRAINT ck_work_event_versions_created_by
        CHECK (created_by IN ('RULE', 'USER')),
    CONSTRAINT ck_work_event_versions_pr_anchor
        CHECK (anchor_mode NOT IN ('PR', 'PR_SEGMENT') OR primary_anchor_source_object_id IS NOT NULL),
    CONSTRAINT ck_work_event_versions_pr_segment_scope
        CHECK (
            anchor_mode <> 'PR_SEGMENT'
            OR (anchor_scope IS NOT NULL AND jsonb_typeof(anchor_scope) = 'object')
        ),
    CONSTRAINT ck_work_event_versions_direct_commit_anchor
        CHECK (anchor_mode <> 'DIRECT_COMMIT' OR primary_anchor_source_object_id IS NOT NULL),
    CONSTRAINT ck_work_event_versions_time_order
        CHECK (
            occurred_start_at IS NULL
            OR occurred_end_at IS NULL
            OR occurred_end_at >= occurred_start_at
        ),
    CONSTRAINT ck_work_event_versions_anchor_scope_no_raw
        CHECK (
            anchor_scope IS NULL
            OR (
                jsonb_typeof(anchor_scope) = 'object'
                AND NOT public.cm_jsonb_contains_forbidden_raw_key(anchor_scope)
            )
        ),
    CONSTRAINT ck_work_event_versions_summary_metadata_object
        CHECK (jsonb_typeof(summary_metadata) = 'object'),
    CONSTRAINT ck_work_event_versions_summary_metadata_no_raw
        CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(summary_metadata)),
    CONSTRAINT ck_work_event_versions_membership_snapshot_no_raw
        CHECK (
            jsonb_typeof(membership_snapshot) = 'object'
            AND NOT public.cm_jsonb_contains_forbidden_raw_key(membership_snapshot)
        ),
    CONSTRAINT uq_work_event_versions_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT uq_work_event_versions_event_version
        UNIQUE (workspace_id, event_id, version),
    CONSTRAINT fk_work_event_versions_same_workspace_event
        FOREIGN KEY (workspace_id, event_id)
        REFERENCES public.work_events (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_work_event_versions_same_workspace_primary_anchor
        FOREIGN KEY (workspace_id, primary_anchor_source_object_id)
        REFERENCES public.source_objects (workspace_id, id)
        ON DELETE CASCADE,
    CONSTRAINT fk_work_event_versions_same_workspace_review_action
        FOREIGN KEY (workspace_id, created_by_review_action_id)
        REFERENCES public.review_actions (workspace_id, id)
        ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_work_event_versions_workspace_event
    ON public.work_event_versions (workspace_id, event_id, version ASC);

CREATE INDEX IF NOT EXISTS idx_work_event_versions_workspace_review_action
    ON public.work_event_versions (workspace_id, created_by_review_action_id)
    WHERE created_by_review_action_id IS NOT NULL;

-- T-305 Immutability Trigger:
-- work_event_versions rows are append-only historical snapshots.
-- UPDATE is always rejected; DELETE is rejected unless auth_ctx.allow_revision_purge()
-- is explicitly enabled inside an approved OD-003 deletion/purge transaction.
CREATE OR REPLACE FUNCTION public.cm_guard_work_event_versions_immutable()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION
            'work_event_versions rows are immutable (T-305): UPDATE is prohibited on version snapshot %',
            OLD.id
            USING ERRCODE = '23514';
    END IF;

    IF TG_OP = 'DELETE' THEN
        IF NOT auth_ctx.allow_revision_purge() THEN
            RAISE EXCEPTION
                'work_event_versions DELETE requires explicit purge context (app.allow_revision_purge = on) for version snapshot %',
                OLD.id
                USING ERRCODE = '23514';
        END IF;
        RETURN OLD;
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_work_event_versions_immutable
    ON public.work_event_versions;

CREATE TRIGGER trg_work_event_versions_immutable
    BEFORE UPDATE OR DELETE
    ON public.work_event_versions
    FOR EACH ROW
    EXECUTE FUNCTION public.cm_guard_work_event_versions_immutable();

-- =============================================================================
-- 3. ENABLE + FORCE Row Level Security & Fail-Closed Workspace Policies
-- =============================================================================
ALTER TABLE public.event_constraints ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.event_constraints FORCE ROW LEVEL SECURITY;

ALTER TABLE public.work_event_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.work_event_versions FORCE ROW LEVEL SECURITY;

-- event_constraints
DROP POLICY IF EXISTS event_constraints_api_workspace_isolation ON public.event_constraints;
CREATE POLICY event_constraints_api_workspace_isolation
    ON public.event_constraints
    FOR ALL
    TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS event_constraints_worker_workspace_select ON public.event_constraints;
CREATE POLICY event_constraints_worker_workspace_select
    ON public.event_constraints
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS event_constraints_worker_workspace_insert ON public.event_constraints;
CREATE POLICY event_constraints_worker_workspace_insert
    ON public.event_constraints
    FOR INSERT
    TO cm_worker_app
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS event_constraints_worker_workspace_update ON public.event_constraints;
CREATE POLICY event_constraints_worker_workspace_update
    ON public.event_constraints
    FOR UPDATE
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- work_event_versions
DROP POLICY IF EXISTS work_event_versions_api_workspace_isolation ON public.work_event_versions;
CREATE POLICY work_event_versions_api_workspace_isolation
    ON public.work_event_versions
    FOR ALL
    TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS work_event_versions_worker_workspace_select ON public.work_event_versions;
CREATE POLICY work_event_versions_worker_workspace_select
    ON public.work_event_versions
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS work_event_versions_worker_workspace_insert ON public.work_event_versions;
CREATE POLICY work_event_versions_worker_workspace_insert
    ON public.work_event_versions
    FOR INSERT
    TO cm_worker_app
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- =============================================================================
-- 4. Least-Privilege GRANT Matrix (scoped strictly to 0060 tables)
-- =============================================================================
REVOKE ALL ON TABLE
    public.event_constraints,
    public.work_event_versions
FROM PUBLIC, cm_api_app, cm_worker_app;

-- event_constraints: cm_api_app & cm_worker_app may read/insert/update (revoke via status='REVOKED')
GRANT SELECT, INSERT, UPDATE ON public.event_constraints TO cm_api_app;
GRANT SELECT, INSERT, UPDATE ON public.event_constraints TO cm_worker_app;

-- work_event_versions: strictly append-only (SELECT + INSERT only; zero UPDATE/DELETE)
GRANT SELECT, INSERT ON public.work_event_versions TO cm_api_app;
GRANT SELECT, INSERT ON public.work_event_versions TO cm_worker_app;

RESET ROLE;
