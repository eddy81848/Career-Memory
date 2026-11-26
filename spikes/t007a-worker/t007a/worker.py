"""Worker: short claim transaction -> processing outside the DB -> short save transaction.

Run as a process with:
    uvicorn t007a.worker:create_worker_app --factory --host 127.0.0.1 --port 8081
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Callable
from uuid import UUID, uuid4

from fastapi import Body, FastAPI, Query

from .db import connect
from .faults import NO_FAULTS, FaultPlan
from .fixtures import AmbiguousExternalTimeoutError, PermanentExternalError
from .settings import Settings

log = logging.getLogger("t007a.worker")
APP_NAME = "t007a-worker"
PROCESSOR_VERSION = "t007a-fixed-v1"


def fixed_process(payload: dict) -> dict:
    """Deterministic synthetic processing. Runs with NO DB transaction open."""
    text = payload["text"]
    return {
        "processor": PROCESSOR_VERSION,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "chars": len(text),
        "words": len(text.split()),
    }


@dataclass(frozen=True)
class Claim:
    job_id: UUID
    attempt_no: int
    lease_token: UUID
    payload: dict


class Worker:
    def __init__(
        self,
        dsn: str,
        worker_id: str,
        *,
        lease_s: float = 30.0,
        process: Callable[[dict], dict] = fixed_process,
    ):
        self.dsn = dsn
        self.worker_id = worker_id
        self.lease_s = lease_s
        self.process = process

    # --- transaction 1: claim (short) -------------------------------------------------
    def claim(self, hint: UUID | None = None) -> Claim | None:
        token = uuid4()
        with connect(self.dsn, APP_NAME) as conn, conn.transaction():
            row = conn.execute(
                """
                WITH c AS (
                    SELECT id, lease_token AS prev_token FROM jobs
                     WHERE attempt_count < max_attempts
                       AND (status = 'PENDING'
                            OR (status = 'RUNNING' AND lease_expires_at < now()))
                     ORDER BY (id = %(hint)s) DESC NULLS LAST, created_at
                     FOR UPDATE SKIP LOCKED
                     LIMIT 1
                )
                UPDATE jobs j
                   SET status = 'RUNNING',
                       attempt_count = j.attempt_count + 1,
                       lease_owner = %(worker)s,
                       lease_token = %(token)s,
                       lease_expires_at = now() + make_interval(secs => %(lease)s::float8),
                       updated_at = now()
                  FROM c
                 WHERE j.id = c.id
             RETURNING j.id, j.attempt_count, j.payload, c.prev_token
                """,
                {"hint": hint, "worker": self.worker_id, "token": token, "lease": self.lease_s},
            ).fetchone()
            if row is None:
                return None
            if row["prev_token"] is not None:
                # Reclaiming an expired RUNNING attempt: close the abandoned attempt row.
                conn.execute(
                    """
                    UPDATE job_attempts
                       SET outcome = 'EXPIRED', error_class = 'LeaseExpired', finished_at = now()
                     WHERE job_id = %s AND lease_token = %s AND outcome = 'RUNNING'
                    """,
                    (row["id"], row["prev_token"]),
                )
            conn.execute(
                """
                INSERT INTO job_attempts (job_id, attempt_no, worker_id, lease_token)
                VALUES (%s, %s, %s, %s)
                """,
                (row["id"], row["attempt_count"], self.worker_id, token),
            )
        return Claim(row["id"], row["attempt_count"], token, row["payload"])

    # --- transaction 2: save (short, fenced by lease token) ----------------------------
    def _save(self, claim: Claim, output: dict) -> str:
        with connect(self.dsn, APP_NAME) as conn, conn.transaction():
            owned = conn.execute(
                """
                UPDATE jobs
                   SET status = 'SUCCEEDED', finished_at = now(), updated_at = now(),
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                 WHERE id = %s AND lease_token = %s AND status = 'RUNNING'
             RETURNING id
                """,
                (claim.job_id, claim.lease_token),
            ).fetchone()
            if owned is None:
                outcome = "LEASE_LOST"
                self._finish_attempt(conn, claim, outcome, "LeaseFenceMismatch")
            else:
                # PK(job_id) is the backstop; with fencing this conflict should never fire.
                conn.execute(
                    """
                    INSERT INTO results (job_id, attempt_no, worker_id, output)
                    VALUES (%s, %s, %s, %s::jsonb)
                    """,
                    (claim.job_id, claim.attempt_no, self.worker_id, json.dumps(output)),
                )
                conn.execute(
                    "UPDATE outbox SET status = 'DONE', done_at = now(), last_error = NULL WHERE job_id = %s",
                    (claim.job_id,),
                )
                outcome = "SUCCEEDED"
                self._finish_attempt(conn, claim, outcome)
        return outcome

    def _quarantine_ambiguous(self, claim: Claim, exc: BaseException) -> str:
        """Unknown upstream side-effect completion: move to REVIEW_REQUIRED, never auto-retry."""
        reason = f"AMBIGUOUS_EXTERNAL_TIMEOUT:{type(exc).__name__}"
        with connect(self.dsn, APP_NAME) as conn, conn.transaction():
            owned = conn.execute(
                """
                UPDATE jobs
                   SET status = 'REVIEW_REQUIRED',
                       terminal_reason = %s,
                       finished_at = now(),
                       updated_at = now(),
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                 WHERE id = %s AND lease_token = %s AND status = 'RUNNING'
             RETURNING id
                """,
                (reason, claim.job_id, claim.lease_token),
            ).fetchone()
            if owned is None:
                self._finish_attempt(conn, claim, "LEASE_LOST", type(exc).__name__)
                return "LEASE_LOST"
            conn.execute(
                """
                UPDATE outbox
                   SET status = 'DONE', done_at = now(), last_error = %s
                 WHERE job_id = %s
                """,
                (reason, claim.job_id),
            )
            self._finish_attempt(conn, claim, "AMBIGUOUS_TIMEOUT", type(exc).__name__)
        return "AMBIGUOUS_TIMEOUT"

    def _fail(self, claim: Claim, exc: BaseException) -> str:
        err_name = type(exc).__name__
        permanent = isinstance(exc, PermanentExternalError)
        with connect(self.dsn, APP_NAME) as conn, conn.transaction():
            row = conn.execute(
                """
                UPDATE jobs
                   SET status = CASE WHEN %(perm)s OR attempt_count >= max_attempts
                                     THEN 'FAILED' ELSE 'PENDING' END,
                       terminal_reason = CASE
                           WHEN %(perm)s THEN 'PERMANENT_FAILURE:' || %(err)s
                           WHEN attempt_count >= max_attempts THEN 'MAX_ATTEMPTS_EXHAUSTED:' || %(err)s
                       END,
                       finished_at = CASE WHEN %(perm)s OR attempt_count >= max_attempts
                                          THEN now() END,
                       updated_at = now(),
                       lease_owner = NULL, lease_token = NULL, lease_expires_at = NULL
                 WHERE id = %(job)s AND lease_token = %(tok)s AND status = 'RUNNING'
             RETURNING status, terminal_reason
                """,
                {"perm": permanent, "err": err_name, "job": claim.job_id, "tok": claim.lease_token},
            ).fetchone()
            if row is None:
                self._finish_attempt(conn, claim, "LEASE_LOST", err_name)
                return "LEASE_LOST"
            if row["status"] == "FAILED":
                conn.execute(
                    """
                    UPDATE outbox
                       SET status = 'DONE', done_at = now(), last_error = %s
                     WHERE job_id = %s
                    """,
                    (row["terminal_reason"], claim.job_id),
                )
            else:
                # Retryable failure with remaining budget: mark outbox PENDING so recovery can wake.
                conn.execute(
                    "UPDATE outbox SET status = 'PENDING', last_error = %s WHERE job_id = %s",
                    (f"retryable:{err_name}", claim.job_id),
                )
            self._finish_attempt(conn, claim, "FAILED", err_name)
        return "FAILED"

    @staticmethod
    def _finish_attempt(conn, claim: Claim, outcome: str, error_class: str | None = None):
        conn.execute(
            """
            UPDATE job_attempts
               SET outcome = %s,
                   error_class = COALESCE(error_class, %s),
                   finished_at = COALESCE(finished_at, now())
             WHERE job_id = %s AND attempt_no = %s AND lease_token = %s
            """,
            (outcome, error_class, claim.job_id, claim.attempt_no, claim.lease_token),
        )

    # --- one unit of work ---------------------------------------------------------------
    def run_one(self, hint: UUID | None = None) -> dict | None:
        claim = self.claim(hint)
        if claim is None:
            return None
        try:
            output = self.process(claim.payload)  # no transaction is open here
        except AmbiguousExternalTimeoutError as exc:
            log.warning("ambiguous upstream timeout for %s; quarantining", claim.job_id)
            outcome = self._quarantine_ambiguous(claim, exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("processing failed for %s", claim.job_id, exc_info=True)
            outcome = self._fail(claim, exc)
        else:
            outcome = self._save(claim, output)
        return {"job_id": str(claim.job_id), "attempt_no": claim.attempt_no, "outcome": outcome}

    def drain(self, max_jobs: int, hint: UUID | None = None) -> list[dict]:
        done = []
        for i in range(max_jobs):
            item = self.run_one(hint if i == 0 else None)
            if item is None:
                break
            done.append(item)
        return done


def create_worker_app(
    settings: Settings | None = None,
    *,
    process: Callable[[dict], dict] = fixed_process,
    faults: FaultPlan = NO_FAULTS,
) -> FastAPI:
    """Worker HTTP surface. Internal only: on Cloud Run this must be IAM-protected
    (unverified here). ``faults``/``process`` are test-only Python injection points."""
    from .relay import recover  # local import keeps module graph acyclic

    settings = settings or Settings.from_env()
    worker = Worker(
        settings.database_url, settings.worker_id, lease_s=settings.lease_s, process=process
    )
    app = FastAPI(title="T-007A prototype worker (internal, NOT product)")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "worker_id": worker.worker_id}

    @app.post("/internal/t007a/wake")
    def wake(body: dict | None = Body(default=None)):
        hint = None
        if body and body.get("job_id"):
            try:
                hint = UUID(str(body["job_id"]))
            except ValueError:
                hint = None
        # Processing happens inside this request on purpose (no BackgroundTasks):
        # on Cloud Run CPU is only guaranteed while a request is in flight.
        processed = worker.drain(settings.max_jobs_per_wake, hint)
        faults.maybe_raise("worker.response_lost_after_save")
        return {"processed": processed}

    @app.post("/internal/t007a/recover")
    def recover_route(
        limit: int = Query(10, ge=1, le=50),
        grace_s: float = Query(30.0, ge=0, le=3600),
    ):
        def in_process_wake(job_id: UUID) -> None:
            worker.drain(1, job_id)

        return recover(settings.database_url, in_process_wake, limit=limit, grace_s=grace_s)

    return app
