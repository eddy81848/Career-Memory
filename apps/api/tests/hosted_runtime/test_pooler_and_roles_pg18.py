"""PG18 tests for Supavisor :6543 pooler compatibility, role guards, and migration_runner (OD-006)."""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import psycopg
import pytest

from career_memory_api.auth.postgres_guard import bind_postgres_workspace_context
from career_memory_api.authz import (
    AuthenticatedUser,
    AuthSubject,
    WorkspaceAccessContext,
    WorkspaceMembership,
)
from career_memory_api.conformance.checks import default_repo_root
from career_memory_api.conformance.db_catalog import (
    EphemeralConformancePg18Cluster,
    diff_db_catalog_snapshots,
    introspect_postgres_catalog,
)
from career_memory_api.hosted_runtime.migration_runner import (
    assert_safe_migration_dsn,
    enroll_pilot_user,
    run_staging_migrations,
)
from career_memory_api.hosted_runtime.models import (
    HostedConfigurationError,
    HostedDbPoolConfig,
    HostedRoleSecurityError,
)
from career_memory_api.hosted_runtime.pooler import (
    HostedTransactionPool,
    scoped_pool_connection,
)

REPO_ROOT = default_repo_root()
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"
EXPECTED_CATALOG_PATH = REPO_ROOT / "contracts" / "conformance" / "expected_db_catalog_snapshot.json"


def _seed_workspace_and_account(
    admin_dsn: str,
    *,
    issuer: str,
    subject: str,
    workspace_name: str,
    external_account_id: str,
) -> WorkspaceAccessContext:
    user_id = str(uuid.uuid4())
    ws_id = str(uuid.uuid4())
    acct_id = str(uuid.uuid4())
    with psycopg.connect(admin_dsn, autocommit=False) as conn:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO public.users (id, auth_issuer, auth_subject, email, name) "
                    "VALUES (%s::uuid, %s, %s, %s, %s)",
                    (user_id, issuer, subject, f"{subject}@example.test", subject),
                )
                cur.execute(
                    "INSERT INTO public.workspaces (id, owner_user_id, name) "
                    "VALUES (%s::uuid, %s::uuid, %s)",
                    (ws_id, user_id, workspace_name),
                )
                cur.execute(
                    "INSERT INTO public.workspace_memberships (workspace_id, user_id, role) "
                    "VALUES (%s::uuid, %s::uuid, 'OWNER')",
                    (ws_id, user_id),
                )
                cur.execute(
                    """
                    INSERT INTO public.source_accounts (
                        id, workspace_id, provider, external_account_id,
                        token_secret_ref, scopes, status
                    ) VALUES (
                        %s::uuid, %s::uuid, 'GITHUB', %s,
                        %s, ARRAY['read:user']::text[], 'ACTIVE'
                    )
                    """,
                    (acct_id, ws_id, external_account_id, f"secret://github/{external_account_id}"),
                )
    return WorkspaceAccessContext(
        user=AuthenticatedUser(
            user_id=user_id,
            auth_subject=AuthSubject(issuer=issuer, subject=subject),
        ),
        membership=WorkspaceMembership(workspace_id=ws_id, user_id=user_id, role="OWNER"),
    )


def test_pool_config_refuses_privileged_roles_and_enforces_caps_and_tls() -> None:
    for bad_role in (
        "postgres",
        "postgres.projref123",
        "supabase_admin",
        "supabase_auth_admin",
        "service_role",
        "cm_migrator",
        "cm_migrator.projref123",
        "anon",
        "authenticated",
        "authenticator",
    ):
        with pytest.raises(HostedRoleSecurityError):
            HostedDbPoolConfig(
                database_url=f"postgresql://{bad_role}:pw@db.example.supabase.co:6543/postgres?sslmode=verify-full",
            )

    # max_size cap > 4 must fail
    with pytest.raises(HostedConfigurationError):
        HostedDbPoolConfig(
            database_url="postgresql://cm_api_app.ref:pw@db.example.supabase.co:6543/postgres?sslmode=verify-full",
            max_size=5,
        )

    # Missing TLS sslmode must fail when require_tls=True
    with pytest.raises(HostedConfigurationError):
        HostedDbPoolConfig(
            database_url="postgresql://cm_api_app.ref:pw@db.example.supabase.co:6543/postgres",
            require_tls=True,
        )

    # Startup options on :6543 must fail
    with pytest.raises(HostedConfigurationError):
        HostedDbPoolConfig(
            database_url="postgresql://cm_api_app.ref:pw@db.example.supabase.co:6543/postgres?sslmode=require&options=-c%20statement_timeout=5000",
            require_tls=True,
        )

    # Migration runner refuses :6543 and runtime roles
    with pytest.raises(HostedConfigurationError):
        assert_safe_migration_dsn(
            "postgresql://cm_migrator:pw@pooler.supabase.com:6543/postgres?sslmode=verify-full"
        )
    with pytest.raises(HostedRoleSecurityError):
        assert_safe_migration_dsn(
            "postgresql://cm_api_app:pw@pooler.supabase.com:5432/postgres?sslmode=verify-full"
        )


@pytest.mark.postgres
def test_pooler_zero_prepared_statements_cross_workspace_rollback_and_role_guard_pg18() -> None:
    with EphemeralConformancePg18Cluster(prefix="cm-hosted-pool-") as cluster:
        dbname = "cm_hosted_pool_test"
        cluster.create_database(dbname)
        admin_dsn = cluster.dsn(dbname)

        # Simulate Supabase anon/authenticated roles existing before migration!
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE ROLE anon NOLOGIN;
                    CREATE ROLE authenticated NOLOGIN;
                    """
                )

        # 1. Run migration_runner (first run applies all 8 migrations + revokes anon/authenticated)
        first_report = run_staging_migrations(
            admin_dsn,
            migrations_dir=MIGRATIONS_DIR,
            allow_insecure_local_test=True,
        )
        assert len(first_report.applied_migrations) == 8
        assert first_report.skipped_already_applied == ()
        assert set(first_report.revoked_supabase_data_api_roles) == {"anon", "authenticated"}
        assert first_report.audit.passed is True
        assert first_report.audit.public_table_count == 46
        assert first_report.audit.supabase_data_api_leaks == ()

        # 2. Idempotent re-apply skips all 8 migrations & preserves 0 catalog drift!
        second_report = run_staging_migrations(
            admin_dsn,
            migrations_dir=MIGRATIONS_DIR,
            allow_insecure_local_test=True,
        )
        assert second_report.applied_migrations == ()
        assert len(second_report.skipped_already_applied) == 8
        assert second_report.audit.passed is True

        expected_catalog = json.loads(EXPECTED_CATALOG_PATH.read_text(encoding="utf-8"))
        with psycopg.connect(admin_dsn) as conn:
            actual_catalog = introspect_postgres_catalog(conn)
        drift = diff_db_catalog_snapshots(expected_catalog, actual_catalog)
        assert drift == [], f"Unexpected DB catalog drift after migration_runner: {drift}"

        # 3. Verify pilot user enrollment helper (idempotent)
        enrolled_1 = enroll_pilot_user(
            admin_dsn,
            auth_issuer="https://pilot.supabase.test/auth/v1",
            auth_subject="sub-pilot-owner-001",
            workspace_name="Pilot Owner Workspace",
            email="owner@pilot.test",
            display_name="Pilot Owner",
            github_login="pilot-owner",
            github_external_id="83367652",
            public_repositories=[("1463618273", "eddy81848/Career-Memory")],
            allow_insecure_local_test=True,
        )
        assert enrolled_1.user_created is True
        assert enrolled_1.workspace_created is True
        assert enrolled_1.source_account_id is not None
        assert len(enrolled_1.source_resource_ids) == 1
        enrolled_2 = enroll_pilot_user(
            admin_dsn,
            auth_issuer="https://pilot.supabase.test/auth/v1",
            auth_subject="sub-pilot-owner-001",
            workspace_name="Pilot Owner Workspace",
            github_login="pilot-owner",
            github_external_id="83367652",
            public_repositories=[("1463618273", "eddy81848/Career-Memory")],
            allow_insecure_local_test=True,
        )
        assert enrolled_2.user_id == enrolled_1.user_id
        assert enrolled_2.workspace_id == enrolled_1.workspace_id
        assert enrolled_2.source_account_id == enrolled_1.source_account_id
        assert enrolled_2.source_resource_ids == enrolled_1.source_resource_ids
        assert enrolled_2.user_created is False
        assert enrolled_2.workspace_created is False

        # 4. Create least-privilege api login + an illicit bypassrls login to verify live guard
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    CREATE ROLE cm_api_login_test LOGIN PASSWORD 'api_pw'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT NOBYPASSRLS;
                    GRANT cm_api_app TO cm_api_login_test;

                    CREATE ROLE cm_bad_bypass_login LOGIN PASSWORD 'bad_pw'
                        NOSUPERUSER NOCREATEDB NOCREATEROLE INHERIT BYPASSRLS;
                    GRANT cm_api_app TO cm_bad_bypass_login;
                    """
                )

        # Live pg_roles check MUST reject BYPASSRLS role on first checkout!
        bad_pool = HostedTransactionPool(
            HostedDbPoolConfig(
                database_url=f"postgresql://cm_bad_bypass_login:bad_pw@127.0.0.1:{cluster.port}/{dbname}",
                min_size=0,
                max_size=1,
                require_tls=False,
                allow_insecure_local_test=True,
                expected_role_group="cm_api_app",
            )
        )
        try:
            with pytest.raises(HostedRoleSecurityError, match="rolbypassrls=true"):
                with bad_pool.connection():
                    pass
        finally:
            bad_pool.close()

        # 5. Seed two distinct workspaces WS_A and WS_B
        ws_a = _seed_workspace_and_account(
            admin_dsn,
            issuer="https://pilot.supabase.test/auth/v1",
            subject="sub-ws-a",
            workspace_name="Workspace A",
            external_account_id="gh-acct-a",
        )
        ws_b = _seed_workspace_and_account(
            admin_dsn,
            issuer="https://pilot.supabase.test/auth/v1",
            subject="sub-ws-b",
            workspace_name="Workspace B",
            external_account_id="gh-acct-b",
        )

        # 6. Single physical connection (max_size=1) with prepare_threshold=None:
        #    - 15 repeated parameterized queries => 0 pg_prepared_statements!
        #    - WS_A -> forced ROLLBACK -> unscoped 0 rows -> WS_B -> unscoped 0 rows on same backend PID!
        api_pool = HostedTransactionPool(
            HostedDbPoolConfig(
                database_url=f"postgresql://cm_api_login_test:api_pw@127.0.0.1:{cluster.port}/{dbname}",
                min_size=1,
                max_size=1,
                require_tls=False,
                allow_insecure_local_test=True,
                expected_role_group="cm_api_app",
            )
        )
        try:
            observed_pids: set[int] = set()

            # Repeated parameterized queries must create ZERO server prepared statements
            for _ in range(15):
                with scoped_pool_connection(api_pool, access=ws_a, read_only=True) as cur:
                    cur.execute("SELECT pg_backend_pid()")
                    observed_pids.add(int(cur.fetchone()[0]))
                    cur.execute(
                        "SELECT external_account_id FROM public.source_accounts WHERE workspace_id = %s::uuid",
                        (ws_a.workspace_id,),
                    )
                    assert [r[0] for r in cur.fetchall()] == ["gh-acct-a"]

            with api_pool.connection() as conn:
                with conn.transaction():
                    with conn.cursor() as cur:
                        cur.execute("SELECT count(*) FROM pg_prepared_statements")
                        prepared_count = int(cur.fetchone()[0])
            assert prepared_count == 0, f"Expected 0 prepared statements, found {prepared_count}"

            # WS_A -> forced ROLLBACK
            with pytest.raises(RuntimeError, match="forced_rollback"):
                with api_pool.connection() as conn:
                    with conn.transaction():
                        with conn.cursor() as cur:
                            bind_postgres_workspace_context(cur, ws_a)
                            cur.execute("SELECT pg_backend_pid()")
                            observed_pids.add(int(cur.fetchone()[0]))
                            cur.execute("SELECT external_account_id FROM public.source_accounts")
                            assert [r[0] for r in cur.fetchall()] == ["gh-acct-a"]
                            raise RuntimeError("forced_rollback")

            # Unscoped query immediately after rollback -> 0 rows (fail-closed)
            with scoped_pool_connection(api_pool, read_only=True) as cur:
                cur.execute("SELECT pg_backend_pid()")
                observed_pids.add(int(cur.fetchone()[0]))
                cur.execute("SELECT external_account_id FROM public.source_accounts")
                assert cur.fetchall() == []

            # WS_B sees ONLY WS_B
            with scoped_pool_connection(api_pool, access=ws_b, read_only=True) as cur:
                cur.execute("SELECT pg_backend_pid()")
                observed_pids.add(int(cur.fetchone()[0]))
                cur.execute("SELECT external_account_id FROM public.source_accounts")
                assert [r[0] for r in cur.fetchall()] == ["gh-acct-b"]

            # Unscoped query after WS_B -> 0 rows
            with scoped_pool_connection(api_pool, read_only=True) as cur:
                cur.execute("SELECT pg_backend_pid()")
                observed_pids.add(int(cur.fetchone()[0]))
                cur.execute("SELECT external_account_id FROM public.source_accounts")
                assert cur.fetchall() == []

            # All interleaved queries ran on the exact same physical PG18 backend PID!
            assert len(observed_pids) == 1
        finally:
            api_pool.close()
