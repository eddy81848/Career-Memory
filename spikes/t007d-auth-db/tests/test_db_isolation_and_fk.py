"""DB role least-privilege, RLS/GRANT, direct/parent/child CRUD, and FK candidate comparison tests."""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest

from src.db import DbRolesConfig, scoped_api_workspace_tx, scoped_worker_workspace_tx, unscoped_tx
from tests.conftest import (
    ARTIFACT_A1,
    ARTIFACT_B1,
    PROJECT_A1,
    PROJECT_B1,
    USER_ALICE_A,
    USER_BOB_B,
    WS_A,
    WS_B,
)


def test_role_attributes_and_force_rls(superuser_conn) -> None:
    """Verify runtime roles have NOBYPASSRLS/NOSUPERUSER and do NOT own app_t007d tables."""
    with superuser_conn.cursor() as cur:
        cur.execute(
            """
            SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolinherit, rolbypassrls
              FROM pg_roles
             WHERE rolname IN ('t007d_migrator', 't007d_api_app', 't007d_worker_app')
             ORDER BY rolname
            """
        )
        roles = {row["rolname"]: row for row in cur.fetchall()}

    for name in ("t007d_api_app", "t007d_worker_app", "t007d_migrator"):
        assert roles[name]["rolsuper"] is False
        assert roles[name]["rolcreatedb"] is False
        assert roles[name]["rolcreaterole"] is False
        assert roles[name]["rolinherit"] is False
        assert roles[name]["rolbypassrls"] is False

    # Verify table ownership is strictly t007d_migrator and FORCE ROW LEVEL SECURITY is on
    with superuser_conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname,
                   pg_get_userbyid(c.relowner) AS owner_role,
                   c.relrowsecurity,
                   c.relforcerowsecurity
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'app_t007d'
               AND c.relkind = 'r'
             ORDER BY c.relname
            """
        )
        tables = {row["relname"]: row for row in cur.fetchall()}

    assert len(tables) >= 7
    for relname, meta in tables.items():
        assert meta["owner_role"] == "t007d_migrator", f"{relname} must be owned by migrator"
        if relname != "users":
            assert meta["relrowsecurity"] is True, f"{relname} must have RLS enabled"
            assert meta["relforcerowsecurity"] is True, f"{relname} must have FORCE RLS enabled"


def test_api_role_cannot_bypass_rls_or_run_ddl_or_touch_identity(api_pool) -> None:
    """Restricted API role cannot disable RLS, run DDL, read users, or write memberships."""
    with unscoped_tx(api_pool) as (conn, _pid):
        with conn.cursor() as cur:
            # Fail-closed when workspace_id GUC is unset
            cur.execute("SELECT id FROM app_t007d.projects")
            assert cur.fetchall() == []
            cur.execute("SELECT id FROM app_t007d.artifacts")
            assert cur.fetchall() == []

            # Cannot read users table
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute("SELECT * FROM app_t007d.users")

            # Cannot mutate workspace_memberships
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute(
                        """
                        UPDATE app_t007d.workspace_memberships
                           SET revoked_at = NULL
                        """
                    )

            # Cannot run DDL
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute("CREATE TABLE app_t007d.rogue (id int)")

            # Confirm runtime connection uses restricted role and cannot escalate to migrator
            cur.execute("SELECT current_user, session_user")
            who = cur.fetchone()
            assert who["current_user"] == "t007d_api_app"
            assert who["session_user"] == "t007d_api_app"

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute("SET ROLE t007d_migrator")

            # Attempting SET row_security = off cannot bypass RLS for NOBYPASSRLS role
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute("SET LOCAL row_security = off")
                    cur.execute("SELECT id FROM app_t007d.projects")


def test_direct_parent_mediated_and_child_crud_under_api_role(api_pool) -> None:
    """Verify direct parent, parent-mediated child, and direct child read/write isolation."""
    # Workspace A (Alice): normal access to A rows only
    with scoped_api_workspace_tx(api_pool, subject_id=USER_ALICE_A, requested_workspace_id=WS_A) as (
        conn,
        _m,
        _pid,
    ):
        with conn.cursor() as cur:
            # Direct parent read
            cur.execute("SELECT id FROM app_t007d.projects")
            assert [r["id"] for r in cur.fetchall()] == [PROJECT_A1]

            # Parent-mediated child read
            cur.execute(
                """
                SELECT a.id
                  FROM app_t007d.artifacts a
                  JOIN app_t007d.projects p
                    ON p.workspace_id = a.workspace_id AND p.id = a.project_id
                """
            )
            assert [r["id"] for r in cur.fetchall()] == [ARTIFACT_A1]

            # Direct child write in own workspace succeeds
            new_art_id = uuid4()
            cur.execute(
                """
                INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title)
                VALUES (%s, %s, %s, 'A-created')
                RETURNING id
                """,
                (str(new_art_id), str(WS_A), str(PROJECT_A1)),
            )
            assert cur.fetchone()["id"] == new_art_id

    # Workspace B (Bob): cannot read/update/delete A's parent or child, nor insert with WS_A
    with scoped_api_workspace_tx(api_pool, subject_id=USER_BOB_B, requested_workspace_id=WS_B) as (
        conn,
        _m,
        _pid,
    ):
        with conn.cursor() as cur:
            # Direct parent read by A's ID returns 0 rows
            cur.execute("SELECT id FROM app_t007d.projects WHERE id = %s", (str(PROJECT_A1),))
            assert cur.fetchall() == []

            # Direct child read by A's ID returns 0 rows
            cur.execute("SELECT id FROM app_t007d.artifacts WHERE id = %s", (str(ARTIFACT_A1),))
            assert cur.fetchall() == []

            # Parent-mediated read targeting A's project returns 0 rows
            cur.execute(
                """
                SELECT a.id
                  FROM app_t007d.artifacts a
                  JOIN app_t007d.projects p ON p.id = a.project_id
                 WHERE p.id = %s
                """,
                (str(PROJECT_A1),),
            )
            assert cur.fetchall() == []

            # UPDATE and DELETE targeting A's rows affect 0 rows
            cur.execute(
                "UPDATE app_t007d.artifacts SET title = 'pwned' WHERE id = %s",
                (str(ARTIFACT_A1),),
            )
            assert cur.rowcount == 0
            cur.execute("DELETE FROM app_t007d.projects WHERE id = %s", (str(PROJECT_A1),))
            assert cur.rowcount == 0

            # INSERT forging workspace_id = WS_A rejected by RLS WITH CHECK
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.projects (id, workspace_id, name, created_by)
                        VALUES (%s, %s, 'forged', %s)
                        """,
                        (str(uuid4()), str(WS_A), str(USER_BOB_B)),
                    )


def test_worker_role_minimal_grants_and_workspace_isolation(worker_pool) -> None:
    """Verify Worker candidate role has no user/membership access, no DELETE, and strict RLS."""
    with scoped_worker_workspace_tx(worker_pool, workspace_id=WS_A) as (conn, _pid):
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, session_user")
            who = cur.fetchone()
            assert who["current_user"] == "t007d_worker_app"
            assert who["session_user"] == "t007d_worker_app"

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute("SET ROLE t007d_migrator")

            # Can read projects in WS_A only
            cur.execute("SELECT id FROM app_t007d.projects")
            assert [r["id"] for r in cur.fetchall()] == [PROJECT_A1]

            # Can insert and update artifacts in WS_A
            worker_art_id = uuid4()
            cur.execute(
                """
                INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title)
                VALUES (%s, %s, %s, 'worker-ingested')
                RETURNING id
                """,
                (str(worker_art_id), str(WS_A), str(PROJECT_A1)),
            )
            assert cur.fetchone()["id"] == worker_art_id

            cur.execute(
                "UPDATE app_t007d.artifacts SET title = 'worker-updated' WHERE id = %s",
                (str(worker_art_id),),
            )
            assert cur.rowcount == 1

            # Cannot DELETE artifacts
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute("DELETE FROM app_t007d.artifacts WHERE id = %s", (str(worker_art_id),))

            # Cannot INSERT projects
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.projects (id, workspace_id, name, created_by)
                        VALUES (%s, %s, 'worker-project', %s)
                        """,
                        (str(uuid4()), str(WS_A), str(USER_ALICE_A)),
                    )

            # Cannot read users or workspace_memberships
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute("SELECT * FROM app_t007d.workspace_memberships")

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute("SELECT * FROM app_t007d.users")

            # Cannot reference WS_B parent project (Composite FK rejects)
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title)
                        VALUES (%s, %s, %s, 'cross-ws-worker')
                        """,
                        (str(uuid4()), str(WS_A), str(PROJECT_B1)),
                    )


def test_cross_workspace_fk_candidates_isolated_comparison(
    api_pool,
    pg_cluster: DbRolesConfig,
) -> None:
    """Empirically compare Baseline (Single FK) vs Candidate A (Composite FK) vs B (RLS EXISTS) vs C (Trigger)."""
    nonexistent_project_id = uuid4()

    with scoped_api_workspace_tx(api_pool, subject_id=USER_BOB_B, requested_workspace_id=WS_B) as (
        conn,
        _m,
        _pid,
    ):
        with conn.cursor() as cur:
            # 1) BASELINE (VULNERABLE): Single-column FK + workspace_id RLS ONLY
            # Because PostgreSQL RI checks execute as table owner bypassing RLS,
            # Workspace B SUCCEEDS in attaching a child row to Workspace A's parent project!
            leaked_row_id = uuid4()
            cur.execute(
                """
                INSERT INTO app_t007d.artifacts_single_fk_baseline (id, workspace_id, project_id, title)
                VALUES (%s, %s, %s, 'cross-workspace-violation-allowed')
                RETURNING id, workspace_id, project_id
                """,
                (str(leaked_row_id), str(WS_B), str(PROJECT_A1)),
            )
            leaked = cur.fetchone()
            assert leaked["workspace_id"] == WS_B
            assert leaked["project_id"] == PROJECT_A1

            # And probing a nonexistent UUID fails with FK error -> proves existence oracle!
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts_single_fk_baseline (id, workspace_id, project_id, title)
                        VALUES (%s, %s, %s, 'probe-nonexistent')
                        """,
                        (str(uuid4()), str(WS_B), str(nonexistent_project_id)),
                    )

            # 2) CANDIDATE A (RECOMMENDED): Composite FK (workspace_id, project_id) -> projects(workspace_id, id)
            # Rejects cross-workspace parent AND nonexistent parent with identical SQLSTATE 23503!
            with pytest.raises(psycopg.errors.ForeignKeyViolation) as cross_exc:
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title)
                        VALUES (%s, %s, %s, 'blocked-by-composite-fk')
                        """,
                        (str(uuid4()), str(WS_B), str(PROJECT_A1)),
                    )
            with pytest.raises(psycopg.errors.ForeignKeyViolation) as missing_exc:
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title)
                        VALUES (%s, %s, %s, 'blocked-missing')
                        """,
                        (str(uuid4()), str(WS_B), str(nonexistent_project_id)),
                    )
            assert cross_exc.value.sqlstate == missing_exc.value.sqlstate == "23503"

            # Reparenting an existing WS_B artifact to WS_A project via UPDATE is also blocked
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    cur.execute(
                        """
                        UPDATE app_t007d.artifacts
                           SET project_id = %s
                         WHERE id = %s
                        """,
                        (str(PROJECT_A1), str(ARTIFACT_B1)),
                    )

            # 3) CANDIDATE B (RLS WITH CHECK EXISTS): Blocks restricted API role with 42501
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts_rls_exists_candidate (id, workspace_id, project_id, title)
                        VALUES (%s, %s, %s, 'blocked-for-api-only')
                        """,
                        (str(uuid4()), str(WS_B), str(PROJECT_A1)),
                    )

            # 4) CANDIDATE C (BEFORE INSERT/UPDATE Trigger): Blocks restricted API role with 23503
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts_trigger_candidate (id, workspace_id, project_id, title)
                        VALUES (%s, %s, %s, 'blocked-by-trigger')
                        """,
                        (str(uuid4()), str(WS_B), str(PROJECT_A1)),
                    )

    # Now compare non-RLS / backfill / superuser path (e.g., broken migration or admin script):
    # - Candidate A (Composite FK): STILL BLOCKS!
    # - Candidate B (RLS EXISTS): FAILS TO BLOCK (allows cross-workspace corruption)!
    # - Candidate C (Trigger): STILL BLOCKS (unless trigger is disabled).
    with psycopg.connect(pg_cluster.superuser, autocommit=False) as su_conn:
        with su_conn.cursor() as cur:
            # Candidate A still blocks even superuser
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with su_conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title)
                        VALUES (%s, %s, %s, 'su-blocked-composite-fk')
                        """,
                        (str(uuid4()), str(WS_B), str(PROJECT_A1)),
                    )

            # Candidate B does NOT block superuser / non-RLS backfill
            with su_conn.transaction():
                cur.execute(
                    """
                    INSERT INTO app_t007d.artifacts_rls_exists_candidate (id, workspace_id, project_id, title)
                    VALUES (%s, %s, %s, 'su-bypassed-rls-exists')
                    """,
                    (str(uuid4()), str(WS_B), str(PROJECT_A1)),
                )

            # Candidate C still blocks superuser via trigger
            with pytest.raises(psycopg.errors.ForeignKeyViolation):
                with su_conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts_trigger_candidate (id, workspace_id, project_id, title)
                        VALUES (%s, %s, %s, 'su-blocked-trigger')
                        """,
                        (str(uuid4()), str(WS_B), str(PROJECT_A1)),
                    )
        su_conn.rollback()
