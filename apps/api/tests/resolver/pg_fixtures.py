"""Ephemeral local PostgreSQL 18 cluster and 0010 + 0020-aligned provenance DDL fixtures."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

_PG18_BIN_DIR = Path("/opt/homebrew/opt/postgresql@18/bin")
_REPO_ROOT = Path(__file__).resolve().parents[4]
_MIGRATION_0010 = (
    _REPO_ROOT / "db" / "migrations" / "0010_identity_workspace_source_auth.sql"
)

# Self-contained 0020-aligned provenance metadata schema DDL for local PG18 testing.
# Applied on top of db/migrations/0010_identity_workspace_source_auth.sql so db/migrations/
# remains untouched and conflict-free with parallel M1 schema tracks.
PROVENANCE_0020_TEST_DDL = """
SET ROLE cm_migrator;

CREATE OR REPLACE FUNCTION auth_ctx.allow_revision_purge()
RETURNS boolean
LANGUAGE sql
STABLE
AS $$
    SELECT COALESCE(current_setting('app.allow_revision_purge', true), '') = 'on';
$$;

REVOKE ALL ON FUNCTION auth_ctx.allow_revision_purge() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION auth_ctx.allow_revision_purge() TO cm_api_app, cm_worker_app;

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
              'body', 'raw_body', 'raw_text', 'text_content', 'diff', 'patch',
              'comment_body', 'message_body', 'quote', 'extracted_span', 'prompt',
              'raw_payload', 'raw_content', 'full_text', 'source_text', 'raw_diff',
              'commit_message_body', 'prompt_text', 'system_prompt', 'user_prompt',
              'completion', 'raw_response', 'content_text', 'excerpt', 'snippet',
              'verbatim_quote', 'pr_body', 'issue_body', 'review_body', 'patch_text',
              'diff_text', 'hunk_text'
          )
    );
$$;

REVOKE ALL ON FUNCTION public.cm_jsonb_contains_forbidden_raw_key(jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.cm_jsonb_contains_forbidden_raw_key(jsonb) TO cm_api_app, cm_worker_app;

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

    CONSTRAINT ck_source_resources_provider_id_non_empty CHECK (length(btrim(provider_resource_id)) > 0),
    CONSTRAINT ck_source_resources_type CHECK (resource_type IN ('GITHUB_REPOSITORY')),
    CONSTRAINT ck_source_resources_name_non_empty CHECK (length(btrim(name)) > 0),
    CONSTRAINT ck_source_resources_canonical_url_https CHECK (canonical_url ~ '^https://[^[:space:]@]+/.+'),
    CONSTRAINT ck_source_resources_visibility CHECK (visibility IN ('PUBLIC', 'PRIVATE', 'INTERNAL')),
    CONSTRAINT ck_source_resources_processing_mode CHECK (processing_mode IN ('PUBLIC_CLOUD', 'PRIVATE_LOCAL', 'PRIVATE_CLOUD_OPT_IN')),
    CONSTRAINT ck_source_resources_raw_storage_mode_none CHECK (raw_storage_mode = 'NONE'),
    CONSTRAINT ck_source_resources_raw_retention_null CHECK (raw_retention_seconds IS NULL),
    CONSTRAINT ck_source_resources_status CHECK (status IN ('ACTIVE', 'PAUSED', 'POLICY_REVIEW_REQUIRED', 'DELETED')),
    CONSTRAINT ck_source_resources_policy_version_non_empty CHECK (length(btrim(policy_version)) > 0),
    CONSTRAINT uq_source_resources_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_source_resources_account_provider_id UNIQUE (source_account_id, provider_resource_id),
    CONSTRAINT fk_source_resources_same_workspace_account
        FOREIGN KEY (workspace_id, source_account_id)
        REFERENCES public.source_accounts (workspace_id, id)
        ON DELETE CASCADE
);

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

    CONSTRAINT ck_source_objects_type CHECK (object_type IN ('PR', 'COMMIT', 'ISSUE', 'REVIEW', 'COMMENT')),
    CONSTRAINT ck_source_objects_provider_id_non_empty CHECK (length(btrim(provider_object_id)) > 0),
    CONSTRAINT ck_source_objects_lifecycle_state CHECK (lifecycle_state IN ('ACTIVE', 'DELETED', 'ORPHANED', 'POLICY_REVIEW_REQUIRED')),
    CONSTRAINT ck_source_objects_pr_association_state CHECK (
        pr_association_state IS NULL
        OR pr_association_state IN ('MATCHED_PR', 'NO_MATCH_OBSERVED', 'UNKNOWN')
    ),
    CONSTRAINT ck_source_objects_seen_order CHECK (last_seen_at >= first_seen_at),
    CONSTRAINT uq_source_objects_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_source_objects_resource_type_provider_id UNIQUE (source_resource_id, object_type, provider_object_id),
    CONSTRAINT fk_source_objects_same_workspace_resource
        FOREIGN KEY (workspace_id, source_resource_id)
        REFERENCES public.source_resources (workspace_id, id)
        ON DELETE CASCADE
);

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

    CONSTRAINT ck_source_object_revisions_content_hash_format CHECK (content_hash ~ '^(sha256:)?[0-9a-f]{64}$'),
    CONSTRAINT ck_source_object_revisions_hash_policy_non_empty CHECK (length(btrim(hash_policy_version)) > 0),
    CONSTRAINT ck_source_object_revisions_connector_version_non_empty CHECK (length(btrim(connector_version)) > 0),
    CONSTRAINT ck_source_object_revisions_schema_version_non_empty CHECK (length(btrim(schema_version)) > 0),
    CONSTRAINT ck_source_object_revisions_metadata_object CHECK (jsonb_typeof(normalized_metadata) = 'object'),
    CONSTRAINT ck_source_object_revisions_metadata_no_raw CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(normalized_metadata)),
    CONSTRAINT uq_source_object_revisions_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_source_object_revisions_object_content_hash UNIQUE (source_object_id, content_hash),
    CONSTRAINT fk_source_object_revisions_same_workspace_object
        FOREIGN KEY (workspace_id, source_object_id)
        REFERENCES public.source_objects (workspace_id, id)
        ON DELETE CASCADE
);

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
                'source_object_revisions DELETE requires explicit purge context for revision %',
                OLD.id
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_source_object_revisions_immutability ON public.source_object_revisions;
CREATE TRIGGER trg_source_object_revisions_immutability
    BEFORE UPDATE OR DELETE
    ON public.source_object_revisions
    FOR EACH ROW
    EXECUTE FUNCTION public.enforce_source_object_revision_immutability();

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

    CONSTRAINT ck_content_units_unit_type CHECK (
        unit_type IN (
            'PR_TITLE', 'PR_BODY_SECTION', 'ISSUE_SECTION', 'COMMENT',
            'REVIEW_COMMENT', 'COMMIT_MESSAGE', 'DIFF_HUNK', 'SYMBOL_CHANGE'
        )
    ),
    CONSTRAINT ck_content_units_anchor_object CHECK (jsonb_typeof(anchor) = 'object'),
    CONSTRAINT ck_content_units_anchor_parser_version CHECK (
        anchor ? 'parser_version'
        AND jsonb_typeof(anchor -> 'parser_version') = 'string'
        AND length(btrim(anchor ->> 'parser_version')) > 0
    ),
    CONSTRAINT ck_content_units_anchor_no_raw CHECK (NOT public.cm_jsonb_contains_forbidden_raw_key(anchor)),
    CONSTRAINT ck_content_units_text_content_null CHECK (text_content IS NULL),
    CONSTRAINT ck_content_units_content_ref_null CHECK (content_ref IS NULL),
    CONSTRAINT ck_content_units_retention_class_none CHECK (retention_class = 'NONE'),
    CONSTRAINT ck_content_units_expires_at_null CHECK (expires_at IS NULL),
    CONSTRAINT ck_content_units_content_hash_format CHECK (content_hash ~ '^(sha256:)?[0-9a-f]{64}$'),
    CONSTRAINT ck_content_units_canonicalization_version_non_empty CHECK (length(btrim(canonicalization_version)) > 0),
    CONSTRAINT ck_content_units_ordinal_non_negative CHECK (ordinal >= 0),
    CONSTRAINT ck_content_units_policy_state CHECK (policy_state IN ('ACTIVE', 'REDACTED', 'PURGED', 'BLOCKED')),
    CONSTRAINT uq_content_units_workspace_id_id UNIQUE (workspace_id, id),
    CONSTRAINT uq_content_units_revision_ordinal UNIQUE (source_object_revision_id, ordinal),
    CONSTRAINT fk_content_units_same_workspace_revision
        FOREIGN KEY (workspace_id, source_object_revision_id)
        REFERENCES public.source_object_revisions (workspace_id, id)
        ON DELETE CASCADE
);

ALTER TABLE public.source_resources ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_resources FORCE ROW LEVEL SECURITY;
ALTER TABLE public.source_objects ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_objects FORCE ROW LEVEL SECURITY;
ALTER TABLE public.source_object_revisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.source_object_revisions FORCE ROW LEVEL SECURITY;
ALTER TABLE public.content_units ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.content_units FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS source_resources_api_workspace_isolation ON public.source_resources;
CREATE POLICY source_resources_api_workspace_isolation ON public.source_resources
    FOR ALL TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS source_objects_api_workspace_isolation ON public.source_objects;
CREATE POLICY source_objects_api_workspace_isolation ON public.source_objects
    FOR ALL TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS source_object_revisions_api_workspace_isolation ON public.source_object_revisions;
CREATE POLICY source_object_revisions_api_workspace_isolation ON public.source_object_revisions
    FOR ALL TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

DROP POLICY IF EXISTS content_units_api_workspace_isolation ON public.content_units;
CREATE POLICY content_units_api_workspace_isolation ON public.content_units
    FOR ALL TO cm_api_app
    USING (workspace_id = auth_ctx.current_workspace_id())
    WITH CHECK (workspace_id = auth_ctx.current_workspace_id());

GRANT SELECT, INSERT, UPDATE, DELETE ON
    public.source_resources,
    public.source_objects,
    public.source_object_revisions,
    public.content_units
TO cm_api_app;

RESET ROLE;
"""


def _find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _pg_bin(name: str) -> str:
    candidate = _PG18_BIN_DIR / name
    if candidate.is_file():
        return str(candidate)
    resolved = shutil.which(name)
    if resolved is None:
        pytest.fail(
            f"PostgreSQL 18 binary {name!r} not found in {_PG18_BIN_DIR} or PATH."
        )
    return resolved


class EphemeralResolverPg18Cluster:
    """Disposable local PostgreSQL 18 cluster bound strictly to 127.0.0.1."""

    def __init__(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix="cm-m1-t103b-pg18-"))
        self._pgdata = self._tmpdir / "pgdata"
        self._logfile = self._tmpdir / "postgres.log"
        self.port = _find_free_local_port()
        self.superuser = "cm_pg_admin"
        self.dbname = "career_memory_resolver_test"
        self.api_login = "cm_api_login_resolver"
        self.api_password = "local_ephemeral_api_pw"

    @property
    def admin_dsn(self) -> str:
        return f"postgresql://{self.superuser}@127.0.0.1:{self.port}/{self.dbname}"

    @property
    def api_dsn(self) -> str:
        return (
            f"postgresql://{self.api_login}:{self.api_password}"
            f"@127.0.0.1:{self.port}/{self.dbname}"
        )

    def start(self) -> None:
        initdb = _pg_bin("initdb")
        pg_ctl = _pg_bin("pg_ctl")
        createdb = _pg_bin("createdb")

        env = os.environ.copy()
        env["LC_ALL"] = "C"

        subprocess.run(
            [
                initdb,
                "-D",
                str(self._pgdata),
                "-U",
                self.superuser,
                "--auth=trust",
                "--encoding=UTF8",
                "--no-locale",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        with (self._pgdata / "postgresql.conf").open("a", encoding="utf-8") as conf:
            conf.write(
                f"\nlisten_addresses = '127.0.0.1'\n"
                f"port = {self.port}\n"
                f"unix_socket_directories = '{self._tmpdir}'\n"
                f"fsync = off\n"
                f"synchronous_commit = off\n"
            )

        subprocess.run(
            [
                pg_ctl,
                "-D",
                str(self._pgdata),
                "-l",
                str(self._logfile),
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        subprocess.run(
            [
                createdb,
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                self.superuser,
                self.dbname,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )

        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                with psycopg.connect(self.admin_dsn, connect_timeout=1) as conn:
                    with conn.cursor() as cur:
                        cur.execute("SHOW server_version_num")
                        row = cur.fetchone()
                        assert row is not None and int(row[0]) >= 180000
                return
            except Exception:  # noqa: BLE001
                time.sleep(0.1)
        raise RuntimeError("Ephemeral PG18 cluster did not become ready.")

    def apply_schemas(self) -> None:
        sql_0010 = _MIGRATION_0010.read_text(encoding="utf-8")
        with psycopg.connect(self.admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(sql_0010)
                cur.execute(PROVENANCE_0020_TEST_DDL)
                cur.execute(
                    f"""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{self.api_login}') THEN
                            CREATE ROLE {self.api_login}
                                LOGIN PASSWORD '{self.api_password}'
                                NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                        END IF;
                    END
                    $$;
                    GRANT cm_api_app TO {self.api_login};
                    """
                )

    def stop(self) -> None:
        pg_ctl = _pg_bin("pg_ctl")
        if self._pgdata.exists():
            subprocess.run(
                [pg_ctl, "-D", str(self._pgdata), "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(self._tmpdir, ignore_errors=True)


def run_ephemeral_pg18_cluster() -> Iterator[EphemeralResolverPg18Cluster]:
    cluster = EphemeralResolverPg18Cluster()
    try:
        cluster.start()
        cluster.apply_schemas()
        yield cluster
    finally:
        cluster.stop()
