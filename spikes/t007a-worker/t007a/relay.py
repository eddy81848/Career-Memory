"""Bounded outbox recovery ("relay").

In a single short SKIP LOCKED transaction, ``recover(...)``:
1. Reaps exhausted jobs whose lease has expired (``status = 'RUNNING'``,
   ``lease_expires_at < now()``, ``attempt_count >= max_attempts``): transitions
   ``jobs`` to ``FAILED`` with ``terminal_reason = 'MAX_ATTEMPTS_LEASE_EXPIRED'``,
   marks the abandoned ``job_attempts`` row ``LEASE_LOST``, and marks ``outbox``
   ``DONE`` so repeated recovery never wakes them again.
2. Closes stale ``outbox`` rows whose job is already in a terminal/quarantined
   state (``SUCCEEDED``, ``FAILED``, ``REVIEW_REQUIRED``).
3. Selects at most the remaining ``limit`` claimable non-DONE ``outbox`` rows
   older than ``grace_s`` (``PENDING``, or ``RUNNING`` with an expired lease and
   attempts remaining) and stamps ``last_recovery_at`` so concurrent relays do
   not double-dispatch.

Wakes for (3) are sent strictly after the transaction commits.

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
EXHAUSTED_REASON = "MAX_ATTEMPTS_LEASE_EXPIRED"


def recover(dsn: str, wake: Wake | None, *, limit: int = 10, grace_s: float = 30.0) -> dict:
    limit = max(1, min(int(limit), MAX_LIMIT))
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        # 1) Reap exhausted expired-lease RUNNING jobs atomically (job + attempt + outbox).
        reaped_rows = conn.execute(
            """
            WITH candidates AS (
                SELECT j.id AS job_id, j.attempt_count, j.lease_token
                  FROM jobs j
                  JOIN outbox o ON o.job_id = j.id
                 WHERE o.status <> 'DONE'
                   AND j.status = 'RUNNING'
                   AND j.lease_expires_at < now()
                   AND j.attempt_count >= j.max_attempts
                   AND o.created_at <= now() - make_interval(secs => %(g)s::float8)
                 ORDER BY o.created_at
                 FOR UPDATE OF j, o SKIP LOCKED
                 LIMIT %(n)s
            ),
            upd_jobs AS (
                UPDATE jobs j
                   SET status = 'FAILED',
                       terminal_reason = %(reason)s,
                       finished_at = now(),
                       updated_at = now(),
                       lease_owner = NULL,
                       lease_token = NULL,
                       lease_expires_at = NULL
                  FROM candidates c
                 WHERE j.id = c.job_id
             RETURNING j.id
            ),
            upd_attempts AS (
                UPDATE job_attempts a
                   SET outcome = 'LEASE_LOST',
                       error_class = 'LeaseExpiredAtMaxAttempts',
                       finished_at = COALESCE(a.finished_at, now())
                  FROM candidates c
                 WHERE a.job_id = c.job_id
                   AND a.attempt_no = c.attempt_count
                   AND a.lease_token = c.lease_token
                   AND a.outcome = 'RUNNING'
            )
            UPDATE outbox o
               SET status = 'DONE',
                   done_at = now(),
                   last_recovery_at = now(),
                   recovery_attempts = o.recovery_attempts + 1,
                   last_error = %(reason)s
              FROM candidates c
             WHERE o.job_id = c.job_id
         RETURNING o.job_id
            """,
            {"g": grace_s, "n": limit, "reason": EXHAUSTED_REASON},
        ).fetchall()

        remaining = limit - len(reaped_rows)
        rows = []
        if remaining > 0:
            # Close any stale non-DONE outbox whose job already converged or quarantined
            # (e.g. REVIEW_REQUIRED) so it can never be woken again.
            conn.execute(
                """
                WITH settled AS (
                    SELECT o.job_id
                      FROM outbox o
                      JOIN jobs j ON j.id = o.job_id
                     WHERE o.status <> 'DONE'
                       AND j.status IN ('SUCCEEDED', 'FAILED', 'REVIEW_REQUIRED')
                     FOR UPDATE OF o SKIP LOCKED
                     LIMIT %(n)s
                )
                UPDATE outbox o
                   SET status = 'DONE', done_at = COALESCE(o.done_at, now())
                  FROM settled s
                 WHERE o.job_id = s.job_id
                """,
                {"n": remaining},
            )
            # 2) Select only claimable jobs (PENDING or expired-lease RUNNING with budget left).
            rows = conn.execute(
                """
                WITH c AS (
                    SELECT o.job_id
                      FROM outbox o
                      JOIN jobs j ON j.id = o.job_id
                     WHERE o.status <> 'DONE'
                       AND j.attempt_count < j.max_attempts
                       AND (j.status = 'PENDING'
                            OR (j.status = 'RUNNING' AND j.lease_expires_at < now()))
                       AND o.created_at <= now() - make_interval(secs => %(g)s::float8)
                       AND (o.last_recovery_at IS NULL
                            OR o.last_recovery_at <= now() - make_interval(secs => %(g)s::float8))
                     ORDER BY o.created_at
                     FOR UPDATE OF o SKIP LOCKED
                     LIMIT %(n)s
                )
                UPDATE outbox o
                   SET last_recovery_at = now(), recovery_attempts = o.recovery_attempts + 1
                  FROM c
                 WHERE o.job_id = c.job_id
             RETURNING o.job_id
                """,
                {"g": grace_s, "n": remaining},
            ).fetchall()

    reaped_outcomes = {str(r["job_id"]): "reaped_failed" for r in reaped_rows}
    wake_outcomes = {str(r["job_id"]): dispatch(dsn, APP_NAME, wake, r["job_id"]) for r in rows}
    return {
        "selected": len(rows),
        "reaped": len(reaped_rows),
        "acknowledged": sum(v == "acknowledged" for v in wake_outcomes.values()),
        "failed": sum(v != "acknowledged" for v in wake_outcomes.values()),
        "jobs": {**reaped_outcomes, **wake_outcomes},
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
