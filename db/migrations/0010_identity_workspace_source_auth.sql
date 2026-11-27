-- Migration 0010: Identity, Workspace Membership, OAuth Connect State,
-- Source Account Boundary, and Orphan Secret Cleanup Audit/Outbox.
--
-- Idempotent: safe to run on a fresh PostgreSQL 18 database and safe to re-apply.
-- Security invariants:
--   * Least-privilege NOLOGIN roles (cm_migrator, cm_api_app, cm_worker_app), all NOBYPASSRLS.
--   * All tables owned by cm_migrator; runtime roles are never table owners.
--   * ENABLE + FORCE ROW LEVEL SECURITY on every identity/tenant/auth table.
--   * Fail-closed auth_ctx.current_workspace_id() and auth_ctx.current_user_id()
--     backed by transaction-local set_config('app.current_*', ..., true).
--   * No plaintext OAuth state, PKCE verifier, or provider access token stored in DB.

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

-- Lock down default PUBLIC schema creation and grant explicit usage.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO cm_migrator;
GRANT USAGE ON SCHEMA public TO cm_api_app, cm_worker_app;

CREATE SCHEMA IF NOT EXISTS auth_ctx AUTHORIZATION cm_migrator;
REVOKE ALL ON SCHEMA auth_ctx FROM PUBLIC;
GRANT USAGE ON SCHEMA auth_ctx TO cm_api_app, cm_worker_app;

SET ROLE cm_migrator;

-- =============================================================================
-- 1. Fail-closed transaction-local context helpers (auth_ctx)
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

-- =============================================================================
-- 2. Identity & Workspace Membership Tables (Data Model §3.1)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.users (
    id           uuid PRIMARY KEY,
    auth_issuer  text NOT NULL CHECK (btrim(auth_issuer) <> ''),
    auth_subject text NOT NULL CHECK (btrim(auth_subject) <> ''),
    email        text NULL,
    name         text NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_users_auth_issuer_subject UNIQUE (auth_issuer, auth_subject)
);

CREATE TABLE IF NOT EXISTS public.workspaces (
    id            uuid PRIMARY KEY,
    owner_user_id uuid NOT NULL REFERENCES public.users(id),
    name          text NOT NULL CHECK (btrim(name) <> ''),
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.workspace_memberships (
    workspace_id uuid NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
    user_id      uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    role         text NOT NULL CHECK (role IN ('OWNER', 'MEMBER', 'VIEWER')),
    revoked_at   timestamptz NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_memberships_active_user_ws
    ON public.workspace_memberships (user_id, workspace_id)
    WHERE revoked_at IS NULL;

-- =============================================================================
-- 3. OAuth Connect State Boundary (single-use hashed state + PKCE ref)
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.oauth_connect_states (
    state_hash             text PRIMARY KEY CHECK (state_hash ~ '^[0-9a-f]{64}$'),
    workspace_id           uuid NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
    actor_user_id          uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    provider               text NOT NULL CHECK (provider IN ('GITHUB')),
    pkce_code_challenge    text NOT NULL CHECK (btrim(pkce_code_challenge) <> ''),
    pkce_code_verifier_ref text NOT NULL CHECK (btrim(pkce_code_verifier_ref) <> ''),
    return_path            text NOT NULL CHECK (
        return_path LIKE '/%'
        AND return_path NOT LIKE '//%'
        AND position('://' in return_path) = 0
    ),
    expires_at             timestamptz NOT NULL,
    consumed_at            timestamptz NULL,
    consumed_request_id    text NULL,
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_oauth_connect_states_membership
        FOREIGN KEY (workspace_id, actor_user_id)
        REFERENCES public.workspace_memberships (workspace_id, user_id)
        ON DELETE CASCADE,
    CONSTRAINT chk_oauth_connect_states_expiry
        CHECK (expires_at > created_at),
    CONSTRAINT chk_oauth_connect_states_consumed_request
        CHECK (consumed_at IS NULL OR (consumed_request_id IS NOT NULL AND btrim(consumed_request_id) <> ''))
);

CREATE INDEX IF NOT EXISTS idx_oauth_connect_states_ws_actor_created
    ON public.oauth_connect_states (workspace_id, actor_user_id, created_at DESC);

-- =============================================================================
-- 4. Source Accounts Minimal Boundary (Data Model §3.2)
-- =============================================================================
-- Never stores raw provider access tokens; only opaque token_secret_ref keys.
CREATE TABLE IF NOT EXISTS public.source_accounts (
    id                  uuid PRIMARY KEY,
    workspace_id        uuid NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
    provider            text NOT NULL CHECK (provider IN ('GITHUB')),
    external_account_id text NOT NULL CHECK (btrim(external_account_id) <> ''),
    token_secret_ref    text NOT NULL CHECK (
        btrim(token_secret_ref) <> ''
        AND token_secret_ref ~ '^[a-zA-Z0-9._:/-]+$'
    ),
    scopes              text[] NOT NULL DEFAULT ARRAY[]::text[],
    status              text NOT NULL CHECK (status IN ('ACTIVE', 'REVOKED')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    revoked_at          timestamptz NULL,
    CONSTRAINT uq_source_accounts_workspace_provider_external
        UNIQUE (workspace_id, provider, external_account_id),
    CONSTRAINT uq_source_accounts_workspace_id_id
        UNIQUE (workspace_id, id),
    CONSTRAINT chk_source_accounts_status_revoked
        CHECK (
            (status = 'REVOKED' AND revoked_at IS NOT NULL)
            OR (status = 'ACTIVE' AND revoked_at IS NULL)
        )
);

-- =============================================================================
-- 5. Orphan Secret Cleanup Audit / Outbox
-- =============================================================================
CREATE TABLE IF NOT EXISTS public.orphan_secret_cleanups (
    id                uuid PRIMARY KEY,
    workspace_id      uuid NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
    actor_user_id     uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    provider          text NOT NULL CHECK (provider IN ('GITHUB')),
    token_secret_ref  text NOT NULL CHECK (btrim(token_secret_ref) <> ''),
    status            text NOT NULL CHECK (status IN ('DELETED', 'RETRY_PENDING')),
    attempt_count     integer NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
    failure_reason    text NOT NULL CHECK (btrim(failure_reason) <> ''),
    redacted_metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    resolved_at       timestamptz NULL,
    CONSTRAINT uq_orphan_secret_cleanups_workspace_ref
        UNIQUE (workspace_id, token_secret_ref)
);

-- =============================================================================
-- 6. ENABLE + FORCE Row Level Security on All Tables
-- =============================================================================
ALTER TABLE public.users ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.users FORCE ROW LEVEL SECURITY;

ALTER TABLE public.workspaces ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workspaces FORCE ROW LEVEL SECURITY;

ALTER TABLE public.workspace_memberships ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.workspace_memberships FORCE ROW LEVEL SECURITY;

ALTER TABLE public.oauth_connect_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.oauth_connect_states FORCE ROW LEVEL SECURITY;

ALTER TABLE public.source_accounts ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_accounts FORCE ROW LEVEL SECURITY;

ALTER TABLE public.orphan_secret_cleanups ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.orphan_secret_cleanups FORCE ROW LEVEL SECURITY;

-- =============================================================================
-- 7. Fail-Closed RLS Policies (Idempotent DROP + CREATE)
-- =============================================================================

-- users: cm_api_app may only read the exact authenticated user or verified (issuer, sub)
DROP POLICY IF EXISTS users_api_self_or_subject_select ON public.users;
CREATE POLICY users_api_self_or_subject_select
    ON public.users
    FOR SELECT
    TO cm_api_app
    USING (
        id = auth_ctx.current_user_id()
        OR (
            auth_issuer = NULLIF(current_setting('app.current_auth_issuer', true), '')
            AND auth_subject = NULLIF(current_setting('app.current_auth_subject', true), '')
        )
    );

-- workspaces: cm_api_app may only read the current scoped workspace with active membership
DROP POLICY IF EXISTS workspaces_api_active_member_select ON public.workspaces;
CREATE POLICY workspaces_api_active_member_select
    ON public.workspaces
    FOR SELECT
    TO cm_api_app
    USING (
        id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = workspaces.id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

-- workspace_memberships: cm_api_app may only read active memberships for current user
DROP POLICY IF EXISTS workspace_memberships_api_self_active_select ON public.workspace_memberships;
CREATE POLICY workspace_memberships_api_self_active_select
    ON public.workspace_memberships
    FOR SELECT
    TO cm_api_app
    USING (
        user_id = auth_ctx.current_user_id()
        AND revoked_at IS NULL
    );

-- oauth_connect_states: cm_api_app scoped to workspace + actor + live active membership
DROP POLICY IF EXISTS oauth_connect_states_api_isolation ON public.oauth_connect_states;
CREATE POLICY oauth_connect_states_api_isolation
    ON public.oauth_connect_states
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND actor_user_id = auth_ctx.current_user_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = oauth_connect_states.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND actor_user_id = auth_ctx.current_user_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = oauth_connect_states.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

-- source_accounts: cm_api_app scoped to workspace + live active membership; worker read-only
DROP POLICY IF EXISTS source_accounts_api_isolation ON public.source_accounts;
CREATE POLICY source_accounts_api_isolation
    ON public.source_accounts
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = source_accounts.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND EXISTS (
            SELECT 1
            FROM public.workspace_memberships m
            WHERE m.workspace_id = source_accounts.workspace_id
              AND m.user_id = auth_ctx.current_user_id()
              AND m.revoked_at IS NULL
        )
    );

DROP POLICY IF EXISTS source_accounts_worker_select ON public.source_accounts;
CREATE POLICY source_accounts_worker_select
    ON public.source_accounts
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

-- orphan_secret_cleanups: cm_api_app can record cleanup audit even if membership
-- was revoked mid-callback; worker can read/update RETRY_PENDING within workspace.
DROP POLICY IF EXISTS orphan_secret_cleanups_api_isolation ON public.orphan_secret_cleanups;
CREATE POLICY orphan_secret_cleanups_api_isolation
    ON public.orphan_secret_cleanups
    FOR ALL
    TO cm_api_app
    USING (
        workspace_id = auth_ctx.current_workspace_id()
        AND actor_user_id = auth_ctx.current_user_id()
    )
    WITH CHECK (
        workspace_id = auth_ctx.current_workspace_id()
        AND actor_user_id = auth_ctx.current_user_id()
    );

DROP POLICY IF EXISTS orphan_secret_cleanups_worker_rw ON public.orphan_secret_cleanups;
CREATE POLICY orphan_secret_cleanups_worker_rw
    ON public.orphan_secret_cleanups
    FOR SELECT
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS orphan_secret_cleanups_worker_update ON public.orphan_secret_cleanups;
CREATE POLICY orphan_secret_cleanups_worker_update
    ON public.orphan_secret_cleanups
    FOR UPDATE
    TO cm_worker_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

-- =============================================================================
-- 8. Explicit Least-Privilege GRANT Matrix (scoped strictly to 0010 tables)
-- =============================================================================
REVOKE ALL ON TABLE
    public.users,
    public.workspaces,
    public.workspace_memberships,
    public.oauth_connect_states,
    public.source_accounts,
    public.orphan_secret_cleanups
FROM PUBLIC, cm_api_app, cm_worker_app;

-- cm_api_app:
--   * Read-only on users, workspaces, workspace_memberships (never mutates identity/membership)
--   * Scoped read/write on oauth_connect_states, source_accounts, orphan_secret_cleanups (no DELETE)
GRANT SELECT ON public.users TO cm_api_app;
GRANT SELECT ON public.workspaces TO cm_api_app;
GRANT SELECT ON public.workspace_memberships TO cm_api_app;
GRANT SELECT, INSERT, UPDATE ON public.oauth_connect_states TO cm_api_app;
GRANT SELECT, INSERT, UPDATE ON public.source_accounts TO cm_api_app;
GRANT SELECT, INSERT, UPDATE ON public.orphan_secret_cleanups TO cm_api_app;

-- cm_worker_app:
--   * NO access to users, workspaces, workspace_memberships, or oauth_connect_states
--   * Read-only on source_accounts; read/update on orphan_secret_cleanups for retries
GRANT SELECT ON public.source_accounts TO cm_worker_app;
GRANT SELECT, UPDATE ON public.orphan_secret_cleanups TO cm_worker_app;

RESET ROLE;
