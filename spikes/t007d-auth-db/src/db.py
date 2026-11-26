"""PostgreSQL connection pooling and transaction-local tenant scoping helpers.

NOTE: Local single-connection pool reuse (`psycopg_pool.ConnectionPool(max_size=1)`)
proves PostgreSQL session/transaction GUC isolation on a reused physical backend
process (`pg_backend_pid()`). It is NOT a substitute for testing Supabase hosted
Supavisor transaction/session pooler modes, TLS, or prepared-statement limits.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


@dataclass(frozen=True)
class DbRolesConfig:
    host: str
    port: int
    dbname: str
    superuser: str
    migrator_dsn: str
    api_dsn: str
    worker_dsn: str
    data_api_anon_dsn: str = ""
    data_api_authenticated_dsn: str = ""


def make_single_connection_pool(dsn: str) -> ConnectionPool:
    """Create a 1-connection pool to deterministically verify physical backend reuse."""
    return ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=1,
        open=True,
        kwargs={"row_factory": dict_row, "autocommit": False},
    )


def get_backend_pid(conn: psycopg.Connection) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT pg_backend_pid() AS pid")
        row = cur.fetchone()
        assert row is not None
        return int(row["pid"])


@contextmanager
def scoped_api_workspace_tx(
    pool: ConnectionPool,
    *,
    subject_id: UUID,
    requested_workspace_id: UUID,
) -> Iterator[tuple[psycopg.Connection, dict, int]]:
    """Verify current DB membership as `t007d_api_app`, then bind transaction-local workspace context.

    Security invariants:
    1. Runs strictly as the restricted `t007d_api_app` role (never migrator/superuser).
    2. Sets `app.current_subject_id` with `is_local = true` (SET LOCAL semantics).
    3. Looks up active (`revoked_at IS NULL`) membership in `app_t007d.workspace_memberships`.
       Never grants access from client-supplied `workspace_id` alone.
    4. Only AFTER active membership is verified, binds `app.current_workspace_id` with
       `is_local = true` to the verified membership's `workspace_id`.
    5. When the transaction commits or rolls back, both transaction-local GUCs revert
       automatically before the physical connection returns to the pool.
    """
    with pool.connection() as conn:
        pid = get_backend_pid(conn)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.current_subject_id', %s, true)",
                    (str(subject_id),),
                )
                cur.execute(
                    """
                    SELECT workspace_id, subject_id, member_role
                      FROM app_t007d.workspace_memberships
                     WHERE workspace_id = %s
                       AND subject_id = %s
                       AND revoked_at IS NULL
                    """,
                    (str(requested_workspace_id), str(subject_id)),
                )
                membership = cur.fetchone()
                if membership is None:
                    raise PermissionError("No active workspace membership for subject")

                verified_ws_id = str(membership["workspace_id"])
                cur.execute(
                    "SELECT set_config('app.current_workspace_id', %s, true)",
                    (verified_ws_id,),
                )
            yield conn, dict(membership), pid


@contextmanager
def scoped_worker_workspace_tx(
    pool: ConnectionPool,
    *,
    workspace_id: UUID,
) -> Iterator[tuple[psycopg.Connection, int]]:
    """Run a background worker transaction scoped to a single workspace as `t007d_worker_app`.

    Uses `set_config(..., is_local=true)` so GUC state cannot leak to subsequent jobs
    reusing the same physical PostgreSQL connection.
    """
    with pool.connection() as conn:
        pid = get_backend_pid(conn)
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.current_workspace_id', %s, true)",
                    (str(workspace_id),),
                )
            yield conn, pid


@contextmanager
def unscoped_tx(pool: ConnectionPool) -> Iterator[tuple[psycopg.Connection, int]]:
    """Open a transaction WITHOUT setting any tenant context to test fail-closed RLS."""
    with pool.connection() as conn:
        pid = get_backend_pid(conn)
        with conn.transaction():
            yield conn, pid


def unsafe_set_session_workspace_id(conn: psycopg.Connection, workspace_id: UUID) -> None:
    """ANTI-PATTERN helper (test-only) demonstrating why session-level SET leaks across pool reuse."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT set_config('app.current_workspace_id', %s, false)",
            (str(workspace_id),),
        )
    conn.commit()
