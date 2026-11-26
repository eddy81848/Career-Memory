-- T-007D Schema, RLS Policies, GRANTs, and Cross-Workspace Parent-Child Integrity Comparison
-- Executed as t007d_migrator (table owner; never used in API or Worker runtime paths).

SET ROLE t007d_migrator;

-- =============================================================================
-- 1. Fail-closed transaction-local context helpers
-- =============================================================================
-- Safe against unset GUCs (NULL), post-RESET/post-tx empty string (''), and malformed values.
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

CREATE OR REPLACE FUNCTION auth_ctx.current_subject_id()
RETURNS uuid
LANGUAGE sql
STABLE
AS $$
    SELECT CASE
        WHEN COALESCE(current_setting('app.current_subject_id', true), '')
             ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
        THEN current_setting('app.current_subject_id', true)::uuid
        ELSE NULL
    END;
$$;

REVOKE ALL ON FUNCTION auth_ctx.current_workspace_id() FROM PUBLIC;
REVOKE ALL ON FUNCTION auth_ctx.current_subject_id() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_ctx.current_workspace_id() TO t007d_api_app, t007d_worker_app;
GRANT EXECUTE ON FUNCTION auth_ctx.current_subject_id() TO t007d_api_app;

-- =============================================================================
-- 2. Synthetic Identity & Workspace Membership Tables
-- =============================================================================
-- NOTE: Synthetic tables for T-007D only; NOT the product User/Identity schema.
CREATE TABLE app_t007d.workspaces (
    id          uuid PRIMARY KEY,
    slug        text NOT NULL UNIQUE,
    display_name text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_t007d.users (
    subject_id  uuid PRIMARY KEY,
    email       text NOT NULL UNIQUE,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE app_t007d.workspace_memberships (
    workspace_id uuid NOT NULL REFERENCES app_t007d.workspaces(id) ON DELETE CASCADE,
    subject_id   uuid NOT NULL REFERENCES app_t007d.users(subject_id) ON DELETE CASCADE,
    member_role  text NOT NULL CHECK (member_role IN ('owner', 'member', 'viewer')),
    revoked_at   timestamptz NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, subject_id)
);

-- =============================================================================
-- 3. Parent Tenant Table (e.g., Import Repository / Project)
-- =============================================================================
CREATE TABLE app_t007d.projects (
    id           uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES app_t007d.workspaces(id) ON DELETE CASCADE,
    name         text NOT NULL,
    created_by   uuid NOT NULL REFERENCES app_t007d.users(subject_id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    -- Required target for Candidate A composite foreign key:
    CONSTRAINT uq_projects_workspace_id_id UNIQUE (workspace_id, id)
);

-- =============================================================================
-- 4. Child Tenant Tables — Three Isolated Candidates for Cross-Workspace FK Comparison
-- =============================================================================

-- Candidate A (RECOMMENDED): Composite Foreign Key (workspace_id, project_id) -> projects(workspace_id, id)
CREATE TABLE app_t007d.artifacts (
    id           uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES app_t007d.workspaces(id) ON DELETE CASCADE,
    project_id   uuid NOT NULL,
    title        text NOT NULL,
    payload_note text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_artifacts_same_workspace_project
        FOREIGN KEY (workspace_id, project_id)
        REFERENCES app_t007d.projects (workspace_id, id)
        ON DELETE CASCADE
);

-- Baseline (VULNERABLE): Single-column FK project_id -> projects(id) + RLS on workspace_id only.
-- Demonstrates why RLS alone does NOT stop cross-workspace FK references or UUID existence probing,
-- because PostgreSQL's internal RI trigger checks the parent table as table owner (bypassing RLS).
CREATE TABLE app_t007d.artifacts_single_fk_baseline (
    id           uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES app_t007d.workspaces(id) ON DELETE CASCADE,
    project_id   uuid NOT NULL REFERENCES app_t007d.projects(id) ON DELETE CASCADE,
    title        text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Candidate B (ALTERNATIVE): Single-column FK + RLS WITH CHECK EXISTS (subquery against parent).
-- Blocks restricted RLS roles, but does NOT enforce cross-workspace integrity for non-RLS paths
-- (e.g., migrator/backfill scripts) and incurs subquery overhead + parent-move drift risk.
CREATE TABLE app_t007d.artifacts_rls_exists_candidate (
    id           uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES app_t007d.workspaces(id) ON DELETE CASCADE,
    project_id   uuid NOT NULL REFERENCES app_t007d.projects(id) ON DELETE CASCADE,
    title        text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- Candidate C (ALTERNATIVE): Single-column FK + BEFORE INSERT/UPDATE Trigger guard.
-- Enforces for all roles including migrator, but requires custom trigger maintenance,
-- explicit locking, and a paired parent-update guard; not declaratively visible in pg_constraint.
CREATE TABLE app_t007d.artifacts_trigger_candidate (
    id           uuid PRIMARY KEY,
    workspace_id uuid NOT NULL REFERENCES app_t007d.workspaces(id) ON DELETE CASCADE,
    project_id   uuid NOT NULL REFERENCES app_t007d.projects(id) ON DELETE CASCADE,
    title        text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION app_t007d.enforce_artifact_parent_workspace()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app_t007d
AS $$
DECLARE
    v_parent_ws uuid;
BEGIN
    SELECT p.workspace_id
      INTO v_parent_ws
      FROM app_t007d.projects p
     WHERE p.id = NEW.project_id
       FOR KEY SHARE;

    IF v_parent_ws IS NULL OR v_parent_ws <> NEW.workspace_id THEN
        RAISE EXCEPTION 'cross-workspace parent reference rejected by trigger'
            USING ERRCODE = '23503';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_artifacts_trigger_candidate_workspace
    BEFORE INSERT OR UPDATE OF workspace_id, project_id
    ON app_t007d.artifacts_trigger_candidate
    FOR EACH ROW
    EXECUTE FUNCTION app_t007d.enforce_artifact_parent_workspace();

-- =============================================================================
-- 5. Enable & FORCE Row Level Security on ALL Tenant-Bearing Tables
-- =============================================================================
ALTER TABLE app_t007d.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_t007d.workspaces FORCE ROW LEVEL SECURITY;

ALTER TABLE app_t007d.workspace_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_t007d.workspace_memberships FORCE ROW LEVEL SECURITY;

ALTER TABLE app_t007d.projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_t007d.projects FORCE ROW LEVEL SECURITY;

ALTER TABLE app_t007d.artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_t007d.artifacts FORCE ROW LEVEL SECURITY;

ALTER TABLE app_t007d.artifacts_single_fk_baseline ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_t007d.artifacts_single_fk_baseline FORCE ROW LEVEL SECURITY;

ALTER TABLE app_t007d.artifacts_rls_exists_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_t007d.artifacts_rls_exists_candidate FORCE ROW LEVEL SECURITY;

ALTER TABLE app_t007d.artifacts_trigger_candidate ENABLE ROW LEVEL SECURITY;
ALTER TABLE app_t007d.artifacts_trigger_candidate FORCE ROW LEVEL SECURITY;

-- =============================================================================
-- 6. RLS Policies (Fail-Closed)
-- =============================================================================

-- workspaces: API can only read the currently scoped workspace
CREATE POLICY workspaces_api_select
    ON app_t007d.workspaces
    FOR SELECT
    TO t007d_api_app
    USING (id = auth_ctx.current_workspace_id());

-- workspace_memberships: API can ONLY read active memberships belonging to verified subject_id
CREATE POLICY memberships_api_self_active_select
    ON app_t007d.workspace_memberships
    FOR SELECT
    TO t007d_api_app
    USING (
        subject_id = auth_ctx.current_subject_id()
        AND revoked_at IS NULL
    );

-- projects: API full CRUD within current workspace; Worker read-only within current workspace
CREATE POLICY projects_api_workspace_isolation
    ON app_t007d.projects
    FOR ALL
    TO t007d_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

CREATE POLICY projects_worker_workspace_select
    ON app_t007d.projects
    FOR SELECT
    TO t007d_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

-- artifacts (Composite FK candidate): API full CRUD; Worker SELECT/INSERT/UPDATE within current workspace
CREATE POLICY artifacts_api_workspace_isolation
    ON app_t007d.artifacts
    FOR ALL
    TO t007d_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

CREATE POLICY artifacts_worker_workspace_rw
    ON app_t007d.artifacts
    FOR SELECT
    TO t007d_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

CREATE POLICY artifacts_worker_workspace_insert
    ON app_t007d.artifacts
    FOR INSERT
    TO t007d_worker_app
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

CREATE POLICY artifacts_worker_workspace_update
    ON app_t007d.artifacts
    FOR UPDATE
    TO t007d_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- artifacts_single_fk_baseline: workspace_id RLS only (deliberately demonstrates single-FK flaw)
CREATE POLICY artifacts_single_fk_api_isolation
    ON app_t007d.artifacts_single_fk_baseline
    FOR ALL
    TO t007d_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- artifacts_rls_exists_candidate: workspace_id RLS + EXISTS check against projects
CREATE POLICY artifacts_rls_exists_api_isolation
    ON app_t007d.artifacts_rls_exists_candidate
    FOR ALL
    TO t007d_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
              FROM app_t007d.projects p
             WHERE p.id = project_id
               AND p.workspace_id = auth_ctx.current_workspace_id()
        )
    );

-- artifacts_trigger_candidate: workspace_id RLS + BEFORE trigger guard
CREATE POLICY artifacts_trigger_api_isolation
    ON app_t007d.artifacts_trigger_candidate
    FOR ALL
    TO t007d_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- =============================================================================
-- 7. Explicit Minimal GRANT Matrix
-- =============================================================================
REVOKE ALL ON ALL TABLES IN SCHEMA app_t007d FROM PUBLIC;

-- t007d_api_app:
--   * READ ONLY on workspaces and workspace_memberships (never modifies tenancy/identity tables)
--   * NO direct access to users table
--   * CRUD on domain tables (projects, artifacts + comparison tables)
GRANT SELECT ON app_t007d.workspaces TO t007d_api_app;
GRANT SELECT ON app_t007d.workspace_memberships TO t007d_api_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_t007d.projects TO t007d_api_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_t007d.artifacts TO t007d_api_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_t007d.artifacts_single_fk_baseline TO t007d_api_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_t007d.artifacts_rls_exists_candidate TO t007d_api_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON app_t007d.artifacts_trigger_candidate TO t007d_api_app;

-- t007d_worker_app:
--   * NO access to users, workspaces, or workspace_memberships
--   * SELECT ONLY on projects (cannot create/modify/delete projects)
--   * SELECT, INSERT, UPDATE on artifacts (cannot DELETE artifacts)
GRANT SELECT ON app_t007d.projects TO t007d_worker_app;
GRANT SELECT, INSERT, UPDATE ON app_t007d.artifacts TO t007d_worker_app;

RESET ROLE;
