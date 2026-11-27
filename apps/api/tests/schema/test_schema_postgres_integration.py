"""Real local PostgreSQL 18 integration tests for M1-T102 & M1-T104 schemas.

Spins up an ephemeral local PostgreSQL 18 cluster via Homebrew ``initdb`` +
``pg_ctl`` bound strictly to ``127.0.0.1`` on a random free port inside a
temporary directory, and guarantees full process + directory teardown.

Verifies:
1. Fresh DB migration (0020 -> 0030), ownership, NOBYPASSRLS roles, RLS FORCE,
   25 Composite FKs, omission of deferred Post-MVP tables, and absence of a
   writable ``claims.verification`` column.
2. Upgrade path (0020 -> seed data -> 0030 -> idempotent re-run) and
   ``0010``-preexisting non-superuser ``cm_migrator`` compatibility.
3. Plan C (방안 C) raw-text non-persistence CHECK constraints and nested/array
   JSONB canary raw-key blocking (DATA-03, REF-04).
4. Revision immutability trigger on ``source_object_revisions`` (DATA-01).
5. Composite FK cross-workspace isolation & SQLSTATE 23503 anti-oracle parity
   across all parent-child relationships (both table owner and runtime roles).
6. Fail-closed RLS, connection-reuse transaction GUC isolation, and least-
   privilege GRANT matrix (worker denied users/workspaces/memberships/DELETE).
7. Domain invariants (fuzzy identity verification guard, work_events anchors,
   composite event self-ref & active duplicate guard, review_actions idempotency,
   MVP RULE contribution role restriction).
8. End-to-end metadata-only provenance traceability join and read-only computed
   ``v_claim_verification_summary`` across all 4 verification states.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pytest
from psycopg import errors as pg_errors
from psycopg.types.json import Jsonb

from career_memory_api.schema_contracts import (
    API_APP_ROLE,
    COMPOSITE_FK_SPEC,
    DEFERRED_POST_MVP_TABLES,
    MIGRATOR_ROLE,
    READONLY_PROVENANCE_VIEWS,
    TENANT_DOMAIN_TABLES,
    WORKER_APP_ROLE,
    ClaimVerification,
    apply_schema_migrations,
    clear_workspace_rls_context,
    fetch_claim_provenance_chain,
    fetch_claim_verification_summary,
    set_workspace_rls_context,
)

pytestmark = pytest.mark.postgres

_HOMEBREW_PG18_BIN = Path("/opt/homebrew/opt/postgresql@18/bin")
_SUPERUSER = "cm_pg_superuser"
_HASH_A = "sha256:" + ("a" * 64)
_HASH_B = "sha256:" + ("b" * 64)
_HASH_C = "sha256:" + ("c" * 64)
_HASH_D = "sha256:" + ("d" * 64)


def _find_pg_binary(name: str) -> str:
    candidate = _HOMEBREW_PG18_BIN / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    found = shutil.which(name)
    if found is not None:
        return found
    pytest.fail(
        f"PostgreSQL 18 binary {name!r} not found at {_HOMEBREW_PG18_BIN} or PATH."
    )


def _reserve_local_ephemeral_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@dataclass(frozen=True, slots=True)
class EphemeralPgCluster:
    host: str
    port: int
    superuser: str
    default_db: str

    def dsn(self, *, dbname: str | None = None, user: str | None = None) -> str:
        target_db = dbname or self.default_db
        target_user = user or self.superuser
        return f"postgresql://{target_user}@{self.host}:{self.port}/{target_db}"


@pytest.fixture(scope="module")
def pg18_cluster() -> Iterator[EphemeralPgCluster]:
    initdb_bin = _find_pg_binary("initdb")
    pg_ctl_bin = _find_pg_binary("pg_ctl")
    port = _reserve_local_ephemeral_port()

    temp_root = Path(tempfile.mkdtemp(prefix="cm-pg18-schema-"))
    data_dir = temp_root / "pgdata"
    socket_dir = temp_root / "run"
    log_file = temp_root / "postgres.log"
    socket_dir.mkdir(parents=True, exist_ok=True)

    started = False
    try:
        subprocess.run(
            [
                initdb_bin,
                "-D",
                str(data_dir),
                "-U",
                _SUPERUSER,
                "--auth=trust",
                "--encoding=UTF8",
                "--locale=C",
                "--no-instructions",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        pg_opts = (
            f"-h 127.0.0.1 -p {port} -k {socket_dir} "
            "-c fsync=off -c synchronous_commit=off -c full_page_writes=off"
        )
        subprocess.run(
            [
                pg_ctl_bin,
                "-D",
                str(data_dir),
                "-l",
                str(log_file),
                "-o",
                pg_opts,
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        started = True

        cluster = EphemeralPgCluster(
            host="127.0.0.1",
            port=port,
            superuser=_SUPERUSER,
            default_db="postgres",
        )
        with psycopg.connect(cluster.dsn()) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW server_version_num")
                row = cur.fetchone()
                assert row is not None
                assert int(row[0]) >= 180000, f"Expected PostgreSQL 18+, got {row[0]}"
        yield cluster
    finally:
        if started:
            subprocess.run(
                [pg_ctl_bin, "-D", str(data_dir), "-m", "immediate", "-w", "stop"],
                check=False,
                capture_output=True,
                text=True,
            )
        shutil.rmtree(temp_root, ignore_errors=True)


def _create_isolated_database(cluster: EphemeralPgCluster, prefix: str) -> str:
    dbname = f"{prefix}_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(cluster.dsn(), autocommit=True) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{dbname}"')
    return dbname


def _drop_isolated_database(cluster: EphemeralPgCluster, dbname: str) -> None:
    with psycopg.connect(cluster.dsn(), autocommit=True) as admin_conn:
        with admin_conn.cursor() as cur:
            cur.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (dbname,),
            )
            cur.execute(f'DROP DATABASE IF EXISTS "{dbname}"')


@pytest.fixture()
def migrated_db(pg18_cluster: EphemeralPgCluster) -> Iterator[tuple[EphemeralPgCluster, str]]:
    dbname = _create_isolated_database(pg18_cluster, "cm_schema")
    try:
        with psycopg.connect(pg18_cluster.dsn(dbname=dbname)) as conn:
            applied = apply_schema_migrations(conn)
            assert applied == [
                "0020_source_provenance_metadata.sql",
                "0030_events_facts_claims_contributions.sql",
            ]
        yield pg18_cluster, dbname
    finally:
        _drop_isolated_database(pg18_cluster, dbname)


@dataclass(frozen=True, slots=True)
class SeededWorkspaceGraph:
    workspace_id: uuid.UUID
    account_id: uuid.UUID
    resource_id: uuid.UUID
    source_object_id: uuid.UUID
    revision_id: uuid.UUID
    content_unit_id: uuid.UUID
    identity_id: uuid.UUID
    work_event_id: uuid.UUID
    fact_id: uuid.UUID
    fact_support_id: uuid.UUID
    contribution_id: uuid.UUID
    generation_run_id: uuid.UUID
    claim_id: uuid.UUID
    claim_atom_id: uuid.UUID
    claim_atom_support_id: uuid.UUID


def _seed_workspace_graph(
    conn: psycopg.Connection, *, slug_suffix: str
) -> SeededWorkspaceGraph:
    ws_id = uuid.uuid4()
    acct_id = uuid.uuid4()
    res_id = uuid.uuid4()
    obj_id = uuid.uuid4()
    rev_id = uuid.uuid4()
    cu_id = uuid.uuid4()
    ident_id = uuid.uuid4()
    event_id = uuid.uuid4()
    fact_id = uuid.uuid4()
    fs_id = uuid.uuid4()
    contrib_id = uuid.uuid4()
    gen_run_id = uuid.uuid4()
    claim_id = uuid.uuid4()
    atom_id = uuid.uuid4()
    cas_id = uuid.uuid4()

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO public.workspaces (id, slug, name) VALUES (%s, %s, %s)",
            (ws_id, f"ws-{slug_suffix}-{ws_id.hex[:6]}", f"Workspace {slug_suffix}"),
        )
        cur.execute(
            """
            INSERT INTO public.source_accounts
                (id, workspace_id, provider, external_account_id, token_secret_ref, scopes, status)
            VALUES (%s, %s, 'GITHUB', %s, %s, ARRAY['repo:read'], 'ACTIVE')
            """,
            (
                acct_id,
                ws_id,
                f"gh-acct-{slug_suffix}",
                f"secret://github/{ws_id}/acct",
            ),
        )
        cur.execute(
            """
            INSERT INTO public.source_resources
                (id, workspace_id, source_account_id, provider_resource_id, resource_type,
                 name, canonical_url, visibility, processing_mode, raw_storage_mode,
                 raw_retention_seconds, status, policy_version)
            VALUES
                (%s, %s, %s, %s, 'GITHUB_REPOSITORY',
                 %s, %s, 'PRIVATE', 'PRIVATE_CLOUD_OPT_IN', 'NONE',
                 NULL, 'ACTIVE', 'policy-v1')
            """,
            (
                res_id,
                ws_id,
                acct_id,
                f"repo-{slug_suffix}",
                f"acme/repo-{slug_suffix}",
                f"https://github.com/acme/repo-{slug_suffix}",
            ),
        )
        cur.execute(
            """
            INSERT INTO public.source_objects
                (id, workspace_id, source_resource_id, object_type, provider_object_id,
                 lifecycle_state, pr_association_state)
            VALUES (%s, %s, %s, 'PR', %s, 'ACTIVE', 'MATCHED_PR')
            """,
            (obj_id, ws_id, res_id, f"pr-101-{slug_suffix}"),
        )
        cur.execute(
            """
            INSERT INTO public.source_object_revisions
                (id, workspace_id, source_object_id, provider_revision_id, content_hash,
                 hash_policy_version, is_tombstone, normalized_metadata,
                 connector_version, schema_version)
            VALUES (%s, %s, %s, %s, %s, 'hash-v1', false, %s, 'gh-conn-0.1', 'schema-0.1')
            """,
            (
                rev_id,
                ws_id,
                obj_id,
                f"rev-etag-{slug_suffix}",
                _HASH_A,
                Jsonb({"provider_number": 101, "merged": True, "unit_count": 1}),
            ),
        )
        cur.execute(
            """
            INSERT INTO public.content_units
                (id, workspace_id, source_object_revision_id, unit_type, anchor,
                 text_content, content_ref, content_hash, canonicalization_version,
                 retention_class, expires_at, language, ordinal, policy_state)
            VALUES (%s, %s, %s, 'PR_BODY_SECTION', %s,
                    NULL, NULL, %s, 'canon-v1', 'NONE', NULL, 'en', 0, 'ACTIVE')
            """,
            (
                cu_id,
                ws_id,
                rev_id,
                Jsonb(
                    {
                        "parser_version": "md-parser-v1",
                        "offset_unit": "char",
                        "char_start": 40,
                        "char_end": 180,
                        "span_sha256": _HASH_B,
                    }
                ),
                _HASH_B,
            ),
        )
        cur.execute(
            """
            INSERT INTO public.identities
                (id, workspace_id, identity_kind, canonical_name, primary_email, is_self, status)
            VALUES (%s, %s, 'PERSON', %s, %s, true, 'ACTIVE')
            """,
            (ident_id, ws_id, f"Alex {slug_suffix}", f"alex-{slug_suffix}@example.com"),
        )
        cur.execute(
            """
            INSERT INTO public.work_events
                (id, workspace_id, event_kind, title, primary_anchor_source_object_id, status)
            VALUES (%s, %s, 'PR', %s, %s, 'CONFIRMED')
            """,
            (event_id, ws_id, f"Optimize checkout latency ({slug_suffix})", obj_id),
        )
        cur.execute(
            """
            INSERT INTO public.facts
                (id, workspace_id, event_id, fact_type, statement, status, extractor_version)
            VALUES (%s, %s, %s, 'ACTION', 'Added Redis read-through cache to checkout', 'SUPPORTED', 'ext-v1')
            """,
            (fact_id, ws_id, event_id),
        )
        cur.execute(
            """
            INSERT INTO public.fact_support
                (id, workspace_id, fact_id, content_unit_id, support_type, extracted_span,
                 support_metadata, validator, validator_version, status)
            VALUES (%s, %s, %s, %s, 'DIRECT', NULL, %s, 'RULE', 'val-v1', 'ACTIVE')
            """,
            (
                fs_id,
                ws_id,
                fact_id,
                cu_id,
                Jsonb({"locator_id": "loc-001", "char_start": 40, "char_end": 180}),
            ),
        )
        cur.execute(
            """
            INSERT INTO public.contributions
                (id, workspace_id, event_id, identity_id, role, origin, status)
            VALUES (%s, %s, %s, %s, 'IMPLEMENTED', 'RULE', 'SOURCE_SUPPORTED')
            """,
            (contrib_id, ws_id, event_id, ident_id),
        )
        cur.execute(
            """
            INSERT INTO public.contribution_support
                (workspace_id, contribution_id, fact_id, status)
            VALUES (%s, %s, %s, 'ACTIVE')
            """,
            (ws_id, contrib_id, fact_id),
        )
        cur.execute(
            """
            INSERT INTO public.generation_runs
                (id, workspace_id, event_id, run_stage, model_id, prompt_version,
                 input_artifact_versions, run_metadata, status)
            VALUES (%s, %s, %s, 'CLAIM_GENERATION', 'gemini-synthetic-v1', 'prompt-v1',
                    %s, %s, 'SUCCEEDED')
            """,
            (
                gen_run_id,
                ws_id,
                event_id,
                Jsonb({"fact_ids": [str(fact_id)], "schema_version": "0.1"}),
                Jsonb({"latency_ms": 42}),
            ),
        )
        cur.execute(
            """
            INSERT INTO public.claims
                (id, workspace_id, event_id, generation_run_id, claim_version, claim_text, status)
            VALUES (%s, %s, %s, %s, 1, 'Implemented Redis read-through caching for checkout.', 'SUPPORT_VALIDATED')
            """,
            (claim_id, ws_id, event_id, gen_run_id),
        )
        cur.execute(
            """
            INSERT INTO public.claim_atoms
                (id, workspace_id, claim_id, ordinal, atom_type, atom_text, status)
            VALUES (%s, %s, %s, 0, 'ACTION', 'Implemented Redis read-through caching for checkout.', 'SUPPORTED')
            """,
            (atom_id, ws_id, claim_id),
        )
        cur.execute(
            """
            INSERT INTO public.claim_atom_support
                (id, workspace_id, claim_atom_id, fact_id, support_type, status)
            VALUES (%s, %s, %s, %s, 'DIRECT', 'ACTIVE')
            """,
            (cas_id, ws_id, atom_id, fact_id),
        )
    conn.commit()
    return SeededWorkspaceGraph(
        workspace_id=ws_id,
        account_id=acct_id,
        resource_id=res_id,
        source_object_id=obj_id,
        revision_id=rev_id,
        content_unit_id=cu_id,
        identity_id=ident_id,
        work_event_id=event_id,
        fact_id=fact_id,
        fact_support_id=fs_id,
        contribution_id=contrib_id,
        generation_run_id=gen_run_id,
        claim_id=claim_id,
        claim_atom_id=atom_id,
        claim_atom_support_id=cas_id,
    )


def test_fresh_migration_catalog_roles_rls_composite_fks_and_no_deferred_tables(
    migrated_db: tuple[EphemeralPgCluster, str],
) -> None:
    cluster, dbname = migrated_db
    with psycopg.connect(cluster.dsn(dbname=dbname)) as conn:
        with conn.cursor() as cur:
            # 1. Roles must be non-superuser and NOBYPASSRLS.
            cur.execute(
                """
                SELECT rolname, rolsuper, rolbypassrls
                FROM pg_roles
                WHERE rolname = ANY(%s)
                ORDER BY rolname
                """,
                ([API_APP_ROLE, MIGRATOR_ROLE, WORKER_APP_ROLE],),
            )
            role_rows = cur.fetchall()
            assert role_rows == [
                (API_APP_ROLE, False, False),
                (MIGRATOR_ROLE, False, False),
                (WORKER_APP_ROLE, False, False),
            ]

            # 2. All 19 domain tables exist, owned by cm_migrator, with ENABLE + FORCE RLS.
            cur.execute(
                """
                SELECT c.relname, pg_get_userbyid(c.relowner), c.relrowsecurity, c.relforcerowsecurity
                FROM pg_class AS c
                INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind = 'r'
                """
            )
            table_meta = {row[0]: row[1:] for row in cur.fetchall()}
            for table_name in TENANT_DOMAIN_TABLES:
                assert table_name in table_meta, f"Missing table {table_name}"
                owner, rls_enabled, rls_forced = table_meta[table_name]
                assert owner == MIGRATOR_ROLE
                assert rls_enabled is True
                assert rls_forced is True

            # 3. Deferred Post-MVP tables MUST NOT exist.
            for deferred_table in DEFERRED_POST_MVP_TABLES:
                assert deferred_table not in table_meta

            # 4. Read-only views exist and have security_invoker = true.
            cur.execute(
                """
                SELECT c.relname, c.reloptions
                FROM pg_class AS c
                INNER JOIN pg_namespace AS n ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relkind = 'v'
                """
            )
            views_meta = {row[0]: row[1] for row in cur.fetchall()}
            for view_name in READONLY_PROVENANCE_VIEWS:
                assert view_name in views_meta
                assert views_meta[view_name] is not None
                assert "security_invoker=true" in views_meta[view_name]

            # 5. All 25 Composite FKs exist on (workspace_id, <fk_col>) -> parent(workspace_id, id).
            cur.execute(
                """
                SELECT
                    child.relname AS child_table,
                    parent.relname AS parent_table,
                    ARRAY(
                        SELECT att.attname
                        FROM unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord)
                        JOIN pg_attribute AS att
                          ON att.attrelid = con.conrelid AND att.attnum = k.attnum
                        ORDER BY k.ord
                    ) AS child_cols,
                    ARRAY(
                        SELECT att.attname
                        FROM unnest(con.confkey) WITH ORDINALITY AS k(attnum, ord)
                        JOIN pg_attribute AS att
                          ON att.attrelid = con.confrelid AND att.attnum = k.attnum
                        ORDER BY k.ord
                    ) AS parent_cols
                FROM pg_constraint AS con
                JOIN pg_class AS child ON child.oid = con.conrelid
                JOIN pg_class AS parent ON parent.oid = con.confrelid
                JOIN pg_namespace AS ns ON ns.oid = child.relnamespace
                WHERE con.contype = 'f'
                  AND ns.nspname = 'public'
                """
            )
            actual_composite_fks: set[tuple[str, str, str]] = set()
            for child_table, parent_table, child_cols, parent_cols in cur.fetchall():
                if parent_table == "workspaces":
                    assert child_cols == ["workspace_id"]
                    assert parent_cols == ["id"]
                    continue
                if child_table == "workspace_memberships":
                    continue
                assert child_cols[0] == "workspace_id", (
                    f"Non-composite FK detected on {child_table} -> {parent_table}: {child_cols}"
                )
                assert parent_cols == ["workspace_id", "id"], (
                    f"Unexpected parent FK target on {child_table} -> {parent_table}: {parent_cols}"
                )
                actual_composite_fks.add((child_table, child_cols[1], parent_table))

            assert actual_composite_fks == set(COMPOSITE_FK_SPEC)

            # 6. `claims` MUST NOT have a writable `verification` column.
            cur.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND table_name = 'claims'
                """
            )
            claim_cols = {row[0] for row in cur.fetchall()}
            assert "verification" not in claim_cols


def test_upgrade_path_and_0010_preexisting_migrator_compatibility(
    pg18_cluster: EphemeralPgCluster,
) -> None:
    # Scenario A: 0020 applied first -> live 0020 data seeded -> 0030 upgraded -> full re-run idempotency.
    upgrade_db = _create_isolated_database(pg18_cluster, "cm_upgrade")
    try:
        with psycopg.connect(pg18_cluster.dsn(dbname=upgrade_db)) as conn:
            assert apply_schema_migrations(conn, versions=["0020"]) == [
                "0020_source_provenance_metadata.sql"
            ]
            ws_id = uuid.uuid4()
            acct_id = uuid.uuid4()
            res_id = uuid.uuid4()
            obj_id = uuid.uuid4()
            rev_id = uuid.uuid4()
            cu_id = uuid.uuid4()
            with conn.cursor() as cur:
                cur.execute("INSERT INTO public.workspaces (id, slug, name) VALUES (%s, 'up-1', 'Upgrade WS')", (ws_id,))
                cur.execute(
                    """
                    INSERT INTO public.source_accounts
                        (id, workspace_id, provider, external_account_id, token_secret_ref)
                    VALUES (%s, %s, 'GITHUB', 'gh-up', 'secret://github/up')
                    """,
                    (acct_id, ws_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_resources
                        (id, workspace_id, source_account_id, provider_resource_id, resource_type,
                         name, canonical_url, visibility, processing_mode, policy_version)
                    VALUES
                        (%s, %s, %s, 'repo-up', 'GITHUB_REPOSITORY',
                         'acme/up', 'https://github.com/acme/up', 'PUBLIC', 'PUBLIC_CLOUD', 'v1')
                    """,
                    (res_id, ws_id, acct_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_objects
                        (id, workspace_id, source_resource_id, object_type, provider_object_id)
                    VALUES (%s, %s, %s, 'PR', 'pr-up-1')
                    """,
                    (obj_id, ws_id, res_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_object_revisions
                        (id, workspace_id, source_object_id, content_hash, hash_policy_version,
                         connector_version, schema_version)
                    VALUES (%s, %s, %s, %s, 'hash-v1', 'conn-1', 'schema-1')
                    """,
                    (rev_id, ws_id, obj_id, _HASH_A),
                )
                cur.execute(
                    """
                    INSERT INTO public.content_units
                        (id, workspace_id, source_object_revision_id, unit_type, anchor,
                         content_hash, canonicalization_version, ordinal)
                    VALUES (%s, %s, %s, 'PR_TITLE', %s, %s, 'canon-v1', 0)
                    """,
                    (cu_id, ws_id, rev_id, Jsonb({"parser_version": "p-v1"}), _HASH_B),
                )
            conn.commit()

            # Upgrade by applying 0030 over populated 0020 DB.
            assert apply_schema_migrations(conn, versions=["0030"]) == [
                "0030_events_facts_claims_contributions.sql"
            ]

            # Link newly created 0030 tables to pre-existing 0020 rows.
            ev_id = uuid.uuid4()
            f_id = uuid.uuid4()
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.work_events
                        (id, workspace_id, event_kind, title, primary_anchor_source_object_id)
                    VALUES (%s, %s, 'PR', 'Upgraded event', %s)
                    """,
                    (ev_id, ws_id, obj_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.facts
                        (id, workspace_id, event_id, fact_type, statement, status, extractor_version)
                    VALUES (%s, %s, %s, 'ACTION', 'Upgraded fact', 'SUPPORTED', 'ext-v1')
                    """,
                    (f_id, ws_id, ev_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.fact_support
                        (workspace_id, fact_id, content_unit_id, support_type, validator, validator_version)
                    VALUES (%s, %s, %s, 'DIRECT', 'RULE', 'val-v1')
                    """,
                    (ws_id, f_id, cu_id),
                )
            conn.commit()

            # Re-run both 0020 and 0030 to verify full idempotency without data loss.
            assert apply_schema_migrations(conn) == [
                "0020_source_provenance_metadata.sql",
                "0030_events_facts_claims_contributions.sql",
            ]
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.content_units WHERE id = %s", (cu_id,))
                assert cur.fetchone() == (1,)
                cur.execute("SELECT COUNT(*) FROM public.fact_support WHERE fact_id = %s", (f_id,))
                assert cur.fetchone() == (1,)
    finally:
        _drop_isolated_database(pg18_cluster, upgrade_db)

    # Scenario B: Simulated 0010 pre-bootstrap + running 0020 & 0030 as non-superuser cm_migrator.
    compat_db = _create_isolated_database(pg18_cluster, "cm_0010_compat")
    try:
        with psycopg.connect(pg18_cluster.dsn(dbname=compat_db)) as admin_conn:
            with admin_conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE SCHEMA IF NOT EXISTS auth_ctx AUTHORIZATION cm_migrator;
                    GRANT USAGE, CREATE ON SCHEMA public TO cm_migrator;
                    GRANT USAGE ON SCHEMA public TO cm_api_app, cm_worker_app;
                    """
                )
                cur.execute("SET ROLE cm_migrator")
                cur.execute(
                    """
                    CREATE TABLE public.users (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                        email text NULL
                    );
                    CREATE TABLE public.workspaces (
                        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                        name text NOT NULL
                    );
                    CREATE TABLE public.workspace_memberships (
                        workspace_id uuid NOT NULL REFERENCES public.workspaces(id) ON DELETE CASCADE,
                        user_id uuid NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
                        revoked_at timestamptz NULL,
                        PRIMARY KEY (workspace_id, user_id)
                    );
                    CREATE POLICY existing_0010_ws_select ON public.workspaces
                        FOR SELECT TO cm_api_app USING (true);
                    RESET ROLE;
                    """
                )
            admin_conn.commit()

        # Now apply 0020 and 0030 directly as non-superuser cm_migrator.
        with psycopg.connect(pg18_cluster.dsn(dbname=compat_db, user=MIGRATOR_ROLE)) as mig_conn:
            assert apply_schema_migrations(mig_conn) == [
                "0020_source_provenance_metadata.sql",
                "0030_events_facts_claims_contributions.sql",
            ]
    finally:
        _drop_isolated_database(pg18_cluster, compat_db)


def test_plan_c_raw_text_non_persistence_and_canary_jsonb_blocking(
    migrated_db: tuple[EphemeralPgCluster, str],
) -> None:
    cluster, dbname = migrated_db
    with psycopg.connect(cluster.dsn(dbname=dbname)) as conn:
        g = _seed_workspace_graph(conn, slug_suffix="planc")

        # 1. Raw GitHub token rejected in source_accounts.token_secret_ref.
        with pytest.raises(pg_errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_accounts
                        (workspace_id, provider, external_account_id, token_secret_ref)
                    VALUES (%s, 'GITHUB', 'gh-bad', 'ghp_CANARY_RAW_GITHUB_TOKEN_12345')
                    """,
                    (g.workspace_id,),
                )
        conn.rollback()

        # 2. source_resources.raw_storage_mode != 'NONE' rejected.
        with pytest.raises(pg_errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_resources
                        (workspace_id, source_account_id, provider_resource_id, resource_type,
                         name, canonical_url, visibility, processing_mode, raw_storage_mode,
                         policy_version)
                    VALUES (%s, %s, 'repo-bad-mode', 'GITHUB_REPOSITORY',
                            'acme/bad', 'https://github.com/acme/bad', 'PRIVATE',
                            'PRIVATE_LOCAL', 'EPHEMERAL', 'v1')
                    """,
                    (g.workspace_id, g.account_id),
                )
        conn.rollback()

        # 3. source_resources.raw_retention_seconds IS NOT NULL rejected.
        with pytest.raises(pg_errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.source_resources
                        (workspace_id, source_account_id, provider_resource_id, resource_type,
                         name, canonical_url, visibility, processing_mode,
                         raw_retention_seconds, policy_version)
                    VALUES (%s, %s, 'repo-bad-ret', 'GITHUB_REPOSITORY',
                            'acme/bad2', 'https://github.com/acme/bad2', 'PRIVATE',
                            'PRIVATE_LOCAL', 300, 'v1')
                    """,
                    (g.workspace_id, g.account_id),
                )
        conn.rollback()

        # 4. content_units: non-NULL text_content, content_ref, expires_at, non-NONE retention_class rejected.
        bad_unit_cases = [
            ("text_content", "CANARY_RAW_TEXT", None, "NONE", None, {"parser_version": "v1"}),
            ("content_ref", None, "s3://bucket/canary.txt", "NONE", None, {"parser_version": "v1"}),
            ("retention_class", None, None, "EPHEMERAL", None, {"parser_version": "v1"}),
            ("expires_at", None, None, "NONE", "2027-01-01T00:00:00Z", {"parser_version": "v1"}),
            ("anchor_no_parser_version", None, None, "NONE", None, {"char_start": 0, "char_end": 5}),
            (
                "anchor_nested_canary",
                None,
                None,
                "NONE",
                None,
                {"parser_version": "v1", "nested": [{"extracted_span": "CANARY_ANCHOR_SPAN"}]},
            ),
        ]
        for idx, (_label, text_c, c_ref, ret_cls, exp_at, anchor_val) in enumerate(bad_unit_cases, start=1):
            with pytest.raises(pg_errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.content_units
                            (workspace_id, source_object_revision_id, unit_type, anchor,
                             text_content, content_ref, content_hash, canonicalization_version,
                             retention_class, expires_at, ordinal)
                        VALUES (%s, %s, 'COMMENT', %s, %s, %s, %s, 'canon-v1', %s, %s, %s)
                        """,
                        (
                            g.workspace_id,
                            g.revision_id,
                            Jsonb(anchor_val),
                            text_c,
                            c_ref,
                            _HASH_C,
                            ret_cls,
                            exp_at,
                            100 + idx,
                        ),
                    )
            conn.rollback()

        # 5. source_object_revisions.normalized_metadata rejects top-level, nested, and array raw keys.
        for idx, bad_meta in enumerate(
            [
                {"body": "CANARY_TOP_LEVEL_BODY"},
                {"nested": {"deep": {"raw_text": "CANARY_DEEP_RAW_TEXT"}}},
                {"items": [{"safe": True}, {"Quote": "CANARY_ARRAY_MIXED_CASE_QUOTE"}]},
                {"diff": "@@ -1,3 +1,3 @@ CANARY_DIFF"},
            ],
            start=1,
        ):
            with pytest.raises(pg_errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.source_object_revisions
                            (workspace_id, source_object_id, content_hash, hash_policy_version,
                             normalized_metadata, connector_version, schema_version)
                        VALUES (%s, %s, %s, 'hash-v1', %s, 'conn-1', 'schema-1')
                        """,
                        (
                            g.workspace_id,
                            g.source_object_id,
                            f"sha256:{idx:064x}",
                            Jsonb(bad_meta),
                        ),
                    )
            conn.rollback()

        # 6. fact_support: extracted_span IS NOT NULL and support_metadata canary keys rejected.
        with pytest.raises(pg_errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.fact_support
                        (workspace_id, fact_id, content_unit_id, support_type,
                         extracted_span, validator, validator_version)
                    VALUES (%s, %s, %s, 'CONTEXT', 'CANARY_EXTRACTED_SPAN', 'RULE', 'v1')
                    """,
                    (g.workspace_id, g.fact_id, g.content_unit_id),
                )
        conn.rollback()

        with pytest.raises(pg_errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.fact_support
                        (workspace_id, fact_id, content_unit_id, support_type,
                         support_metadata, validator, validator_version)
                    VALUES (%s, %s, %s, 'CONTEXT', %s, 'RULE', 'v1')
                    """,
                    (
                        g.workspace_id,
                        g.fact_id,
                        g.content_unit_id,
                        Jsonb({"nested": [{"raw_body": "CANARY_SUPPORT_META"}]}),
                    ),
                )
        conn.rollback()

        # 7. generation_runs rejects prompt/raw_payload smuggling in input_artifact_versions & run_metadata.
        for bad_inputs, bad_meta in [
            ({"prompt": "CANARY_PROMPT_IN_INPUTS"}, {}),
            ({"fact_ids": []}, {"trace": [{"raw_payload": "CANARY_RAW_PAYLOAD"}]}),
        ]:
            with pytest.raises(pg_errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.generation_runs
                            (workspace_id, event_id, run_stage, model_id, prompt_version,
                             input_artifact_versions, run_metadata)
                        VALUES (%s, %s, 'CLAIM_GENERATION', 'model-1', 'pv-1', %s, %s)
                        """,
                        (
                            g.workspace_id,
                            g.work_event_id,
                            Jsonb(bad_inputs),
                            Jsonb(bad_meta),
                        ),
                    )
            conn.rollback()


def test_source_object_revision_immutability_data01(
    migrated_db: tuple[EphemeralPgCluster, str],
) -> None:
    cluster, dbname = migrated_db
    with psycopg.connect(cluster.dsn(dbname=dbname)) as conn:
        g = _seed_workspace_graph(conn, slug_suffix="immut")

        # UPDATE content_hash must fail (DATA-01).
        with pytest.raises(pg_errors.IntegrityError) as hash_exc:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.source_object_revisions SET content_hash = %s WHERE id = %s",
                    (_HASH_D, g.revision_id),
                )
        assert hash_exc.value.sqlstate == "23000"
        conn.rollback()

        # UPDATE observed_at must fail (DATA-01).
        with pytest.raises(pg_errors.IntegrityError) as ts_exc:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE public.source_object_revisions SET observed_at = now() + interval '1 day' WHERE id = %s",
                    (g.revision_id,),
                )
        assert ts_exc.value.sqlstate == "23000"
        conn.rollback()

        # DELETE without explicit purge context must fail.
        with pytest.raises(pg_errors.IntegrityError) as del_exc:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM public.source_object_revisions WHERE id = %s",
                    (g.revision_id,),
                )
        assert del_exc.value.sqlstate == "23000"
        conn.rollback()

        # DELETE WITH explicit transaction-local purge context succeeds.
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.allow_revision_purge = 'on'")
            cur.execute(
                "DELETE FROM public.source_object_revisions WHERE id = %s",
                (g.revision_id,),
            )
            assert cur.rowcount == 1
        conn.commit()


def test_composite_fk_blocks_cross_workspace_parents_for_owner_and_runtime_with_anti_oracle_parity(
    migrated_db: tuple[EphemeralPgCluster, str],
) -> None:
    cluster, dbname = migrated_db
    with psycopg.connect(cluster.dsn(dbname=dbname)) as setup_conn:
        ws_a = _seed_workspace_graph(setup_conn, slug_suffix="alpha")
        ws_b = _seed_workspace_graph(setup_conn, slug_suffix="beta")

    nonexistent = uuid.uuid4()

    # Each case attempts to insert a child in ws_a pointing to a parent UUID from ws_b
    # vs a completely nonexistent UUID, verifying identical SQLSTATE 23503 (ForeignKeyViolation).
    def cross_ws_cases(target_parent_id: uuid.UUID, case_name: str) -> tuple[str, tuple[object, ...]]:
        cases: dict[str, tuple[str, tuple[object, ...]]] = {
            "source_resources->source_accounts": (
                """
                INSERT INTO public.source_resources
                    (workspace_id, source_account_id, provider_resource_id, resource_type,
                     name, canonical_url, visibility, processing_mode, policy_version)
                VALUES (%s, %s, %s, 'GITHUB_REPOSITORY', 'acme/x', 'https://github.com/acme/x',
                        'PUBLIC', 'PUBLIC_CLOUD', 'v1')
                """,
                (ws_a.workspace_id, target_parent_id, f"pr-{uuid.uuid4().hex[:8]}"),
            ),
            "source_objects->source_resources": (
                """
                INSERT INTO public.source_objects
                    (workspace_id, source_resource_id, object_type, provider_object_id)
                VALUES (%s, %s, 'ISSUE', %s)
                """,
                (ws_a.workspace_id, target_parent_id, f"iss-{uuid.uuid4().hex[:8]}"),
            ),
            "source_object_revisions->source_objects": (
                """
                INSERT INTO public.source_object_revisions
                    (workspace_id, source_object_id, content_hash, hash_policy_version,
                     connector_version, schema_version)
                VALUES (%s, %s, %s, 'hash-v1', 'conn-1', 'schema-1')
                """,
                (ws_a.workspace_id, target_parent_id, _HASH_C),
            ),
            "content_units->source_object_revisions": (
                """
                INSERT INTO public.content_units
                    (workspace_id, source_object_revision_id, unit_type, anchor,
                     content_hash, canonicalization_version, ordinal)
                VALUES (%s, %s, 'COMMENT', %s, %s, 'canon-v1', 9)
                """,
                (ws_a.workspace_id, target_parent_id, Jsonb({"parser_version": "v1"}), _HASH_C),
            ),
            "external_identity_mappings->identities": (
                """
                INSERT INTO public.external_identity_mappings
                    (workspace_id, identity_id, provider, external_login, verification_method, status)
                VALUES (%s, %s, 'GITHUB', 'cross-ws-user', 'FUZZY_HEURISTIC', 'CANDIDATE')
                """,
                (ws_a.workspace_id, target_parent_id),
            ),
            "work_events->source_objects": (
                """
                INSERT INTO public.work_events
                    (workspace_id, event_kind, title, primary_anchor_source_object_id)
                VALUES (%s, 'PR', 'Cross-ws anchor', %s)
                """,
                (ws_a.workspace_id, target_parent_id),
            ),
            "event_memberships->work_events": (
                """
                INSERT INTO public.event_memberships
                    (workspace_id, event_id, source_object_id, membership_role)
                VALUES (%s, %s, %s, 'CONTEXT_ONLY')
                """,
                (ws_a.workspace_id, target_parent_id, ws_a.source_object_id),
            ),
            "event_memberships->source_objects": (
                """
                INSERT INTO public.event_memberships
                    (workspace_id, event_id, source_object_id, membership_role)
                VALUES (%s, %s, %s, 'CONTEXT_ONLY')
                """,
                (ws_a.workspace_id, ws_a.work_event_id, target_parent_id),
            ),
            "event_composite_memberships->composite": (
                """
                INSERT INTO public.event_composite_memberships
                    (workspace_id, composite_event_id, member_event_id)
                VALUES (%s, %s, %s)
                """,
                (ws_a.workspace_id, target_parent_id, ws_a.work_event_id),
            ),
            "event_composite_memberships->member": (
                """
                INSERT INTO public.event_composite_memberships
                    (workspace_id, composite_event_id, member_event_id)
                VALUES (%s, %s, %s)
                """,
                (ws_a.workspace_id, ws_a.work_event_id, target_parent_id),
            ),
            "facts->work_events": (
                """
                INSERT INTO public.facts
                    (workspace_id, event_id, fact_type, statement, extractor_version)
                VALUES (%s, %s, 'ACTION', 'Cross-ws fact', 'ext-v1')
                """,
                (ws_a.workspace_id, target_parent_id),
            ),
            "fact_support->facts": (
                """
                INSERT INTO public.fact_support
                    (workspace_id, fact_id, content_unit_id, support_type, validator, validator_version)
                VALUES (%s, %s, %s, 'CONTEXT', 'RULE', 'v1')
                """,
                (ws_a.workspace_id, target_parent_id, ws_a.content_unit_id),
            ),
            "fact_support->content_units": (
                """
                INSERT INTO public.fact_support
                    (workspace_id, fact_id, content_unit_id, support_type, validator, validator_version)
                VALUES (%s, %s, %s, 'CONTEXT', 'RULE', 'v1')
                """,
                (ws_a.workspace_id, ws_a.fact_id, target_parent_id),
            ),
            "contributions->work_events": (
                """
                INSERT INTO public.contributions
                    (workspace_id, event_id, identity_id, role, origin, status)
                VALUES (%s, %s, %s, 'REVIEWED', 'RULE', 'CANDIDATE')
                """,
                (ws_a.workspace_id, target_parent_id, ws_a.identity_id),
            ),
            "contributions->identities": (
                """
                INSERT INTO public.contributions
                    (workspace_id, event_id, identity_id, role, origin, status)
                VALUES (%s, %s, %s, 'REVIEWED', 'RULE', 'CANDIDATE')
                """,
                (ws_a.workspace_id, ws_a.work_event_id, target_parent_id),
            ),
            "contribution_support->contributions": (
                """
                INSERT INTO public.contribution_support
                    (workspace_id, contribution_id, fact_id)
                VALUES (%s, %s, %s)
                """,
                (ws_a.workspace_id, target_parent_id, ws_a.fact_id),
            ),
            "contribution_support->facts": (
                """
                INSERT INTO public.contribution_support
                    (workspace_id, contribution_id, fact_id)
                VALUES (%s, %s, %s)
                """,
                (ws_a.workspace_id, ws_a.contribution_id, target_parent_id),
            ),
            "claims->work_events": (
                """
                INSERT INTO public.claims
                    (workspace_id, event_id, claim_text)
                VALUES (%s, %s, 'Cross-ws claim')
                """,
                (ws_a.workspace_id, target_parent_id),
            ),
            "claim_atoms->claims": (
                """
                INSERT INTO public.claim_atoms
                    (workspace_id, claim_id, ordinal, atom_type, atom_text)
                VALUES (%s, %s, 5, 'ACTION', 'Cross-ws atom')
                """,
                (ws_a.workspace_id, target_parent_id),
            ),
            "claim_atom_support->claim_atoms": (
                """
                INSERT INTO public.claim_atom_support
                    (workspace_id, claim_atom_id, fact_id)
                VALUES (%s, %s, %s)
                """,
                (ws_a.workspace_id, target_parent_id, ws_a.fact_id),
            ),
            "claim_atom_support->facts": (
                """
                INSERT INTO public.claim_atom_support
                    (workspace_id, claim_atom_id, fact_id)
                VALUES (%s, %s, %s)
                """,
                (ws_a.workspace_id, ws_a.claim_atom_id, target_parent_id),
            ),
        }
        return cases[case_name]

    parent_id_by_case: list[tuple[str, uuid.UUID]] = [
        ("source_resources->source_accounts", ws_b.account_id),
        ("source_objects->source_resources", ws_b.resource_id),
        ("source_object_revisions->source_objects", ws_b.source_object_id),
        ("content_units->source_object_revisions", ws_b.revision_id),
        ("external_identity_mappings->identities", ws_b.identity_id),
        ("work_events->source_objects", ws_b.source_object_id),
        ("event_memberships->work_events", ws_b.work_event_id),
        ("event_memberships->source_objects", ws_b.source_object_id),
        ("event_composite_memberships->composite", ws_b.work_event_id),
        ("event_composite_memberships->member", ws_b.work_event_id),
        ("facts->work_events", ws_b.work_event_id),
        ("fact_support->facts", ws_b.fact_id),
        ("fact_support->content_units", ws_b.content_unit_id),
        ("contributions->work_events", ws_b.work_event_id),
        ("contributions->identities", ws_b.identity_id),
        ("contribution_support->contributions", ws_b.contribution_id),
        ("contribution_support->facts", ws_b.fact_id),
        ("claims->work_events", ws_b.work_event_id),
        ("claim_atoms->claims", ws_b.claim_id),
        ("claim_atom_support->claim_atoms", ws_b.claim_atom_id),
        ("claim_atom_support->facts", ws_b.fact_id),
    ]

    # Verify for BOTH RLS-bypassing admin/backfill role AND restricted runtime role (cm_api_app).
    for role_name in (cluster.superuser, API_APP_ROLE):
        with psycopg.connect(cluster.dsn(dbname=dbname, user=role_name)) as conn:
            for case_name, other_ws_parent_id in parent_id_by_case:
                set_workspace_rls_context(conn, ws_a.workspace_id)
                sql_cross, params_cross = cross_ws_cases(other_ws_parent_id, case_name)
                with pytest.raises(pg_errors.ForeignKeyViolation) as cross_exc:
                    with conn.cursor() as cur:
                        cur.execute(sql_cross, params_cross)
                conn.rollback()

                set_workspace_rls_context(conn, ws_a.workspace_id)
                sql_missing, params_missing = cross_ws_cases(nonexistent, case_name)
                with pytest.raises(pg_errors.ForeignKeyViolation) as missing_exc:
                    with conn.cursor() as cur:
                        cur.execute(sql_missing, params_missing)
                conn.rollback()

                assert cross_exc.value.sqlstate == "23503"
                assert missing_exc.value.sqlstate == "23503"
                assert cross_exc.value.diag.constraint_name == missing_exc.value.diag.constraint_name

    # Also verify that even table owner cm_migrator (NOBYPASSRLS) is subject to FORCE RLS
    # and cannot read or insert tenant rows when no policy grants it runtime DML.
    with psycopg.connect(cluster.dsn(dbname=dbname, user=MIGRATOR_ROLE)) as owner_conn:
        with owner_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM public.source_resources")
            assert cur.fetchone() == (0,)
        with pytest.raises(pg_errors.InsufficientPrivilege):
            sql_cross, params_cross = cross_ws_cases(
                ws_b.account_id, "source_resources->source_accounts"
            )
            with owner_conn.cursor() as cur:
                cur.execute(sql_cross, params_cross)
        owner_conn.rollback()


def test_rls_fail_closed_connection_reuse_and_least_privilege_grants(
    migrated_db: tuple[EphemeralPgCluster, str],
) -> None:
    cluster, dbname = migrated_db
    with psycopg.connect(cluster.dsn(dbname=dbname)) as setup_conn:
        ws_a = _seed_workspace_graph(setup_conn, slug_suffix="rls-a")
        ws_b = _seed_workspace_graph(setup_conn, slug_suffix="rls-b")

    # 1. Both cm_api_app and cm_worker_app fail closed (0 rows) when GUC is unset/empty/malformed,
    #    and isolate cleanly across transactions on the SAME reused connection.
    for role_name in (API_APP_ROLE, WORKER_APP_ROLE):
        with psycopg.connect(cluster.dsn(dbname=dbname, user=role_name), autocommit=False) as conn:
            # Unset GUC -> 0 rows everywhere.
            with conn.cursor() as cur:
                for tbl in ("source_objects", "content_units", "work_events", "facts", "claims"):
                    cur.execute(f"SELECT COUNT(*) FROM public.{tbl}")
                    assert cur.fetchone() == (0,)
                cur.execute("SELECT COUNT(*) FROM public.v_claim_verification_summary")
                assert cur.fetchone() == (0,)
            conn.rollback()

            # Malformed GUC -> 0 rows (no cast error).
            with conn.cursor() as cur:
                cur.execute("SELECT set_config('app.current_workspace_id', 'not-a-uuid', true)")
                cur.execute("SELECT COUNT(*) FROM public.claims")
                assert cur.fetchone() == (0,)
            conn.rollback()

            # Tx 1 scoped to ws_a -> sees ONLY ws_a.
            set_workspace_rls_context(conn, ws_a.workspace_id)
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM public.claims")
                assert cur.fetchall() == [(ws_a.claim_id,)]
            conn.commit()

            # Tx 2 immediately after commit without setting GUC -> back to 0 rows (no leak!).
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.claims")
                assert cur.fetchone() == (0,)
            conn.rollback()

            # Tx 3 scoped to ws_b -> sees ONLY ws_b.
            set_workspace_rls_context(conn, ws_b.workspace_id)
            with conn.cursor() as cur:
                cur.execute("SELECT id FROM public.claims")
                assert cur.fetchall() == [(ws_b.claim_id,)]
            clear_workspace_rls_context(conn)
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM public.claims")
                assert cur.fetchone() == (0,)
            conn.commit()

    # 2. cm_worker_app MUST be denied users, workspaces, workspace_memberships, DELETE, and review_actions INSERT.
    with psycopg.connect(cluster.dsn(dbname=dbname, user=WORKER_APP_ROLE), autocommit=False) as worker_conn:
        for forbidden_table in ("users", "workspaces", "workspace_memberships"):
            with pytest.raises(pg_errors.InsufficientPrivilege):
                with worker_conn.cursor() as cur:
                    cur.execute(f"SELECT COUNT(*) FROM public.{forbidden_table}")
            worker_conn.rollback()

        set_workspace_rls_context(worker_conn, ws_a.workspace_id)
        with pytest.raises(pg_errors.InsufficientPrivilege):
            with worker_conn.cursor() as cur:
                cur.execute("DELETE FROM public.claims WHERE id = %s", (ws_a.claim_id,))
        worker_conn.rollback()

        set_workspace_rls_context(worker_conn, ws_a.workspace_id)
        with pytest.raises(pg_errors.InsufficientPrivilege):
            with worker_conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.review_actions
                        (workspace_id, action, target_type, target_id, idempotency_key)
                    VALUES (%s, 'APPROVE', 'CLAIM', %s, 'worker-attempt')
                    """,
                    (ws_a.workspace_id, ws_a.claim_id),
                )
        worker_conn.rollback()


def test_domain_constraints_identity_events_review_actions_and_contributions(
    migrated_db: tuple[EphemeralPgCluster, str],
) -> None:
    cluster, dbname = migrated_db
    with psycopg.connect(cluster.dsn(dbname=dbname, user=API_APP_ROLE), autocommit=False) as conn:
        with psycopg.connect(cluster.dsn(dbname=dbname)) as setup_conn:
            g = _seed_workspace_graph(setup_conn, slug_suffix="domain")

        # 1. external_identity_mappings: FUZZY_HEURISTIC cannot be VERIFIED.
        set_workspace_rls_context(conn, g.workspace_id)
        with pytest.raises(pg_errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.external_identity_mappings
                        (workspace_id, identity_id, provider, external_login,
                         verification_method, status)
                    VALUES (%s, %s, 'GITHUB', 'alex-guess', 'FUZZY_HEURISTIC', 'VERIFIED')
                    """,
                    (g.workspace_id, g.identity_id),
                )
        conn.rollback()

        set_workspace_rls_context(conn, g.workspace_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.external_identity_mappings
                    (workspace_id, identity_id, provider, external_user_id,
                     verification_method, status, verified_at)
                VALUES (%s, %s, 'GITHUB', '12345', 'OAUTH_ACCOUNT_ID', 'VERIFIED', now())
                """,
                (g.workspace_id, g.identity_id),
            )
        conn.commit()

        # 2. work_events anchor invariants.
        for bad_kind, bad_anchor, bad_scope in [
            ("PR", None, None),
            ("DIRECT_COMMIT", None, None),
            ("PR_SEGMENT", g.source_object_id, None),
            ("PR_SEGMENT", None, {"segment_index": 0}),
        ]:
            set_workspace_rls_context(conn, g.workspace_id)
            with pytest.raises(pg_errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.work_events
                            (workspace_id, event_kind, title, primary_anchor_source_object_id, anchor_scope)
                        VALUES (%s, %s, 'Invalid anchor', %s, %s)
                        """,
                        (
                            g.workspace_id,
                            bad_kind,
                            bad_anchor,
                            Jsonb(bad_scope) if bad_scope is not None else None,
                        ),
                    )
            conn.rollback()

        # 3. event_composite_memberships: no self-reference & no duplicate active member.
        set_workspace_rls_context(conn, g.workspace_id)
        comp_a = uuid.uuid4()
        comp_b = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO public.work_events (id, workspace_id, event_kind, title) VALUES (%s, %s, 'COMPOSITE', 'Comp A')",
                (comp_a, g.workspace_id),
            )
            cur.execute(
                "INSERT INTO public.work_events (id, workspace_id, event_kind, title) VALUES (%s, %s, 'COMPOSITE', 'Comp B')",
                (comp_b, g.workspace_id),
            )
        conn.commit()

        set_workspace_rls_context(conn, g.workspace_id)
        with pytest.raises(pg_errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.event_composite_memberships
                        (workspace_id, composite_event_id, member_event_id)
                    VALUES (%s, %s, %s)
                    """,
                    (g.workspace_id, comp_a, comp_a),
                )
        conn.rollback()

        set_workspace_rls_context(conn, g.workspace_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.event_composite_memberships
                    (workspace_id, composite_event_id, member_event_id, status)
                VALUES (%s, %s, %s, 'ACTIVE')
                """,
                (g.workspace_id, comp_a, g.work_event_id),
            )
        conn.commit()

        set_workspace_rls_context(conn, g.workspace_id)
        with pytest.raises(pg_errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.event_composite_memberships
                        (workspace_id, composite_event_id, member_event_id, status)
                    VALUES (%s, %s, %s, 'ACTIVE')
                    """,
                    (g.workspace_id, comp_b, g.work_event_id),
                )
        conn.rollback()

        # 4. review_actions idempotency uniqueness.
        set_workspace_rls_context(conn, g.workspace_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.review_actions
                    (workspace_id, action, target_type, target_id, idempotency_key)
                VALUES (%s, 'CONFIRM', 'WORK_EVENT', %s, 'idem-001')
                """,
                (g.workspace_id, g.work_event_id),
            )
        conn.commit()

        set_workspace_rls_context(conn, g.workspace_id)
        with pytest.raises(pg_errors.UniqueViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO public.review_actions
                        (workspace_id, action, target_type, target_id, idempotency_key)
                    VALUES (%s, 'CONFIRM', 'WORK_EVENT', %s, 'idem-001')
                    """,
                    (g.workspace_id, g.work_event_id),
                )
        conn.rollback()

        # 5. contributions: MVP RULE origin restricted to IMPLEMENTED / REVIEWED only.
        for disallowed_role in ("PROPOSED", "DESIGNED", "OPERATED", "MENTORED", "MEASURED"):
            set_workspace_rls_context(conn, g.workspace_id)
            with pytest.raises(pg_errors.CheckViolation):
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO public.contributions
                            (workspace_id, event_id, identity_id, role, origin, status)
                        VALUES (%s, %s, %s, %s, 'RULE', 'CANDIDATE')
                        """,
                        (g.workspace_id, g.work_event_id, g.identity_id, disallowed_role),
                    )
            conn.rollback()

        # USER origin may record DESIGNED and RULE origin may record REVIEWED.
        set_workspace_rls_context(conn, g.workspace_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.contributions
                    (workspace_id, event_id, identity_id, role, origin, status)
                VALUES
                    (%s, %s, %s, 'DESIGNED', 'USER', 'USER_CONFIRMED'),
                    (%s, %s, %s, 'REVIEWED', 'RULE', 'CANDIDATE')
                """,
                (
                    g.workspace_id,
                    g.work_event_id,
                    g.identity_id,
                    g.workspace_id,
                    g.work_event_id,
                    g.identity_id,
                ),
            )
        conn.commit()


def test_full_provenance_chain_and_computed_claim_verification_states(
    migrated_db: tuple[EphemeralPgCluster, str],
) -> None:
    cluster, dbname = migrated_db
    with psycopg.connect(cluster.dsn(dbname=dbname)) as setup_conn:
        ws_a = _seed_workspace_graph(setup_conn, slug_suffix="prov-a")
        ws_b = _seed_workspace_graph(setup_conn, slug_suffix="prov-b")

    with psycopg.connect(cluster.dsn(dbname=dbname, user=API_APP_ROLE), autocommit=False) as conn:
        # 1. Full provenance chain join under ws_a RLS context.
        set_workspace_rls_context(conn, ws_a.workspace_id)
        chain = fetch_claim_provenance_chain(
            conn, workspace_id=ws_a.workspace_id, claim_id=ws_a.claim_id
        )
        assert len(chain) == 1
        row = chain[0]
        assert row.claim_id == ws_a.claim_id
        assert row.claim_atom_id == ws_a.claim_atom_id
        assert row.fact_id == ws_a.fact_id
        assert row.content_unit_id == ws_a.content_unit_id
        assert row.source_object_revision_id == ws_a.revision_id
        assert row.revision_content_hash == _HASH_A
        assert row.content_unit_hash == _HASH_B
        assert row.content_unit_anchor["parser_version"] == "md-parser-v1"

        # 2. Initial claim has 1 SUPPORTED atom with active DIRECT provenance -> SOURCE_VERIFIED.
        summary = fetch_claim_verification_summary(
            conn, workspace_id=ws_a.workspace_id, claim_id=ws_a.claim_id
        )
        assert summary is not None
        assert summary.computed_verification == ClaimVerification.SOURCE_VERIFIED
        assert summary.atom_count == 1
        assert summary.source_backed_atom_count == 1
        assert summary.user_confirmed_atom_count == 0

        # Cross-workspace read via security_invoker view returns empty / None.
        assert (
            fetch_claim_provenance_chain(
                conn, workspace_id=ws_b.workspace_id, claim_id=ws_b.claim_id
            )
            == []
        )
        assert (
            fetch_claim_verification_summary(
                conn, workspace_id=ws_b.workspace_id, claim_id=ws_b.claim_id
            )
            is None
        )

        # 3. Adding a second USER_CONFIRMED atom transitions computed_verification to MIXED.
        user_atom_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.claim_atoms
                    (id, workspace_id, claim_id, ordinal, atom_type, atom_text, status)
                VALUES (%s, %s, %s, 1, 'RESULT', 'Reduced peak timeout alerts', 'USER_CONFIRMED')
                """,
                (user_atom_id, ws_a.workspace_id, ws_a.claim_id),
            )
        mixed_summary = fetch_claim_verification_summary(
            conn, workspace_id=ws_a.workspace_id, claim_id=ws_a.claim_id
        )
        assert mixed_summary is not None
        assert mixed_summary.computed_verification == ClaimVerification.MIXED

        # 4. Adding an active CONTRADICTS fact_support drops claim to UNVERIFIED.
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.fact_support
                    (workspace_id, fact_id, content_unit_id, support_type, validator, validator_version)
                VALUES (%s, %s, %s, 'CONTRADICTS', 'MODEL', 'val-v2')
                """,
                (ws_a.workspace_id, ws_a.fact_id, ws_a.content_unit_id),
            )
        contra_summary = fetch_claim_verification_summary(
            conn, workspace_id=ws_a.workspace_id, claim_id=ws_a.claim_id
        )
        assert contra_summary is not None
        assert contra_summary.computed_verification == ClaimVerification.UNVERIFIED

        # 5. Claim with ONLY USER_CONFIRMED atom computes to USER_CONFIRMED (never SOURCE_VERIFIED).
        user_only_claim_id = uuid.uuid4()
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.claims
                    (id, workspace_id, event_id, claim_text, status)
                VALUES (%s, %s, %s, 'User attested contribution', 'DRAFT')
                """,
                (user_only_claim_id, ws_a.workspace_id, ws_a.work_event_id),
            )
            cur.execute(
                """
                INSERT INTO public.claim_atoms
                    (workspace_id, claim_id, ordinal, atom_type, atom_text, status)
                VALUES (%s, %s, 0, 'ROLE', 'Coordinated rollout', 'USER_CONFIRMED')
                """,
                (ws_a.workspace_id, user_only_claim_id),
            )
        user_only_summary = fetch_claim_verification_summary(
            conn, workspace_id=ws_a.workspace_id, claim_id=user_only_claim_id
        )
        assert user_only_summary is not None
        assert user_only_summary.computed_verification == ClaimVerification.USER_CONFIRMED

        # 6. Direct write to computed view is impossible.
        with pytest.raises((pg_errors.InsufficientPrivilege, pg_errors.ObjectNotInPrerequisiteState)):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE public.v_claim_verification_summary
                    SET computed_verification = 'SOURCE_VERIFIED'
                    WHERE claim_id = %s
                    """,
                    (user_only_claim_id,),
                )
        conn.rollback()
