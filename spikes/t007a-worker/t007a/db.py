"""PostgreSQL access helpers.

Every connection is autocommit; transactions are only opened explicitly with
``conn.transaction()`` so no code path can leave an implicit transaction open
while it talks to the network. ``idle_in_transaction_session_timeout`` is a
server-side guard that turns an accidental long transaction into an error.
"""

from collections import Counter
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

SCHEMA = "t007a_spike"
SCHEMA_SQL = Path(__file__).with_name("schema.sql")
SESSION_OPTIONS = (
    f"-c search_path={SCHEMA} "
    "-c idle_in_transaction_session_timeout=5000 "
    "-c lock_timeout=2000 "
    "-c statement_timeout=5000"
)


def connect(dsn: str, app_name: str) -> psycopg.Connection:
    return psycopg.connect(
        dsn,
        autocommit=True,
        application_name=app_name,
        row_factory=dict_row,
        options=SESSION_OPTIONS,
    )


def apply_schema(dsn: str) -> None:
    """Drop and recreate the spike schema. Only for throwaway databases."""
    with connect(dsn, "t007a-setup") as conn:
        conn.execute(SCHEMA_SQL.read_text())


def snapshot(dsn: str) -> dict:
    """Observable row counts/states used as test evidence."""
    with connect(dsn, "t007a-observer") as conn:
        jobs = conn.execute("SELECT status, attempt_count, terminal_reason FROM jobs").fetchall()
        outbox = conn.execute(
            "SELECT status, dispatch_attempts, recovery_attempts, last_error FROM outbox"
        ).fetchall()
        results = conn.execute("SELECT job_id, attempt_no FROM results").fetchall()
        attempts = conn.execute("SELECT outcome FROM job_attempts").fetchall()
    return {
        "jobs": {"count": len(jobs), "by_status": dict(Counter(r["status"] for r in jobs))},
        "job_details": {
            "attempt_counts": sorted(r["attempt_count"] for r in jobs),
            "terminal_reasons": sorted(
                r["terminal_reason"] for r in jobs if r["terminal_reason"]
            ),
        },
        "outbox": {
            "count": len(outbox),
            "by_status": dict(Counter(r["status"] for r in outbox)),
            "dispatch_attempts": sum(r["dispatch_attempts"] for r in outbox),
            "recovery_attempts": sum(r["recovery_attempts"] for r in outbox),
            "last_errors": sorted({r["last_error"] for r in outbox if r["last_error"]}),
        },
        "results": {"count": len(results)},
        "job_attempts": {
            "count": len(attempts),
            "by_outcome": dict(Counter(r["outcome"] for r in attempts)),
        },
    }


def open_app_transactions(dsn: str) -> list[dict]:
    """Sessions of prototype API/worker/relay that currently hold a transaction."""
    with connect(dsn, "t007a-observer") as conn:
        return conn.execute(
            """
            SELECT application_name, state, now() - xact_start AS xact_age
              FROM pg_stat_activity
             WHERE application_name IN ('t007a-api', 't007a-worker', 't007a-relay')
               AND xact_start IS NOT NULL
               AND pid <> pg_backend_pid()
            """
        ).fetchall()
