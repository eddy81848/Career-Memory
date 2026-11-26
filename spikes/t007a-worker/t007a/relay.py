"""Bounded outbox recovery ("relay").

Finds outbox rows that are not DONE and older than ``grace_s`` and re-sends the
wake hint for at most ``limit`` of them. Selection happens in one short
transaction (SKIP LOCKED + ``last_recovery_at`` stamp so concurrent relays do
not double-dispatch); wakes are sent after that transaction commits.

Locally: ``python -m t007a.relay --worker-url http://127.0.0.1:8081/internal/t007a/wake``.
On Cloud Run this would be triggered by a scheduler (unverified, out of scope).
"""

import argparse
import json
import os

from .db import connect
from .wake import HttpWake, Wake, dispatch

APP_NAME = "t007a-relay"
MAX_LIMIT = 50


def recover(dsn: str, wake: Wake | None, *, limit: int = 10, grace_s: float = 30.0) -> dict:
    limit = max(1, min(int(limit), MAX_LIMIT))
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        rows = conn.execute(
            """
            WITH c AS (
                SELECT job_id FROM outbox
                 WHERE status <> 'DONE'
                   AND created_at <= now() - make_interval(secs => %(g)s::float8)
                   AND (last_recovery_at IS NULL
                        OR last_recovery_at <= now() - make_interval(secs => %(g)s::float8))
                 ORDER BY created_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %(n)s
            )
            UPDATE outbox o
               SET last_recovery_at = now(), recovery_attempts = o.recovery_attempts + 1
              FROM c
             WHERE o.job_id = c.job_id
         RETURNING o.job_id
            """,
            {"g": grace_s, "n": limit},
        ).fetchall()
    outcomes = {str(r["job_id"]): dispatch(dsn, APP_NAME, wake, r["job_id"]) for r in rows}
    return {
        "selected": len(rows),
        "acknowledged": sum(v == "acknowledged" for v in outcomes.values()),
        "failed": sum(v != "acknowledged" for v in outcomes.values()),
        "jobs": outcomes,
    }


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--worker-url", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--grace-s", type=float, default=30.0)
    p.add_argument("--timeout-s", type=float, default=2.0)
    a = p.parse_args()
    dsn = os.environ["T007A_DATABASE_URL"]
    out = recover(dsn, HttpWake(a.worker_url, a.timeout_s), limit=a.limit, grace_s=a.grace_s)
    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
