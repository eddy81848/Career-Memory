"""OD006-5 Local PostgreSQL Role-Layer Data API Bypass Denial Tests.

SCOPE BOUNDARY:
- Verifies at the real local PostgreSQL 18 role/privilege layer that Data API candidate
  roles (`t007d_data_api_anon`, `t007d_data_api_authenticated`) CANNOT directly SELECT,
  INSERT, UPDATE, or DELETE domain tables (`projects`, `artifacts`, `workspace_memberships`,
  `workspaces`, `users`), while the normal server-mediated BFF -> API -> DB path succeeds.
- Verifies live service health (`SELECT 1` + `/api/v1/health`) BEFORE asserting denials
  so a downed service can NEVER be counted as a permission-denial pass.
- Explicitly DOES NOT replace real PostgREST HTTP gateway (`/rest/v1/*`) testing, which
  lives in `test_local_supabase_integration.py` and requires a running local Docker Engine.
"""

from __future__ import annotations

from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from src.db import DbRolesConfig
from tests.conftest import (
    ARTIFACT_A1,
    PROJECT_A1,
    USER_ALICE_A,
    WS_A,
)


def _snapshot_domain_rows(superuser_conn) -> dict[str, list[tuple]]:
    with superuser_conn.cursor() as cur:
        cur.execute("SELECT id, workspace_id, name FROM app_t007d.projects ORDER BY id")
        projects = [(r["id"], r["workspace_id"], r["name"]) for r in cur.fetchall()]
        cur.execute("SELECT id, workspace_id, project_id, title FROM app_t007d.artifacts ORDER BY id")
        artifacts = [(r["id"], r["workspace_id"], r["project_id"], r["title"]) for r in cur.fetchall()]
    return {"projects": projects, "artifacts": artifacts}


@pytest.mark.parametrize("role_label,dsn_attr", [
    ("anon", "data_api_anon_dsn"),
    ("authenticated", "data_api_authenticated_dsn"),
])
def test_od006_5_db_role_layer_blocks_direct_domain_read_write_with_zero_mutation(
    pg_cluster: DbRolesConfig,
    superuser_conn,
    api_client: TestClient,
    role_label: str,
    dsn_attr: str,
) -> None:
    """Confirm live DB & API health first, then prove anon/authenticated DB roles cannot read or mutate domain tables."""
    # 1. Confirm DB and Backend API services are actively alive (never count downed service as pass)
    health = api_client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "db": "reachable"}

    before = _snapshot_domain_rows(superuser_conn)
    role_dsn = getattr(pg_cluster, dsn_attr)
    assert role_dsn, f"{dsn_attr} must be configured"

    with psycopg.connect(role_dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            # Confirm this connection itself is live and authenticated as the expected Data API role
            cur.execute("SELECT 1, current_user")
            ok_val, current_role = cur.fetchone()
            assert ok_val == 1
            assert current_role == f"t007d_data_api_{role_label}"

            # Even if caller attempts to set app.current_workspace_id manually, schema/table grants deny access
            cur.execute("SELECT set_config('app.current_workspace_id', %s, true)", (str(WS_A),))

            # Direct SELECT on domain tables rejected
            for table in ("projects", "artifacts", "workspace_memberships", "workspaces", "users"):
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn.transaction():
                        cur.execute(f"SELECT * FROM app_t007d.{table}")

            # Direct INSERT on projects & artifacts rejected
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.projects (id, workspace_id, name, created_by)
                        VALUES (%s, %s, 'bypass-insert', %s)
                        """,
                        (str(uuid4()), str(WS_A), str(USER_ALICE_A)),
                    )

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute(
                        """
                        INSERT INTO app_t007d.artifacts (id, workspace_id, project_id, title)
                        VALUES (%s, %s, %s, 'bypass-artifact')
                        """,
                        (str(uuid4()), str(WS_A), str(PROJECT_A1)),
                    )

            # Direct UPDATE & DELETE on domain tables rejected
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute(
                        "UPDATE app_t007d.projects SET name = 'bypass-updated' WHERE id = %s",
                        (str(PROJECT_A1),),
                    )

            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.transaction():
                    cur.execute(
                        "DELETE FROM app_t007d.artifacts WHERE id = %s",
                        (str(ARTIFACT_A1),),
                    )

    # 2. Verify zero DB row mutations before/after all blocked attempts
    after = _snapshot_domain_rows(superuser_conn)
    assert after == before


def test_od006_5_normal_server_mediated_bff_path_succeeds_while_data_api_roles_blocked(
    bff_app,
    superuser_conn,
) -> None:
    """Prove normal server-mediated Browser -> BFF -> API -> t007d_api_app read/write works cleanly."""
    before = _snapshot_domain_rows(superuser_conn)

    with TestClient(bff_app, base_url="http://localhost:3000") as browser:
        login = browser.post("/bff/auth/synthetic-login", json={"subject_id": str(USER_ALICE_A)})
        assert login.status_code == 200
        csrf_token = login.json()["csrf_token"]

        read_resp = browser.get(f"/bff/workspaces/{WS_A}/projects")
        assert read_resp.status_code == 200
        assert len(read_resp.json()["items"]) == 1

        create_resp = browser.post(
            f"/bff/workspaces/{WS_A}/projects",
            headers={"Origin": "http://localhost:3000", "X-CSRF-Token": csrf_token},
            json={"name": "Server-Mediated Project"},
        )
        assert create_resp.status_code == 201
        created_id = create_resp.json()["id"]

    after = _snapshot_domain_rows(superuser_conn)
    assert len(after["projects"]) == len(before["projects"]) + 1
    assert any(str(row[0]) == created_id for row in after["projects"])
