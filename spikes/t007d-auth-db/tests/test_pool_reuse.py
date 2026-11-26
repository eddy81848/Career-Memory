"""Local physical DB connection reuse tests across Workspace A and Workspace B.

NOTE: These tests prove zero context carryover on a single reused local PostgreSQL 18
backend process (`pg_backend_pid()` identical across requests via `psycopg_pool(max_size=1)`).
They are explicitly NOT a substitute for testing Supabase hosted Supavisor pooler modes.
"""

from __future__ import annotations

import pytest

from src.db import (
    get_backend_pid,
    scoped_api_workspace_tx,
    scoped_worker_workspace_tx,
    unsafe_set_session_workspace_id,
    unscoped_tx,
)
from tests.conftest import (
    ARTIFACT_A1,
    ARTIFACT_B1,
    PROJECT_A1,
    PROJECT_B1,
    USER_ALICE_A,
    USER_BOB_B,
    USER_DANA_DUAL,
    WS_A,
    WS_B,
)


def test_single_physical_connection_reuse_api_role_zero_leakage(api_pool) -> None:
    """Reuse the exact same physical PG backend PID across WS_A -> unscoped -> WS_B -> rollback -> unscoped."""
    observed_pids: list[int] = []

    # 1. Request in Workspace A
    with scoped_api_workspace_tx(api_pool, subject_id=USER_ALICE_A, requested_workspace_id=WS_A) as (
        conn,
        _m,
        pid,
    ):
        observed_pids.append(pid)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_t007d.projects")
            assert [r["id"] for r in cur.fetchall()] == [PROJECT_A1]

    # 2. Immediately check bare unscoped tx on same physical connection -> must be fail-closed
    with unscoped_tx(api_pool) as (conn, pid):
        observed_pids.append(pid)
        with conn.cursor() as cur:
            cur.execute("SELECT current_setting('app.current_workspace_id', true) AS ws")
            assert cur.fetchone()["ws"] in (None, "")
            cur.execute("SELECT id FROM app_t007d.projects")
            assert cur.fetchall() == []
            cur.execute("SELECT id FROM app_t007d.artifacts")
            assert cur.fetchall() == []

    # 3. Request in Workspace B on same physical connection
    with scoped_api_workspace_tx(api_pool, subject_id=USER_BOB_B, requested_workspace_id=WS_B) as (
        conn,
        _m,
        pid,
    ):
        observed_pids.append(pid)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_t007d.projects")
            assert [r["id"] for r in cur.fetchall()] == [PROJECT_B1]
            cur.execute("SELECT id FROM app_t007d.artifacts")
            assert [r["id"] for r in cur.fetchall()] == [ARTIFACT_B1]

    # 4. Aborted/rolled-back transaction in Workspace A must NOT leave GUC behind
    with pytest.raises(RuntimeError, match="simulated failure"):
        with scoped_api_workspace_tx(
            api_pool, subject_id=USER_ALICE_A, requested_workspace_id=WS_A
        ) as (_conn, _m, pid):
            observed_pids.append(pid)
            raise RuntimeError("simulated failure")

    # 5. Unscoped tx right after rollback must still see 0 rows
    with unscoped_tx(api_pool) as (conn, pid):
        observed_pids.append(pid)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_t007d.projects")
            assert cur.fetchall() == []

    # Hard proof: every single step ran on the EXACT same physical PostgreSQL backend process.
    assert len(set(observed_pids)) == 1, f"Expected 1 reused backend PID, got {observed_pids}"


def test_single_physical_connection_reuse_worker_role_zero_leakage(worker_pool) -> None:
    """Reuse the exact same physical Worker PG connection across WS_A -> WS_B -> unscoped."""
    observed_pids: list[int] = []

    with scoped_worker_workspace_tx(worker_pool, workspace_id=WS_A) as (conn, pid):
        observed_pids.append(pid)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_t007d.artifacts")
            assert [r["id"] for r in cur.fetchall()] == [ARTIFACT_A1]

    with scoped_worker_workspace_tx(worker_pool, workspace_id=WS_B) as (conn, pid):
        observed_pids.append(pid)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_t007d.artifacts")
            assert [r["id"] for r in cur.fetchall()] == [ARTIFACT_B1]

    with unscoped_tx(worker_pool) as (conn, pid):
        observed_pids.append(pid)
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_t007d.artifacts")
            assert cur.fetchall() == []

    assert len(set(observed_pids)) == 1, f"Expected 1 reused worker PID, got {observed_pids}"


def test_http_api_requests_reuse_same_physical_connection_across_workspaces(
    api_client,
    jwt_authority,
) -> None:
    """Verify end-to-end HTTP API requests switch workspaces cleanly on one pooled PG connection."""
    alice_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_ALICE_A)
    bob_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_BOB_B)
    dana_jwt, _, _ = jwt_authority.issue_token(subject_id=USER_DANA_DUAL)

    r_alice = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {alice_jwt}"},
    )
    r_bob = api_client.get(
        f"/api/v1/workspaces/{WS_B}/projects",
        headers={"Authorization": f"Bearer {bob_jwt}"},
    )
    r_dana_a = api_client.get(
        f"/api/v1/workspaces/{WS_A}/projects",
        headers={"Authorization": f"Bearer {dana_jwt}"},
    )
    r_dana_b = api_client.get(
        f"/api/v1/workspaces/{WS_B}/projects",
        headers={"Authorization": f"Bearer {dana_jwt}"},
    )

    assert r_alice.status_code == 200
    assert r_bob.status_code == 200
    assert r_dana_a.status_code == 200
    assert r_dana_b.status_code == 200

    assert [i["id"] for i in r_alice.json()["items"]] == [str(PROJECT_A1)]
    assert [i["id"] for i in r_bob.json()["items"]] == [str(PROJECT_B1)]
    assert [i["id"] for i in r_dana_a.json()["items"]] == [str(PROJECT_A1)]
    assert [i["id"] for i in r_dana_b.json()["items"]] == [str(PROJECT_B1)]

    pids = {
        r_alice.headers["X-T007D-Backend-PID"],
        r_bob.headers["X-T007D-Backend-PID"],
        r_dana_a.headers["X-T007D-Backend-PID"],
        r_dana_b.headers["X-T007D-Backend-PID"],
    }
    assert len(pids) == 1, f"Expected all 4 HTTP requests to reuse 1 physical PG PID, got {pids}"


def test_antipattern_session_level_set_leaks_across_pool_reuse(api_pool) -> None:
    """Negative contrast test proving WHY session-level SET (is_local=false) is unsafe on pooled connections."""
    # Caller 1 mistakenly uses session-level SET instead of SET LOCAL
    with api_pool.connection() as conn:
        pid1 = get_backend_pid(conn)
        unsafe_set_session_workspace_id(conn, WS_A)

    # Caller 2 reuses the same physical connection without setting context -> LEAKS WS_A!
    with unscoped_tx(api_pool) as (conn, pid2):
        assert pid1 == pid2
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_t007d.projects")
            leaked_rows = [r["id"] for r in cur.fetchall()]
            assert leaked_rows == [PROJECT_A1], "Session-level SET leaked across pooled connection!"
            # Clean up explicitly so pool stays hygienic
            cur.execute("RESET app.current_workspace_id")

    # After RESET, unscoped tx returns to fail-closed 0 rows
    with unscoped_tx(api_pool) as (conn, pid3):
        assert pid1 == pid3
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM app_t007d.projects")
            assert cur.fetchall() == []
