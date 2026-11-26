-- T-007D Synthetic Role Definitions (Local PostgreSQL Experiment Only)
-- Principle of least privilege:
--   1. t007d_migrator   : Schema/DDL owner (CI/migration path ONLY; never used by API or Worker runtime)
--   2. t007d_api_app    : Restricted API runtime role (NOBYPASSRLS, narrow CRUD, read-only active self memberships)
--   3. t007d_worker_app : Restricted Worker candidate role (NOBYPASSRLS, no user/membership access, no DELETE)
--
-- Synthetic test passwords are for ephemeral local PostgreSQL only and must never be used outside local spikes.

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 't007d_migrator') THEN
        CREATE ROLE t007d_migrator
            LOGIN
            PASSWORD 'local_t007d_migrator_only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 't007d_api_app') THEN
        CREATE ROLE t007d_api_app
            LOGIN
            PASSWORD 'local_t007d_api_only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 't007d_worker_app') THEN
        CREATE ROLE t007d_worker_app
            LOGIN
            PASSWORD 'local_t007d_worker_only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;

    -- Synthetic Data API (PostgREST-equivalent) DB roles used solely to test
    -- DB-layer denial of direct domain table reads/writes (OD006-5 local DB layer).
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 't007d_data_api_anon') THEN
        CREATE ROLE t007d_data_api_anon
            LOGIN
            PASSWORD 'local_t007d_anon_only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 't007d_data_api_authenticated') THEN
        CREATE ROLE t007d_data_api_authenticated
            LOGIN
            PASSWORD 'local_t007d_authenticated_only'
            NOSUPERUSER
            NOCREATEDB
            NOCREATEROLE
            NOINHERIT
            NOBYPASSRLS;
    END IF;
END
$$;

-- Lock down default PUBLIC privileges on the test database.
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Dedicated schemas owned by t007d_migrator so application roles are never table owners.
CREATE SCHEMA IF NOT EXISTS auth_ctx AUTHORIZATION t007d_migrator;
CREATE SCHEMA IF NOT EXISTS app_t007d AUTHORIZATION t007d_migrator;

REVOKE ALL ON SCHEMA auth_ctx FROM PUBLIC, t007d_data_api_anon, t007d_data_api_authenticated;
REVOKE ALL ON SCHEMA app_t007d FROM PUBLIC, t007d_data_api_anon, t007d_data_api_authenticated;

GRANT USAGE ON SCHEMA auth_ctx TO t007d_api_app, t007d_worker_app;
GRANT USAGE ON SCHEMA app_t007d TO t007d_api_app, t007d_worker_app;
