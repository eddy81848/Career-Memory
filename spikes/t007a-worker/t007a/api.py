"""PROTOTYPE-ONLY submit/status API for T-007A. NOT the product API contract.

All routes live under ``/_prototype/t007a`` and every response carries
``X-T007A-Prototype: not-product-api``.

Run as a process with:
    uvicorn t007a.api:create_api --factory --host 127.0.0.1 --port 8080
"""

import hashlib
import json
import logging
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field

from .db import connect
from .faults import NO_FAULTS, FaultPlan, InjectedFault
from .settings import Settings
from .wake import HttpWake, Wake, WakeError, dispatch

log = logging.getLogger("t007a.api")
APP_NAME = "t007a-api"
PREFIX = "/_prototype/t007a"
PROTOTYPE_HEADER = ("X-T007A-Prototype", "not-product-api")


class SubmitBody(BaseModel):
    text: str = Field(min_length=1, max_length=10_000)


class IdempotencyConflict(Exception):
    pass


def request_hash(payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _record_job(dsn: str, faults: FaultPlan, key: str, payload: dict) -> tuple[UUID, bool, bool]:
    """Job + outbox in ONE transaction. Returns (job_id, replayed, needs_wake).

    A replay only re-sends the wake if an earlier wake *definitely failed*
    (outbox still PENDING with last_error). A replay racing an in-flight first
    request does not, which avoids wake amplification; an API crash between
    commit and wake (no error recorded) is left to bounded recovery.
    """
    rh = request_hash(payload)
    with connect(dsn, APP_NAME) as conn, conn.transaction():
        row = conn.execute(
            """
            INSERT INTO jobs (id, idempotency_key, request_hash, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            (uuid4(), key, rh, json.dumps(payload)),
        ).fetchone()
        if row is None:  # same logical request already committed (or committed concurrently)
            existing = conn.execute(
                """
                SELECT j.id, j.request_hash,
                       (o.status = 'PENDING' AND o.last_error IS NOT NULL) AS needs_wake
                  FROM jobs j JOIN outbox o ON o.job_id = j.id
                 WHERE j.idempotency_key = %s
                """,
                (key,),
            ).fetchone()
            if existing["request_hash"] != rh:
                raise IdempotencyConflict(key)
            return existing["id"], True, existing["needs_wake"]
        faults.maybe_raise("api.between_job_and_outbox")
        conn.execute("INSERT INTO outbox (job_id) VALUES (%s)", (row["id"],))
        faults.maybe_raise("api.before_commit")
    return row["id"], False, True


def create_api(
    settings: Settings | None = None,
    *,
    wake: Wake | None = None,
    faults: FaultPlan = NO_FAULTS,
) -> FastAPI:
    """``wake``/``faults`` are injection points for tests only; uvicorn uses defaults."""
    settings = settings or Settings.from_env()
    if wake is None and settings.worker_wake_url:
        wake = HttpWake(settings.worker_wake_url, settings.wake_timeout_s)

    def guarded_wake(job_id: UUID) -> None:
        if faults.should_fail("api.wake"):
            raise WakeError("injected")
        if wake is None:
            raise WakeError("no_wake_configured")
        wake(job_id)

    app = FastAPI(title="T-007A prototype API (NOT the product API)")

    @app.middleware("http")
    async def mark_prototype(request, call_next):
        response = await call_next(request)
        response.headers[PROTOTYPE_HEADER[0]] = PROTOTYPE_HEADER[1]
        return response

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.post(f"{PREFIX}/jobs", status_code=202)
    def submit(
        body: SubmitBody,
        response: Response,
        idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
    ):
        try:
            job_id, replayed, needs_wake = _record_job(
                settings.database_url, faults, idempotency_key, body.model_dump()
            )
        except IdempotencyConflict:
            raise HTTPException(409, {"code": "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_BODY"})
        # Committed. Only now is the external, bounded wake attempted.
        wake_state = "skipped"
        if needs_wake:
            wake_state = dispatch(settings.database_url, APP_NAME, guarded_wake, job_id)
        if faults.should_fail("api.response_lost"):
            raise InjectedFault("api.response_lost")
        status_url = f"{PREFIX}/jobs/{job_id}"
        response.headers["Location"] = status_url
        return {
            "job_id": str(job_id),
            "status_url": status_url,
            "replayed": replayed,
            "wake": wake_state,
            "note": "Poll status_url. Resubmit with the same Idempotency-Key and body to "
            "recover a lost response; it returns the same job_id.",
        }

    @app.get(f"{PREFIX}/jobs/{{job_id}}")
    def status(job_id: UUID):
        with connect(settings.database_url, APP_NAME) as conn:
            job = conn.execute(
                """
                SELECT id, status, terminal_reason, attempt_count, max_attempts,
                       created_at, finished_at
                  FROM jobs WHERE id = %s
                """,
                (job_id,),
            ).fetchone()
            if job is None:
                raise HTTPException(404, {"code": "JOB_NOT_FOUND"})
            outbox = conn.execute(
                """
                SELECT status, dispatch_attempts, recovery_attempts, last_error
                  FROM outbox WHERE job_id = %s
                """,
                (job_id,),
            ).fetchone()
            result = conn.execute(
                "SELECT attempt_no, worker_id, output, created_at FROM results WHERE job_id = %s",
                (job_id,),
            ).fetchone()
            attempts = conn.execute(
                """
                SELECT attempt_no, worker_id, outcome, error_class, claimed_at, finished_at
                  FROM job_attempts WHERE job_id = %s ORDER BY attempt_no
                """,
                (job_id,),
            ).fetchall()
        return {"job": job, "outbox": outbox, "result": result, "attempts": attempts}

    return app
