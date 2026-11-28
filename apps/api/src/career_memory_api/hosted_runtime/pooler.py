"""Supabase Supavisor Transaction Pooler (:6543) & least-privilege role guard (OD-006 / T-007D).

Key invariants:
- ``prepare_threshold=None`` is mandatory on ``psycopg_pool.ConnectionPool`` so server-side
  prepared statements are NEVER created (``prepare_threshold=0`` is forbidden because psycopg v3
  interprets ``0`` as preparing every statement immediately).
- Refuses privileged or disallowed roles (``postgres``, ``supabase_admin``, ``service_role``,
  ``cm_migrator``, etc.) both at DSN parse time and on live ``pg_roles`` verification
  (``rolsuper = false AND rolbypassrls = false`` and ``0`` owned public tables).
- Never uses startup ``options=-c ...`` (incompatible with Supavisor :6543), session-level ``SET``,
  ``LISTEN/NOTIFY``, ``TEMP TABLE``, or session advisory locks.
- Applies transaction-local timeouts via ``set_config(..., true)`` inside transactions and rolls
  back uncommitted state cleanly before returning connections to the pool.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import Any

import psycopg
from psycopg.pq import TransactionStatus
from psycopg_pool import ConnectionPool

from career_memory_api.auth.postgres_guard import bind_postgres_workspace_context
from career_memory_api.authz import WorkspaceAccessContext
from career_memory_api.hosted_runtime.models import (
    FORBIDDEN_RUNTIME_DB_ROLES,
    HostedDbPoolConfig,
    HostedRoleSecurityError,
    extract_base_role_name,
)

ConnectionFactory = Callable[[], AbstractContextManager[psycopg.Connection]]


def _reset_pooled_connection(conn: psycopg.Connection) -> None:
    """Ensure no uncommitted transaction or transaction-local GUC leaks back into the pool."""
    if conn.closed:
        return
    if conn.info.transaction_status != TransactionStatus.IDLE:
        conn.rollback()


def apply_transaction_local_timeouts(
    cur: psycopg.Cursor,
    *,
    statement_timeout_ms: int,
    lock_timeout_ms: int,
) -> None:
    """Apply bounded ``statement_timeout`` and ``lock_timeout`` strictly to the current tx."""
    cur.execute(
        """
        SELECT
            set_config('statement_timeout', %s, true),
            set_config('lock_timeout', %s, true)
        """,
        (f"{int(statement_timeout_ms)}ms", f"{int(lock_timeout_ms)}ms"),
    )


def verify_connection_role_security(
    conn: psycopg.Connection,
    *,
    expected_role_group: str | None = None,
) -> dict[str, Any]:
    """Verify the active PostgreSQL role is non-superuser, NOBYPASSRLS, and owns 0 public tables."""
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    current_user::text,
                    r.rolsuper,
                    r.rolbypassrls,
                    (
                        SELECT count(*)
                        FROM pg_tables t
                        WHERE t.schemaname = 'public'
                          AND t.tableowner = current_user
                    ) AS owned_public_tables
                FROM pg_roles r
                WHERE r.rolname = current_user
                """
            )
            row = cur.fetchone()
            if row is None:
                raise HostedRoleSecurityError("Unable to inspect current_user in pg_roles.")

            current_role = str(row[0])
            rolsuper = bool(row[1])
            rolbypassrls = bool(row[2])
            owned_tables = int(row[3])
            base_role = extract_base_role_name(current_role)

            if (
                current_role.lower() in FORBIDDEN_RUNTIME_DB_ROLES
                or base_role in FORBIDDEN_RUNTIME_DB_ROLES
            ):
                raise HostedRoleSecurityError(
                    f"Connected role {current_role!r} is forbidden for runtime workloads (OD-006)."
                )
            if rolsuper:
                raise HostedRoleSecurityError(
                    f"Connected role {current_role!r} has rolsuper=true; runtime roles must be NOSUPERUSER."
                )
            if rolbypassrls:
                raise HostedRoleSecurityError(
                    f"Connected role {current_role!r} has rolbypassrls=true; runtime roles must be NOBYPASSRLS."
                )
            if owned_tables > 0:
                raise HostedRoleSecurityError(
                    f"Connected role {current_role!r} owns {owned_tables} public table(s); "
                    "runtime roles must never own tables (OD-006-1)."
                )

            if expected_role_group is not None:
                cur.execute(
                    "SELECT pg_has_role(current_user, %s, 'MEMBER')",
                    (expected_role_group,),
                )
                has_group = bool((cur.fetchone() or (False,))[0])
                if not has_group:
                    raise HostedRoleSecurityError(
                        f"Connected role {current_role!r} is not a member of required role {expected_role_group!r}."
                    )

    return {
        "current_user": current_role,
        "rolsuper": rolsuper,
        "rolbypassrls": rolbypassrls,
        "owned_public_tables": owned_tables,
        "expected_role_group": expected_role_group,
    }


class HostedTransactionPool:
    """Transaction-pooler-safe ``psycopg_pool.ConnectionPool`` wrapper for Cloud Run API/Worker."""

    def __init__(
        self,
        config: HostedDbPoolConfig,
        *,
        verify_role_on_first_checkout: bool = True,
    ) -> None:
        self._config = config
        self._verify_role_on_first_checkout = verify_role_on_first_checkout
        self._role_verified = False
        self._role_audit: dict[str, Any] | None = None
        self._lock = threading.Lock()
        self._opened = False

        self._pool = ConnectionPool(
            conninfo=config.database_url,
            min_size=config.min_size,
            max_size=config.max_size,
            open=False,
            check=ConnectionPool.check_connection,
            max_idle=45.0,
            reconnect_timeout=5.0,
            timeout=10.0,
            kwargs={
                "prepare_threshold": None,
                "autocommit": False,
                "connect_timeout": config.connect_timeout_seconds,
            },
            reset=_reset_pooled_connection,
        )

    @property
    def config(self) -> HostedDbPoolConfig:
        return self._config

    @property
    def role_audit(self) -> dict[str, Any] | None:
        return dict(self._role_audit) if self._role_audit is not None else None

    def open(self) -> None:
        with self._lock:
            if not self._opened:
                self._pool.open(wait=self._config.min_size > 0)
                self._opened = True

    def close(self) -> None:
        with self._lock:
            if self._opened:
                self._pool.close()
                self._opened = False

    def get_stats(self) -> dict[str, int]:
        """Return live ``psycopg_pool`` telemetry (including ``connections_in_use``)."""
        if not self._opened:
            return {
                "pool_min": self._config.min_size,
                "pool_max": self._config.max_size,
                "pool_size": 0,
                "pool_available": 0,
                "requests_waiting": 0,
                "connections_in_use": 0,
            }
        raw = self._pool.get_stats()
        pool_size = int(raw.get("pool_size", 0))
        pool_available = int(raw.get("pool_available", 0))
        in_use = max(0, pool_size - pool_available)
        return {
            **{k: int(v) for k, v in raw.items() if isinstance(v, (int, float))},
            "connections_in_use": in_use,
        }

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection]:
        """Check out a transaction-pooler connection and guarantee clean rollback on exit."""
        self.open()
        with self._pool.connection() as conn:
            # Verify prepare_threshold is disabled on every connection
            if conn.prepare_threshold is not None:
                conn.prepare_threshold = None

            if self._verify_role_on_first_checkout and not self._role_verified:
                with self._lock:
                    if not self._role_verified:
                        self._role_audit = verify_connection_role_security(
                            conn,
                            expected_role_group=self._config.expected_role_group,
                        )
                        self._role_verified = True
            try:
                yield conn
            finally:
                if not conn.closed and conn.info.transaction_status != TransactionStatus.IDLE:
                    conn.rollback()

    @contextmanager
    def scoped_transaction(
        self,
        *,
        access: WorkspaceAccessContext | None = None,
        workspace_id: str | None = None,
        read_only: bool = False,
    ) -> Iterator[psycopg.Cursor]:
        """Open a pooled transaction with transaction-local timeouts and optional RLS context."""
        with self.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    if read_only:
                        cur.execute("SET TRANSACTION READ ONLY")
                    apply_transaction_local_timeouts(
                        cur,
                        statement_timeout_ms=self._config.statement_timeout_ms,
                        lock_timeout_ms=self._config.lock_timeout_ms,
                    )
                    if access is not None:
                        bind_postgres_workspace_context(cur, access)
                    elif workspace_id is not None:
                        cur.execute(
                            "SELECT set_config('app.current_workspace_id', %s, true)",
                            (workspace_id,),
                        )
                    yield cur

    def as_connection_factory(self) -> ConnectionFactory:
        """Return a ``ConnectionFactory`` compatible with all M1-M5 PostgreSQL services."""

        @contextmanager
        def _checkout() -> Iterator[psycopg.Connection]:
            with self.connection() as conn:
                yield conn

        return _checkout

    def __enter__(self) -> HostedTransactionPool:
        self.open()
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()


@contextmanager
def scoped_pool_connection(
    pool: HostedTransactionPool,
    *,
    access: WorkspaceAccessContext | None = None,
    workspace_id: str | None = None,
    read_only: bool = False,
) -> Iterator[psycopg.Cursor]:
    """Convenience helper opening a timeout-guarded transaction-local cursor on ``pool``."""
    with pool.scoped_transaction(
        access=access,
        workspace_id=workspace_id,
        read_only=read_only,
    ) as cur:
        yield cur


__all__ = [
    "ConnectionFactory",
    "HostedTransactionPool",
    "apply_transaction_local_timeouts",
    "scoped_pool_connection",
    "verify_connection_role_security",
]
